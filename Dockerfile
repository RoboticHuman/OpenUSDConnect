# OpenUSDConnect Server
#
# Build:
#   docker build -t openusdconnect-server .
#
# Build with dashboard support:
#   docker build --build-arg DASHBOARD=1 -t openusdconnect-server:dashboard .
#
# Run:
#   docker run -p 7200:7200 -p 7280:7280 openusdconnect-server
#   docker run -p 7200:7200 -p 7280:7280 -p 8080:8080 openusdconnect-server:dashboard \
#     --port 7200 --base /scenes/scene.usda --vfs-port 7280 --dashboard-port 8080

FROM python:3.13-slim AS base

ARG DASHBOARD=0

WORKDIR /app

# Install the package, bundled OpenUSD runtime, and VFS dependencies.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY openusdconnect/ openusdconnect/
COPY native/sdf_notice_bridge/ native/sdf_notice_bridge/

RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir usd-core==26.8 wsgidav==4.3.3 cheroot==10.0.1

# Conditionally install dashboard dependencies and copy integration code
COPY integrations/dashboard/ integrations/dashboard/
RUN if [ "$DASHBOARD" = "1" ]; then \
        pip install --no-cache-dir nicegui==3.9.0; \
    fi

RUN mkdir -p /data /scenes

EXPOSE 7200 7280 8080
VOLUME ["/data", "/scenes"]

ENTRYPOINT ["openusdconnect-server"]
CMD ["--host", "0.0.0.0", "--port", "7200", "--event-log", "/data/usd_events.db", "--vfs-host", "0.0.0.0", "--vfs-port", "7280", "--advertise-host", "127.0.0.1"]
