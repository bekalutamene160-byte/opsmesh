# OPSMESH Makefile — works on macOS/Linux. On Windows PowerShell, use the
# equivalent python -m commands documented in README.md §Quick Start.

.PHONY: demo test eval lint typecheck db-reset db-seed db-migrate

# One-command demo (course OPS-1: < 5 min from clean checkout)
demo:
	python -m venv venv
	./venv/bin/activate && pip install -r requirements.txt
	./venv/bin/activate && python -m opsmesh.scripts.seed
	./venv/bin/activate & python -m opsmesh.api.app &
	./venv/bin/activate && python -m opsmesh.worker &

# Run all tests
test:
	pytest tests/ --cov=opsmesh --cov-fail-under=80

# Run eval suite (32 scenarios; mock providers, no API keys needed).
# Exit code 0 = all pass, 1 = any fail. Use in CI to gate merges.
eval:
	OPSMESH_LLM_PROVIDER=mock OPSMESH_CRM_PROVIDER=mock OPSMESH_EMAIL_PROVIDER=mock \
	python -m opsmesh.eval.runner --scenarios=all --output=eval_results.jsonl

# Eval + unit tests together (CI gate)
ci: test eval
	@echo "CI gate passed."

# Lint + typecheck
lint:
	ruff check opsmesh/ tests/
	mypy --strict opsmesh/

# Database
db-reset:
	python -m opsmesh.scripts.db_reset

db-seed:
	python -m opsmesh.scripts.seed

db-migrate:
	alembic upgrade head
