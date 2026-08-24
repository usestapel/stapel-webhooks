# stapel-webhooks — the local gate.
#
# PYTHON must have the module + its deps importable (the repo venv, or a CI
# venv). Every target here is also a CI step, so a green `make` locally is
# the same verdict CI reaches.
PYTHON ?= python3

.PHONY: lint test emit-check migration-lint check

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

check: lint emit-check test
