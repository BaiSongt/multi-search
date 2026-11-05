FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv_binary

FROM scratch AS browser_artifacts
# 以固定摘要获取 openSUSE Build Service 的发布密钥和安全更新。
ADD --checksum=sha256:1619feab3ac09295afd1f63c6a1e528c700bf021c84c487cb3bb3589f465f45f https://api.opensuse.org/public/source/network:chromium/_pubkey /chromium.key
ADD --checksum=sha256:9a77e613919fc43b78376be81b0365a8ba487118a06c209e4ccacf1352a80e31 https://download.opensuse.org/repositories/network:/chromium/openSUSE_Tumbleweed/x86_64/chromium-157.0.8103.38-1.1.x86_64.rpm /chromium.rpm

FROM registry.opensuse.org/opensuse/tumbleweed@sha256:f98f1a114f2abf07740418c0f2844d93c22d890e4e2dd761c7a9a5c7de064764 AS base
RUN zypper --non-interactive refresh \
    && zypper --non-interactive install --no-recommends python312-base=3.12.15-2.1 \
    && zypper clean --all
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app

FROM base AS dependencies
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=uv_binary /uv /usr/local/bin/uv
COPY deploy/renderer/pyproject.toml deploy/renderer/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python /usr/bin/python3.12

FROM base
RUN --mount=type=bind,from=browser_artifacts,target=/artifacts \
    rpmkeys --import /artifacts/chromium.key \
    && rpmkeys --checksig /artifacts/chromium.rpm \
    && zypper --non-interactive refresh \
    && zypper --non-interactive install --no-recommends /artifacts/chromium.rpm \
    && zypper clean --all
ENV PATH="/app/.venv/bin:$PATH"
COPY --from=dependencies /app/.venv/ /app/.venv/
COPY deploy/renderer/app.py ./
USER 1000:1000
CMD ["python", "app.py"]
