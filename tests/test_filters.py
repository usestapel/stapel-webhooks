"""The subscription filter language.

Two properties carry the feature and both are asserted here: what a
predicate SELECTS, and what the language REFUSES. The refusals matter as
much — a predicate is evaluated once per matching event in the dispatcher's
own process.
"""
import pytest
from django.test import override_settings

from stapel_webhooks.filters import InvalidFilter, matches, validate_filter

PAYLOAD = {
    "listing_id": "abc",
    "price": 500,
    "city": "berlin",
    "tags": ["new", "featured"],
    "owner": {"id": "u1", "verified": True},
    "note": None,
}


class TestMatching:
    def test_empty_filter_matches_everything(self):
        assert matches(PAYLOAD, {}) is True
        assert matches(PAYLOAD, None) is True

    def test_scalar_equality_and_and_semantics(self):
        assert matches(PAYLOAD, {"city": "berlin"}) is True
        assert matches(PAYLOAD, {"city": "berlin", "price": 500}) is True
        assert matches(PAYLOAD, {"city": "berlin", "price": 600}) is False

    def test_dotted_path(self):
        assert matches(PAYLOAD, {"owner.id": "u1"}) is True
        assert matches(PAYLOAD, {"owner.verified": True}) is True
        assert matches(PAYLOAD, {"owner.missing": "x"}) is False

    def test_comparison_operators(self):
        assert matches(PAYLOAD, {"price": {"$gt": 100}}) is True
        assert matches(PAYLOAD, {"price": {"$gte": 500, "$lt": 501}}) is True
        assert matches(PAYLOAD, {"price": {"$lte": 499}}) is False

    def test_membership_and_text(self):
        assert matches(PAYLOAD, {"city": {"$in": ["berlin", "paris"]}}) is True
        assert matches(PAYLOAD, {"city": {"$nin": ["paris"]}}) is True
        assert matches(PAYLOAD, {"tags": {"$contains": "featured"}}) is True
        assert matches(PAYLOAD, {"listing_id": {"$prefix": "ab"}}) is True
        assert matches(PAYLOAD, {"listing_id": {"$prefix": "zz"}}) is False

    def test_missing_is_not_null(self):
        """The distinction a filter language silently widens if it conflates
        them: a null field is present, an absent one is not."""
        assert matches(PAYLOAD, {"note": None}) is True
        assert matches(PAYLOAD, {"nope": None}) is False
        assert matches(PAYLOAD, {"nope": {"$exists": False}}) is True
        assert matches(PAYLOAD, {"note": {"$exists": True}}) is True

    def test_ne_is_true_for_a_missing_key(self):
        assert matches(PAYLOAD, {"nope": {"$ne": "x"}}) is True
        assert matches(PAYLOAD, {"city": {"$ne": "berlin"}}) is False

    def test_groups(self):
        assert matches(PAYLOAD, {"$or": [{"city": "paris"}, {"city": "berlin"}]}) is True
        assert matches(PAYLOAD, {"$or": [{"city": "paris"}]}) is False
        assert matches(PAYLOAD, {"$and": [{"city": "berlin"}, {"price": 500}]}) is True
        assert matches(PAYLOAD, {"$not": {"city": "paris"}}) is True

    def test_booleans_are_not_compared_as_numbers(self):
        assert matches({"flag": True}, {"flag": {"$gt": 0}}) is False

    def test_hostile_payload_shapes_do_not_raise(self):
        assert matches(None, {"a": 1}) is False
        assert matches([], {"a": 1}) is False
        assert matches({"a": {"b": 1}}, {"a.b.c": 1}) is False


class TestValidation:
    def test_valid_predicates_pass(self):
        validate_filter({"city": "berlin", "price": {"$gte": 100}})
        validate_filter({"$or": [{"a": 1}, {"$not": {"b": 2}}]})
        validate_filter({})
        validate_filter(None)

    @pytest.mark.parametrize("bad", [
        {"a": {"$regex": ".*"}},          # no regexes, ever
        {"a": {"$in": "not-a-list"}},
        {"a": {"$exists": "yes"}},
        {"a": {"$gt": "500"}},
        {"a": {"$prefix": 3}},
        {"a": {}},
        {"$nope": [{"a": 1}]},
        {"$or": []},
        {"a..b": 1},
        {"": 1},
        "not a dict",
    ])
    def test_refusals(self, bad):
        with pytest.raises(InvalidFilter):
            validate_filter(bad)

    @override_settings(STAPEL_WEBHOOKS={"MAX_FILTER_DEPTH": 2})
    def test_depth_is_bounded(self):
        validate_filter({"$or": [{"a": 1}]})
        with pytest.raises(InvalidFilter):
            validate_filter({"$or": [{"$or": [{"a": 1}]}]})
