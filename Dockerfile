# syntax=docker/dockerfile:1
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/root/.local/bin:/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh -s -- -y

COPY pyproject.toml uv.lock ./
COPY app ./app

RUN uv sync --frozen --no-dev

COPY README.md .

VOLUME ["/app/uploads"]

CMD ["uv", "run", "python", "-m", "app.bot"]
