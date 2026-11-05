FROM python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN pip install --no-cache-dir uv==0.7.6 && uv sync --locked --no-dev --no-install-project
ENV PATH="/app/backend/.venv/bin:$PATH"
COPY VERSION /app/VERSION
COPY backend/ ./
USER 1000:1000
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "multisearch.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]
