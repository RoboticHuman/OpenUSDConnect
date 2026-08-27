# syntax=docker/dockerfile:1

ARG UV_VERSION=0.8.13
# USD_PROFILE selects core (usd-core), full (pinned MaterialX), or external.
ARG USD_PROFILE=full

# Named contexts override these empty stages only when external inputs are used:
# --build-context usd_runtime=/path/to/linux/openusd-prefix
# --build-context usd_plugins=/path/to/plugin-roots (one root per child directory)
FROM scratch AS usd_runtime
FROM scratch AS usd_plugins


FROM python:3.13-slim-trixie AS usd-build-base

ARG UV_VERSION
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential cmake git ninja-build \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install "uv==${UV_VERSION}"

# This Python image includes its matching headers and libpython. Trixie provides
# CMake >= 3.27.
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE NOTICE .python-version openusd.lock.json ./
COPY scripts/build_openusd.py scripts/package_usd_runtime.py scripts/
COPY packaging/server_launcher.py packaging/


# Service-only edits should not invalidate the source-built OpenUSD runtime.
FROM usd-build-base AS build-base

COPY CMakeLists.txt test_scene.usda ./
COPY openusdconnect/ openusdconnect/
COPY integrations/ integrations/
COPY native/ native/
COPY packaging/ packaging/
COPY scripts/ scripts/


FROM build-base AS wheel-builder

ARG USD_PROFILE

# Export each service's resolved dependencies, then build their shared wheelhouse.
# No dependency resolution is allowed during wheel creation or installation.
# Hashes are omitted because locally built wheels differ from their source archives.
RUN <<'SH'
set -eu
case "$USD_PROFILE" in
    core) set -- ;;
    full|external) set -- --no-emit-package usd-core ;;
    *) echo "USD_PROFILE must be core, full, or external" >&2; exit 1 ;;
esac
mkdir -p /requirements
for extra in runtime vfs complete mcp; do
    uv export --frozen --no-dev --extra "$extra" --no-emit-project --no-hashes \
        "$@" --format requirements-txt --output-file "/requirements/$extra.txt"
done
python -m pip wheel --no-deps --wheel-dir /wheels --requirement /requirements/complete.txt
python -m pip wheel --no-deps --wheel-dir /wheels .
SH


FROM usd-build-base AS usd-core

# The packaging helper does not install usd-core. Use the lockfile's USD-only group.
RUN uv export --frozen --only-group bundled-usd --no-emit-project \
        --format requirements-txt --output-file /tmp/usd-core.txt \
    && python -m pip install --no-deps --requirement /tmp/usd-core.txt \
    && python scripts/package_usd_runtime.py --usd-profile core --output-dir /opt/ouc \
    && python -I /opt/ouc/_launch.py --runtime-info


FROM usd-build-base AS usd-full

# MaterialX's CMake config requires X11 even with rendering disabled. These
# development files stay in this build stage, not the exported runtime.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libxt-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python scripts/package_usd_runtime.py --usd-profile full --output-dir /opt/ouc \
    && python -I /opt/ouc/_launch.py --runtime-info


FROM usd-build-base AS usd-external

# ALLOW_UNPINNED_USD is an explicit opt-in for an external runtime with another pin.
ARG ALLOW_UNPINNED_USD=0

RUN --mount=type=bind,from=usd_runtime,target=/usd-input \
    --mount=type=bind,from=usd_plugins,target=/usd-plugins <<'SH'
set -eu
set -- --usd-profile external --usd-root /usd-input --output-dir /opt/ouc
case "$ALLOW_UNPINNED_USD" in
    0) ;;
    1) set -- "$@" --allow-unpinned-usd ;;
    *) echo "ALLOW_UNPINNED_USD must be 0 or 1" >&2; exit 1 ;;
