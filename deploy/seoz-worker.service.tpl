[Unit]
Description=Seoz Platform — Worker process (job engine + scheduler)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=__USER__
WorkingDirectory=__ROOT__
# config is pydantic-settings: it reads .env from the working directory
ExecStart=__ROOT__/.venv/bin/python -m app.workers.worker
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target