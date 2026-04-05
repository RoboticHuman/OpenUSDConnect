# OpenUSDConnect Server
#
# Build:
#   docker build -t openusdconnect-server .
#
# Build with dashboard support:
#   docker build --build-arg DASHBOARD=1 -t openusdconnect-server:dashboard .
#
# Run:
#   docker run -p 7200:7200 openusdconnect-server
#   docker run -p 7200:7200 -p 8080:8080 openusdconnect-server:dashboard \
#     --port 7200 --base /scenes/scene.usda --dashboard 8080

FROM python:3.13-slim AS base

ARG DASHBOARD=0

WORKDIR /app

# Install core package and server dependencies
COPY pyproject.toml .
COPY openusdconnect/ openusdconnect/

RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir usd-core==26.3

# Conditionally install dashboard dependencies and copy integration code
COPY integrations/dashboard/ integrations/dashboard/
RUN if [ "$DASHBOARD" = "1" ]; then \
        pip install --no-cache-dir nicegui==3.9.0; \
    fi

RUN mkdir -p /data /scenes

EXPOSE 7200 8080
VOLUME ["/data", "/scenes"]

ENTRYPOINT ["openusdconnect-server"]
CMD ["--host", "0.0.0.0", "--port", "7200", "--log", "/data/usd_events.db"]
