# Use a lightweight Python image
FROM python:3.12-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docs ./docs

# Web process (default)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

# Worker process (override CMD):
#   docker run --rm -e CMD ...  or in compose:
#   command: ["python", "-m", "app.workers.worker"]
