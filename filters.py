"""The subscription filter — a JSON predicate over an event payload.

The user-facing reaction rule is *event + filter + target*
(docs/pending/forms-events-transports.md §3). The filter half is what turns
"tell me about listings" into "tell me about listings in Berlin over 500";
without it every subscriber receives every fact of its type and does the
selection itself, which is the same as having no filter language and a lot
more traffic.

Deliberate non-features, each of them a refusal rather than an omission:

- **no regular expressions.** A predicate is authored by whoever owns the
  subscription, evaluated in our process, once per matching event. A regex
  there is a catastrophic-backtracking lever pointed at the dispatcher.
  ``$prefix`` and ``$contains`` cover what regexes were wanted for.
- **no arbitrary expressions / no eval.** Every operator is a fixed name.
- **bounded depth.** ``STAPEL_WEBHOOKS["MAX_FILTER_DEPTH"]`` caps nesting,
  validated when the subscription is written, so a hostile predicate cannot
  be paid for at delivery time.
- **missing is not null.** ``{"city": None}`` matches a payload whose
  ``city`` IS null, not one that lacks the key; ``{"city": {"$exists":
  false}}`` is how "lacks the key" is spelled. Conflating them is how a
  filter silently widens.

Grammar::

    predicate := {path: matcher, ...}            # keys AND together
                 | {"$or": [predicate, ...]}     # at least one
                 | {"$and": [predicate, ...]}
                 | {"$not": predicate}
    matcher   := <scalar>                        # equality
                 | {"$eq"|"$ne": scalar}
                 | {"$in"|"$nin": [scalar, ...]}
                 | {"$gt"|"$gte"|"$lt"|"$lte": number}
                 | {"$exists": bool}
                 | {"$contains": scalar}         # substring or list member
                 | {"$prefix": str}

``path`` is dotted (``owner.id``) and traverses objects only — a list is a
value, matched with ``$contains``/``$in``, never indexed. An empty predicate
matches everything, which is what an unfiltered subscription is.
"""
from __future__ import annotations

from numbers import Number

#: Group operators — the only keys allowed to appear at predicate level.
GROUP_OPS = ("$or", "$and", "$not")

#: Value operators, per field.
FIELD_OPS = (
    "$eq", "$ne", "$in", "$nin", "$gt", "$gte", "$lt", "$lte",
    "$exists", "$contains", "$prefix",
)

_MISSING = object()


class InvalidFilter(Exception):
    """The predicate is not a filter this module will evaluate."""


def _resolve(payload, path: str):
    """Walk a dotted path; ``_MISSING`` when any hop is absent."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _compare(op: str, value, operand) -> bool:
    if op == "$eq":
        return value is not _MISSING and value == operand
    if op == "$ne":
        return value is _MISSING or value != operand
    if op == "$in":
        return value is not _MISSING and value in operand
    if op == "$nin":
        return value is _MISSING or value not in operand
    if op == "$exists":
        return (value is not _MISSING) is bool(operand)
    if op in ("$gt", "$gte", "$lt", "$lte"):
        # A bool is a Number in Python; ordering booleans against numbers is
        # never what an author meant, so it does not match rather than
        # comparing True > 0.
        if value is _MISSING or isinstance(value, bool) or not isinstance(value, Number):
            return False
        if op == "$gt":
            return value > operand
        if op == "$gte":
            return value >= operand
        if op == "$lt":
            return value < operand
        return value <= operand
    if op == "$contains":
        if value is _MISSING:
            return False
        if isinstance(value, str):
            return isinstance(operand, str) and operand in value
        if isinstance(value, (list, tuple)):
            return operand in value
        return False
    if op == "$prefix":
        return (
            value is not _MISSING
            and isinstance(value, str)
            and isinstance(operand, str)
            and value.startswith(operand)
        )
    raise InvalidFilter(f"unknown operator {op!r}")


def _match_field(value, matcher) -> bool:
    if isinstance(matcher, dict):
        # An empty operator object matches nothing meaningful; validation
        # refuses it, and evaluation agrees rather than matching everything.
        if not matcher:
            return False
        return all(_compare(op, value, operand) for op, operand in matcher.items())
    return value is not _MISSING and value == matcher


def matches(payload: dict, predicate: dict | None) -> bool:
    """Whether *payload* satisfies *predicate*. Empty predicate = always.

    Never raises on payload shape: an event whose payload is not an object,
    or lacks the keys a predicate names, simply does not match. A filter
    that could crash the dispatcher would take every other subscription of
    that event down with it.
    """
    if not predicate:
        return True
    if not isinstance(predicate, dict):
        return False
    if not isinstance(payload, dict):
        payload = {}
    for key, matcher in predicate.items():
        if key == "$or":
            if not any(matches(payload, sub) for sub in matcher or ()):
                return False
        elif key == "$and":
            if not all(matches(payload, sub) for sub in matcher or ()):
                return False
        elif key == "$not":
            if matches(payload, matcher):
                return False
        elif key.startswith("$"):
            return False
        elif not _match_field(_resolve(payload, key), matcher):
            return False
    return True


def validate_filter(predicate, *, max_depth: int | None = None, _depth: int = 1) -> None:
    """Refuse a predicate this module cannot evaluate. Raises
    :class:`InvalidFilter`.

    Called when a subscription is written, never at delivery time: a
    predicate is authored once and evaluated on every matching event, so
    that is where the cost of checking it belongs.
    """
    if predicate in (None, {}):
        return
    if max_depth is None:
        from .conf import webhooks_settings

        max_depth = int(webhooks_settings.MAX_FILTER_DEPTH or 4)
    if _depth > max_depth:
        raise InvalidFilter(f"filter nests deeper than {max_depth} levels")
    if not isinstance(predicate, dict):
        raise InvalidFilter("filter must be an object")

    for key, matcher in predicate.items():
        if not isinstance(key, str) or not key:
            raise InvalidFilter("filter keys must be non-empty strings")
        if key in ("$or", "$and"):
            if not isinstance(matcher, list) or not matcher:
                raise InvalidFilter(f"{key} takes a non-empty list of predicates")
            for sub in matcher:
                validate_filter(sub, max_depth=max_depth, _depth=_depth + 1)
            continue
        if key == "$not":
            validate_filter(matcher, max_depth=max_depth, _depth=_depth + 1)
            continue
        if key.startswith("$"):
            raise InvalidFilter(f"unknown group operator {key!r}")
        if any(part == "" for part in key.split(".")):
            raise InvalidFilter(f"malformed payload path {key!r}")
        _validate_matcher(key, matcher)


def _validate_matcher(path: str, matcher) -> None:
    if not isinstance(matcher, dict):
        return  # a scalar (or null) is the equality shorthand
    if not matcher:
        raise InvalidFilter(f"{path}: empty operator object")
    for op, operand in matcher.items():
        if op not in FIELD_OPS:
            raise InvalidFilter(f"{path}: unknown operator {op!r}")
        if op in ("$in", "$nin") and not isinstance(operand, list):
            raise InvalidFilter(f"{path}: {op} takes a list")
        if op == "$exists" and not isinstance(operand, bool):
            raise InvalidFilter(f"{path}: $exists takes a boolean")
        if op == "$prefix" and not isinstance(operand, str):
            raise InvalidFilter(f"{path}: $prefix takes a string")
        if op in ("$gt", "$gte", "$lt", "$lte"):
            if isinstance(operand, bool) or not isinstance(operand, Number):
                raise InvalidFilter(f"{path}: {op} takes a number")


__all__ = ["FIELD_OPS", "GROUP_OPS", "InvalidFilter", "matches", "validate_filter"]
