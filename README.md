# Ping Monitor

Real-time network monitoring dashboard with Ping, Traceroute, MTR, and Dublin Traceroute (ECMP multi-path).  
All data is persisted to a SQLite database so your POIs and diagnostic history survive container restarts and rebuilds.

---

## Quick Start (Docker)

```bash
# 1. Unzip and enter folder
unzip ping-monitor.zip
cd ping-monitor

# 2. Build the image
docker build -t ping-monitor .

# 3. Run with a persistent data volume
docker run -d \
  --name ping-monitor \
  --cap-add NET_RAW \
  -p 8000:8000 \
  -v $(pwd)/data:/data \
  --restart unless-stopped \
  ping-monitor
```

Dashboard is live at **http://localhost:8000**

The `./data/` folder on your host will contain `ping_monitor.db`.  
Stop, rebuild, re-run — your POIs and history are still there.

---

## Docker Commands

```bash
# View live logs
docker logs -f ping-monitor

# Stop the container
docker stop ping-monitor

# Remove the container (data volume is untouched)
docker rm ping-monitor

# Rebuild after a code change and restart (keeps existing data)
docker build -t ping-monitor .
docker rm -f ping-monitor
docker run -d \
  --name ping-monitor \
  --cap-add NET_RAW \
  -p 8000:8000 \
  -v $(pwd)/data:/data \
  --restart unless-stopped \
  ping-monitor

# Run with a global Teams webhook
docker run -d \
  --name ping-monitor \
  --cap-add NET_RAW \
  -p 8000:8000 \
  -v $(pwd)/data:/data \
  -e TEAMS_WEBHOOK="https://your-webhook-url" \
  --restart unless-stopped \
  ping-monitor
```

---

## SQLite Database

| Detail         | Value                                      |
|----------------|--------------------------------------------|
| Default path   | `/data/ping_monitor.db` (inside container) |
| Host path      | `./data/ping_monitor.db` (via volume mount) |
| Override path  | `-e DB_PATH=/custom/path/monitor.db`       |

### What is persisted

| Table                | Contents                                      |
|----------------------|-----------------------------------------------|
| `pois`               | All configured POIs and their settings        |
| `ping_history`       | Last 120 ping probe records per POI           |
| `traceroute_history` | Last 50 traceroute runs per POI (hops + raw)  |
| `mtr_history`        | Last 50 MTR runs per POI (hop stats + raw)    |
| `dublin_history`     | Last 50 Dublin Traceroute runs per POI        |
| `settings`           | Global Teams webhook                          |

### Check DB health

```
GET /api/db/info
```

Returns DB path, file size, and row counts for every table.

---

## Why `--cap-add NET_RAW`?

Required for ICMP-based tools (ping, mtr, traceroute, dublin-traceroute) to work inside a container.  
**Do NOT use `--privileged`** — `NET_RAW` is the minimal capability needed.

---

## Environment Variables

| Variable        | Default                        | Description                             |
|-----------------|--------------------------------|-----------------------------------------|
| `TEAMS_WEBHOOK` | (empty)                        | Global Teams webhook for all POI alerts |
| `DB_PATH`       | `/data/ping_monitor.db`        | SQLite database file path               |

Teams webhook can also be set per-POI individually via the UI.

---

## Features

| Tool               | Tab        | What it shows                                                  |
|--------------------|------------|----------------------------------------------------------------|
| Ping               | Ping       | RTT, packet loss, uptime %, live charts, probe history table   |
| Traceroute         | Traceroute | Per-hop RTTs, avg RTT, timestamped runs, collapsible           |
| MTR                | MTR        | Loss%, Snt, Last/Avg/Best/Worst/StDev per hop, timestamped     |
| Dublin Traceroute  | Dublin     | ECMP multi-path matrix, divergence TTL detection, NAT tagging  |

---

## API Endpoints

```
GET    /api/pois                                List all POIs
POST   /api/pois                                Create POI {name, host, interval, count, teams_webhook}
PUT    /api/pois/{id}                           Update POI
DELETE /api/pois/{id}                           Delete POI
GET    /api/pois/{id}/history                   Ping probe history
POST   /api/pois/{id}/ping-now                  Trigger immediate ping
GET    /api/pois/{id}/traceroute                Traceroute run history
POST   /api/pois/{id}/traceroute?max_hops=30    Trigger traceroute
GET    /api/pois/{id}/mtr                       MTR run history
POST   /api/pois/{id}/mtr?cycles=10             Trigger MTR
GET    /api/pois/{id}/dublin                    Dublin Traceroute history
POST   /api/pois/{id}/dublin?npaths=10&max_ttl=30  Trigger Dublin Traceroute
GET    /api/settings                            Global settings
PUT    /api/settings                            Update global Teams webhook
GET    /api/db/info                             Database health and row counts
WS     /ws                                      WebSocket live feed
GET    /                                        Dashboard UI
```

---

## Manual Install (No Docker)

```bash
# Ubuntu/Debian
sudo apt install iputils-ping mtr-tiny traceroute dublin-traceroute

pip install fastapi uvicorn httpx pydantic

# DB will be created at /data/ping_monitor.db by default
# Override location if /data doesn't exist locally:
export DB_PATH="$HOME/ping_monitor.db"

python api_server.py   # serves on :8000
```
