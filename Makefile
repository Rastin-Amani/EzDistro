.PHONY: install css css-watch bootstrap web worker test lint typecheck check \
	install-worker-service install-web-service

# The project interpreter lives in .venv (Python 3.12) — bare `python` is NOT
# on PATH on fresh servers, so every target resolves it explicitly.
PY := .venv/bin/python

install:
	$(PY) -m pip install -r requirements.txt
	npm install

css:
	npm run css:build

css-watch:
	npm run css:watch

bootstrap:
	$(PY) -m app.scripts.bootstrap_pb

web:
	$(PY) -m uvicorn app.main:app --reload --port 8000

worker:
	$(PY) -m app.workers.worker

test:
	$(PY) -m pytest tests/ -q

lint:
	.venv/bin/ruff check app/ tests/ && .venv/bin/ruff format --check app/ tests/

typecheck:
	.venv/bin/mypy app/

check: lint typecheck test

# systemd services (runs both processes as managed services on this host)
install-worker-service:
	@echo "Installing seoz-worker.service for user $(USER)…"
	@sed -e "s/__USER__/$(USER)/g" -e "s|__ROOT__|$(CURDIR)|g" deploy/seoz-worker.service.tpl | sudo tee /etc/systemd/system/seoz-worker.service > /dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable --now seoz-worker
	@echo "seoz-worker installed. Logs: journalctl -u seoz-worker -f"

install-web-service:
	@echo "Installing seoz-web.service for user $(USER)…"
	@sed -e "s/__USER__/$(USER)/g" -e "s|__ROOT__|$(CURDIR)|g" deploy/seoz-web.service.tpl | sudo tee /etc/systemd/system/seoz-web.service > /dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable --now seoz-web
	@echo "seoz-web installed. Logs: journalctl -u seoz-web -f"