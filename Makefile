PY ?= .venv/bin/python
PORT ?= 8000

.PHONY: venv seed serve sweep test lint deploy demo

venv:
	uv venv -p 3.12 .venv
	uv pip install -e ".[dev]"

seed:
	$(PY) -m pantrypilot.cli seed

serve:
	$(PY) -m uvicorn app.server:app --reload --port $(PORT)

sweep:
	$(PY) -m pantrypilot.cli sweep

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

deploy:
	./scripts/deploy_agentcore.sh

deploy-web:
	./scripts/deploy_web.sh

demo:
	./scripts/demo.sh
