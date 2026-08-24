# stapel-webhooks — the local gate.
#
# PYTHON must have the module + its deps importable (the repo venv, or a CI
# venv). Every target here is also a CI step, so a green `make` locally is
# the same verdict CI reaches.
PYTHON ?= python3

.PHONY: lint test emit-check migration-lint check contract contract-check

# The contract triad — schema.json + flows.json + errors.json — emitted from a
# single-module {webhooks + core} Django instance mounted at the canonical
# /webhooks/api/v1 prefix (see _codegen.py / _codegen_settings.py /
# codegen_urls.py). Emission is pinned to Python 3.12: drf-spectacular renders
# component descriptions differently across minors, and a contract emitted on
# the wrong one produces false diffs forever.
#
# This module is not mounted in stapel-example-monolith, so until it is there
# is no aggregate slice to diff against — which is exactly why the triad has
# to live HERE. A module whose only OpenAPI lives inside somebody's host is a
# module no frontend codegen can generate a client for, and the react pair
# then hand-writes the types off presenters.py and finds out at runtime.
contract:
	$(PYTHON) -m stapel_webhooks._codegen --out docs

# Drift gate: regenerate into a temp dir and diff against the committed docs/*.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_webhooks._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json; do \
		if ! cmp -s "docs/$$f" "$$tmp/$$f"; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors}.json up to date"; fi; \
	exit $$rc

# The ruff selection the git hooks and CI both use (single source: pyproject).
lint:
	ruff check . --select E,F,W --ignore E501

test:
	$(PYTHON) -m pytest tests/ -q

# Outbox discipline: an emit that is not inside the mutating transaction, or
# one whose failure is swallowed, is a row that exists without the fact it
# announced. This module emits three facts about its own deliveries, all of
# them from inside the attempt's transaction.
emit-check:
	$(PYTHON) -m stapel_core.lint.emit_check .

# Expand/contract gate for Django migrations (release-management.md §3).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict $(if $(BASE_SHA),--base-sha $(BASE_SHA),)

check: lint emit-check contract-check test
