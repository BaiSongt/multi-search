# 保留 Patchright 的 manylinux 轮子所需的 glibc。
FROM cgr.dev/chainguard/wolfi-base@sha256:771146c2dfc880902a185f060c3db5571473c46b267edd98c3119c83068dfb61 AS base
RUN apk add --no-cache python-3.12-base=3.12.16_git20261030-r0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app

FROM base AS dependencies
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY deploy/crawl4ai/pyproject.toml deploy/crawl4ai/uv.lock ./
RUN apk add --no-cache uv=0.13.4-r1 && uv sync --locked --no-dev --no-install-project --python /usr/bin/python3.12

FROM base
ENV PATH="/app/.venv/bin:$PATH"
COPY --from=dependencies /app/.venv/ /app/.venv/
COPY deploy/crawl4ai/app.py deploy/crawl4ai/parse_html.py ./
USER 1000:1000
EXPOSE 11235
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "11235", "--no-access-log", "--limit-concurrency", "8"]
