.PHONY: install css bootstrap web worker test lint typecheck check

install:
	pip install -r requirements.txt
	npm install

css:
	npm run css:build

css-watch:
	npm run css:watch

bootstrap:
	python -m app.scripts.bootstrap_pb

web:
	uvicorn app.main:app --reload --port 8000

worker:
	python -m app.workers.worker

test:
	pytest tests/ -q

lint:
	ruff check app/ tests/ && ruff format --check app/ tests/

typecheck:
	mypy app/

check: lint typecheck test
