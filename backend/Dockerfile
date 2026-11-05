FROM python:3.12-alpine3.24@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/backend

FROM base AS dependencies
ENV UV_LINK_MODE=copy
COPY backend/pyproject.toml backend/uv.lock ./
RUN pip install --no-cache-dir uv==0.13.4 && uv sync --locked --no-dev --no-install-project

FROM base
ENV PATH="/app/backend/.venv/bin:$PATH"
COPY --from=dependencies /app/backend/.venv/ /app/backend/.venv/
COPY VERSION /app/VERSION
COPY backend/ ./
USER 1000:1000
EXPOSE 8765
CMD ["sh", "-c", "alembic upgrade head && exec python -m uvicorn multisearch.main:create_app --factory --host 0.0.0.0 --port 8765 --no-access-log"]
