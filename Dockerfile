FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY scripts/phase9_migrate.py ./scripts/phase9_migrate.py
COPY config ./config
RUN pip install --no-cache-dir --timeout=120 --retries=5 .

USER nobody

CMD ["python", "-m", "quant_phase1.entrypoints.engine"]
