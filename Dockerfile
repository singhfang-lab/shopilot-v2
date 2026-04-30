FROM python:3.12-slim

WORKDIR /app

# System deps for psycopg2 / pdfplumber / Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Uploads dir (Cloud Run: ephemeral; GCS mount or volume for persistence)
RUN mkdir -p /app/uploads /app/logs

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --workers 2"]
