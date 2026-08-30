.PHONY: install css css-watch bootstrap web worker test lint typecheck check \
	i18n-extract i18n-add i18n-update i18n-compile \
	install-worker-service install-web-service

# Babel CLI (pybabel) — same venv interpreter.
PYBABEL := .venv/bin/pybabel

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

# --- i18n workflow (gettext/Babel) ---
# msgids are the Persian source strings; catalogs live in app/locales/<code>/LC_MESSAGES/.
i18n-extract:
	$(PYBABEL) extract -F babel.cfg -k _ -k ngettext:1,2 -o app/locales/messages.pot app

i18n-add: i18n-extract ## new locale: make i18n-add LOCALE=de
	$(PYBABEL) init -i app/locales/messages.pot -d app/locales -l $(LOCALE)

i18n-update: i18n-extract ## merge new strings into existing catalogs
	$(PYBABEL) update -i app/locales/messages.pot -d app/locales

i18n-compile:
	$(PYBABEL) compile -d app/locales --use-fuzzy

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