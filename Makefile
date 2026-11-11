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

# Run eval suite (24 scenarios; mock provider)
eval:
	LLM_PROVIDER=mock CRM_PROVIDER=mock EMAIL_PROVIDER=mock python -m opsmesh.eval.runner --scenarios=all --output=eval_results.jsonl

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
