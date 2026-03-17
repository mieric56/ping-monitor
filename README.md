# Ping Monitor

Real-time network monitoring dashboard with Ping, Traceroute, MTR, and Dublin Traceroute (ECMP multi-path).

## Quick Start (Docker — Recommended)

```bash
# 1. Unzip and enter folder
unzip ping-monitor.zip
cd ping-monitor

# 2. Build and start (single command)
docker-compose up -d --build

# 3. Open dashboard
open http://localhost:8000
```

Dashboard is live at **http://localhost:8000**

---

## Docker Commands

```bash
# Start in background (build if needed)
docker-compose up -d --build

# View live logs
docker-compose logs -f

# Stop
docker-compose down

# Rebuild after any code change
docker-compose up -d --build

# Run with a global Teams webhook
TEAMS_WEBHOOK="https://your-webhook-url" docker-compose up -d --build
```

---

## Why cap_add: NET_RAW?

The `docker-compose.yml` includes:
```yaml
cap_add:
  - NET_RAW
```
This is required for ICMP-based tools (ping, mtr, traceroute, dublin-traceroute) to work inside a container.  
**Do NOT use `--privileged`** — `NET_RAW` is the minimal capability needed.

---

## Environment Variables

| Variable        | Default | Description                                  |
|-----------------|---------|----------------------------------------------|
| `TEAMS_WEBHOOK` | (empty) | Global Teams webhook for all POI alerts      |

Can also be set per-POI individually via the UI.

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
GET  /api/pois                              List all POIs
POST /api/pois                              Create POI {name, host, interval, count, teams_webhook}
PUT  /api/pois/{id}                         Update POI
DELETE /api/pois/{id}                       Delete POI
GET  /api/pois/{id}/history                 Ping probe history
POST /api/pois/{id}/ping-now                Trigger immediate ping
GET  /api/pois/{id}/traceroute              Traceroute run history
POST /api/pois/{id}/traceroute?max_hops=30  Trigger traceroute
GET  /api/pois/{id}/mtr                     MTR run history
POST /api/pois/{id}/mtr?cycles=10           Trigger MTR
GET  /api/pois/{id}/dublin                  Dublin Traceroute history
POST /api/pois/{id}/dublin?npaths=10&max_ttl=30  Trigger Dublin Traceroute
GET  /api/settings                          Global settings
PUT  /api/settings                          Update global Teams webhook
WS   /ws                                    WebSocket live feed
GET  /                                      Dashboard UI
```

---

## Manual Install (No Docker)

```bash
# Ubuntu/Debian
sudo apt install iputils-ping mtr-tiny traceroute dublin-traceroute
pip install fastapi uvicorn httpx pydantic

python api_server.py   # serves on :8000
```
