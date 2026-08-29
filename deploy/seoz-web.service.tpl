[Unit]
Description=Seoz Platform — Web process (FastAPI + HTMX)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=__USER__
WorkingDirectory=__ROOT__
# config is pydantic-settings: it reads .env from the working directory
ExecStart=__ROOT__/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target