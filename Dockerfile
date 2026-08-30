# BLACKBOX Cloud Run image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY blackbox ./blackbox

# Cloud Run supplies PORT. One worker keeps the in-process stub state coherent;
# scale with instances rather than workers.
ENV PORT=8080
CMD exec uvicorn blackbox.main:app --host 0.0.0.0 --port ${PORT} --workers 1
