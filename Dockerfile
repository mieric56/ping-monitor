FROM python:3.12-slim

# Install network tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    mtr-tiny \
    traceroute \
    dublin-traceroute \
    libcap2-bin \
  && rm -rf /var/lib/apt/lists/*

# Grant NET_RAW capability to tools so container runs without --privileged
RUN setcap cap_net_raw+ep /bin/ping && \
    setcap cap_net_raw+ep /usr/bin/mtr && \
    setcap cap_net_raw+ep /usr/bin/dublin-traceroute 2>/dev/null || true

WORKDIR /app

# Install Python dependencies
RUN pip install --no-cache-dir fastapi uvicorn httpx pydantic

# Copy app files
COPY api_server.py .
COPY index.html .

# Create the data directory for SQLite persistence
# Mount a host volume here (-v /host/path/data:/data) to survive rebuilds
RUN mkdir -p /data && chmod 777 /data

EXPOSE 8000

# Optional: override at runtime with -e TEAMS_WEBHOOK=https://...
ENV TEAMS_WEBHOOK=""
# Optional: override DB path (default /data/ping_monitor.db)
ENV DB_PATH="/data/ping_monitor.db"

CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
