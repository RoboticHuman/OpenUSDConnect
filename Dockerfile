# syntax=docker/dockerfile:1

FROM python:3.13-slim AS wheel-builder

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential cmake ninja-build \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY pyproject.toml README.md LICENSE NOTICE .python-version openusd.lock.json CMakeLists.txt ./
COPY openusdconnect/ openusdconnect/
COPY integrations/__init__.py integrations/renderman.py integrations/
COPY integrations/dashboard/ integrations/dashboard/
COPY integrations/mcp/ integrations/mcp/
COPY integrations/openpbr_translate.py integrations/openpbr_translate.py
COPY integrations/usdview/ integrations/usdview/
COPY native/client_core/ native/client_core/
COPY native/python/ native/python/
COPY native/sdf_notice_bridge/ native/sdf_notice_bridge/

# Build the project and every dependency as wheels so the runtime stage never
# needs a compiler or network access.
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels ".[complete]"


FROM python:3.13-slim AS runtime

LABEL org.opencontainers.image.title="OpenUSDConnect Server" \
      org.opencontainers.image.source="https://github.com/RoboticHuman/OpenUSDConnect"

COPY --from=wheel-builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
        "openusdconnect[complete]" \
    && rm -rf /wheels

RUN useradd --create-home --uid 10001 openusdconnect \
    && mkdir -p /data /scenes \
    && chown openusdconnect:openusdconnect /data /scenes

USER openusdconnect
WORKDIR /scenes

EXPOSE 7200 7280 8080
VOLUME ["/data", "/scenes"]
HEALTHCHECK --interval=2s --timeout=2s --start-period=10s --retries=15 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=1).read()"

ENTRYPOINT ["openusdconnect-server"]
CMD ["--host", "0.0.0.0", "--port", "7200", "--event-log", "/data/usd_events.db", "--vfs-host", "0.0.0.0", "--vfs-port", "7280", "--dashboard-port", "8080", "--advertise-host", "127.0.0.1"]