esac
for plugin in /usd-plugins/* /usd-plugins/.[!.]* /usd-plugins/..?*; do
    if [ -d "$plugin" ]; then
        set -- "$@" --usd-plugin-path "$plugin"
    fi
done
python scripts/package_usd_runtime.py "$@"
python -I /opt/ouc/_launch.py --runtime-info
SH


FROM usd-${USD_PROFILE} AS usd-builder


FROM python:3.13-slim-trixie AS runtime-base

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

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" openusdconnect \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home openusdconnect \
    && mkdir -p /data /scenes /work \
    && chown openusdconnect:openusdconnect /data /scenes /work

COPY --from=usd-builder /opt/ouc/ /opt/ouc/

STOPSIGNAL SIGTERM


FROM runtime-base AS server

RUN --mount=type=bind,from=wheel-builder,source=/wheels,target=/wheels \
    --mount=type=bind,from=wheel-builder,source=/requirements,target=/requirements \
    python -m pip install --no-deps --no-index --find-links=/wheels \
        --requirement /requirements/runtime.txt /wheels/openusdconnect-*.whl

USER openusdconnect
WORKDIR /scenes
RUN python -I /opt/ouc/_launch.py --runtime-info \
    && python -I /opt/ouc/_launch.py openusdconnect.server --help
EXPOSE 7200
VOLUME ["/data", "/scenes"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('OPENUSDCONNECT_HEALTH_PORT', '7200'))), 2).close()"
ENTRYPOINT ["python", "-I", "/opt/ouc/_launch.py", "openusdconnect.server"]
CMD ["--host", "0.0.0.0", "--port", "7200", "--event-log", "/data/events.db"]


FROM runtime-base AS live-open

RUN --mount=type=bind,from=wheel-builder,source=/wheels,target=/wheels \
    --mount=type=bind,from=wheel-builder,source=/requirements,target=/requirements \
    python -m pip install --no-deps --no-index --find-links=/wheels \
        --requirement /requirements/vfs.txt /wheels/openusdconnect-*.whl

USER openusdconnect
WORKDIR /scenes
RUN python -I /opt/ouc/_launch.py --runtime-info \
    && python -I /opt/ouc/_launch.py openusdconnect.server --help
EXPOSE 7200 7280
VOLUME ["/data", "/scenes"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('OPENUSDCONNECT_HEALTH_PORT', '7200'))), 2).close()"
ENTRYPOINT ["python", "-I", "/opt/ouc/_launch.py", "openusdconnect.server"]
CMD ["--host", "0.0.0.0", "--port", "7200", "--event-log", "/data/events.db", "--vfs-host", "0.0.0.0", "--vfs-port", "7280", "--advertise-host", "127.0.0.1"]


FROM runtime-base AS complete

RUN --mount=type=bind,from=wheel-builder,source=/wheels,target=/wheels \
    --mount=type=bind,from=wheel-builder,source=/requirements,target=/requirements \
    python -m pip install --no-deps --no-index --find-links=/wheels \
        --requirement /requirements/complete.txt /wheels/openusdconnect-*.whl

USER openusdconnect
WORKDIR /scenes
RUN python -I /opt/ouc/_launch.py --runtime-info \
    && python -I /opt/ouc/_launch.py openusdconnect.server --help \
    && python -I /opt/ouc/_launch.py integrations.mcp --help
EXPOSE 7200 7280 8080
VOLUME ["/data", "/scenes"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('OPENUSDCONNECT_HEALTH_PORT', '7200'))), 2).close()"
ENTRYPOINT ["python", "-I", "/opt/ouc/_launch.py", "openusdconnect.server"]
CMD ["--host", "0.0.0.0", "--port", "7200", "--event-log", "/data/events.db", "--vfs-host", "0.0.0.0", "--vfs-port", "7280", "--dashboard-port", "8080", "--advertise-host", "127.0.0.1"]


FROM runtime-base AS mcp

RUN --mount=type=bind,from=wheel-builder,source=/wheels,target=/wheels \
    --mount=type=bind,from=wheel-builder,source=/requirements,target=/requirements \
    python -m pip install --no-deps --no-index --find-links=/wheels \
        --requirement /requirements/mcp.txt /wheels/openusdconnect-*.whl

USER openusdconnect
WORKDIR /work
RUN python -I /opt/ouc/_launch.py --runtime-info \
    && python -I /opt/ouc/_launch.py integrations.mcp --help
ENTRYPOINT ["python", "-I", "/opt/ouc/_launch.py", "integrations.mcp"]


# Optional reproducible Linux artifact builder. Export with:
# docker build --target release-packages --output type=local,dest=dist/linux .
FROM build-base AS release-builder

ARG USD_PROFILE
ARG ALLOW_UNPINNED_USD=0
ARG OPENUSDCONNECT_BUILD_COMMIT=unknown

RUN --mount=type=bind,from=usd_runtime,target=/usd-input \
    --mount=type=bind,from=usd_plugins,target=/usd-plugins <<'SH'
set -eu
set -- --usd-profile "$USD_PROFILE"
case "$USD_PROFILE" in
    core|full) ;;
    external)
        set -- "$@" --usd-root /usd-input
        for plugin in /usd-plugins/* /usd-plugins/.[!.]* /usd-plugins/..?*; do
            if [ -d "$plugin" ]; then
                set -- "$@" --usd-plugin-path "$plugin"
            fi
        done
        ;;
    *) echo "USD_PROFILE must be core, full, or external" >&2; exit 1 ;;
esac
case "$ALLOW_UNPINNED_USD" in
    0) ;;
    1) set -- "$@" --allow-unpinned-usd ;;
    *) echo "ALLOW_UNPINNED_USD must be 0 or 1" >&2; exit 1 ;;
esac
OPENUSDCONNECT_BUILD_COMMIT="$OPENUSDCONNECT_BUILD_COMMIT" \
    python scripts/build_distribution.py "$@" \
        --component python --component server --component cpp-sdk \
        --generator Ninja --output-dir /release --clean-output
SH

FROM scratch AS release-packages
COPY --from=release-builder /release/ /


# A plain docker build produces the TCP server with the full headless USD runtime.
# Select the smaller runtime with --build-arg USD_PROFILE=core.
FROM server AS default
