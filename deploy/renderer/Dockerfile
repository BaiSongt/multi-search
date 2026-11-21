FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv_binary

FROM registry.opensuse.org/opensuse/tumbleweed@sha256:ffa72fac2df2712333bfbc73650389e3627ad9a0b9f693cfa0cccc34a1a78226 AS base
RUN zypper --non-interactive refresh \
    && zypper --non-interactive install --from openSUSE-Tumbleweed-Oss --no-recommends python312-base=3.12.16-1.1 \
    && zypper clean --all
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app

FROM base AS dependencies
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=uv_binary /uv /usr/local/bin/uv
COPY deploy/renderer/pyproject.toml deploy/renderer/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python /usr/bin/python3.12

FROM base
# Resolve the exact Chromium build through the distribution's signed repository metadata.
RUN zypper --non-interactive refresh \
    && zypper --non-interactive install --from openSUSE-Tumbleweed-Oss --no-recommends chromium=157.0.8103.78-1.1 \
    && zypper clean --all
ENV PATH="/app/.venv/bin:$PATH"
COPY --from=dependencies /app/.venv/ /app/.venv/
COPY deploy/renderer/app.py ./
USER 1000:1000
CMD ["python", "app.py"]
