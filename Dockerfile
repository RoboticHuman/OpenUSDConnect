# syntax=docker/dockerfile:1

ARG UV_VERSION=0.8.13

FROM python:3.13-slim AS build-base

ARG UV_VERSION
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential cmake ninja-build \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install "uv==${UV_VERSION}"

WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE NOTICE .python-version openusd.lock.json CMakeLists.txt test_scene.usda ./
COPY openusdconnect/ openusdconnect/
COPY integrations/ integrations/
COPY native/ native/
COPY packaging/ packaging/
COPY scripts/ scripts/


FROM build-base AS wheel-builder

# One cached wheelhouse supports every runtime profile. BuildKit mounts it into
# later stages without copying the complete dependency set into each image.
RUN uv export --frozen --no-dev --extra complete --no-emit-project \
        --format requirements-txt --output-file /tmp/requirements.txt \
    && python -m pip wheel --wheel-dir /wheels --requirement /tmp/requirements.txt \
    && python -m pip wheel --wheel-dir /wheels --no-deps .


FROM python:3.13-slim AS runtime-base

ARG APP_UID=10001
ARG APP_GID=10001

LABEL org.opencontainers.image.title="OpenUSDConnect" \
      org.opencontainers.image.source="https://github.com/RoboticHuman/OpenUSDConnect" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPENUSDCONNECT_HEALTH_PORT=7200

RUN groupadd --gid "${APP_GID}" openusdconnect \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home openusdconnect \
    && mkdir -p /data /scenes /work \
    && chown openusdconnect:openusdconnect /data /scenes /work

STOPSIGNAL SIGTERM


FROM runtime-base AS server

RUN --mount=type=bind,from=wheel-builder,source=/wheels,target=/wheels \
    python -m pip install --no-index --find-links=/wheels "openusdconnect[runtime]"

USER openusdconnect
WORKDIR /scenes
EXPOSE 7200
VOLUME ["/data", "/scenes"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('OPENUSDCONNECT_HEALTH_PORT', '7200'))), 2).close()"
ENTRYPOINT ["openusdconnect-server"]
CMD ["--host", "0.0.0.0", "--port", "7200", "--event-log", "/data/events.db"]


FROM runtime-base AS live-open

RUN --mount=type=bind,from=wheel-builder,source=/wheels,target=/wheels \
    python -m pip install --no-index --find-links=/wheels "openusdconnect[vfs]"

USER openusdconnect
WORKDIR /scenes
EXPOSE 7200 7280
VOLUME ["/data", "/scenes"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('OPENUSDCONNECT_HEALTH_PORT', '7200'))), 2).close()"
ENTRYPOINT ["openusdconnect-server"]
CMD ["--host", "0.0.0.0", "--port", "7200", "--event-log", "/data/events.db", "--vfs-host", "0.0.0.0", "--vfs-port", "7280", "--advertise-host", "127.0.0.1"]


FROM runtime-base AS complete

RUN --mount=type=bind,from=wheel-builder,source=/wheels,target=/wheels \
    python -m pip install --no-index --find-links=/wheels "openusdconnect[complete]"

USER openusdconnect
WORKDIR /scenes
EXPOSE 7200 7280 8080
VOLUME ["/data", "/scenes"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('OPENUSDCONNECT_HEALTH_PORT', '7200'))), 2).close()"
ENTRYPOINT ["openusdconnect-server"]
CMD ["--host", "0.0.0.0", "--port", "7200", "--event-log", "/data/events.db", "--vfs-host", "0.0.0.0", "--vfs-port", "7280", "--dashboard-port", "8080", "--advertise-host", "127.0.0.1"]


FROM runtime-base AS mcp

RUN --mount=type=bind,from=wheel-builder,source=/wheels,target=/wheels \
    python -m pip install --no-index --find-links=/wheels "openusdconnect[mcp]"

USER openusdconnect
WORKDIR /work
ENTRYPOINT ["openusdconnect-mcp"]


# Optional reproducible Linux artifact builder. Export with:
# docker build --target release-packages --output type=local,dest=dist/linux .
FROM build-base AS release-builder

ARG OPENUSDCONNECT_BUILD_COMMIT=unknown
RUN OPENUSDCONNECT_BUILD_COMMIT="${OPENUSDCONNECT_BUILD_COMMIT}" \
       python scripts/build_distribution.py \
         --component python \
         --component server \
         --component cpp-sdk \
         --generator Ninja \
         --output-dir /release \
         --clean-output

FROM scratch AS release-packages
COPY --from=release-builder /release/ /


# A plain docker build intentionally produces the minimal TCP server.
FROM server AS default
