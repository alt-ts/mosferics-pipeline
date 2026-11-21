# The pipeline: Python 3.12 with the project's pinned requirements.
# psycopg2-binary carries its own libpq, so the slim image needs nothing extra.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user; reports are written to /app/out (a mounted volume).
RUN useradd --create-home --uid 1000 pipeline && mkdir -p /app/out && chown pipeline /app/out
USER pipeline

CMD ["bash", "docker/test.sh"]
