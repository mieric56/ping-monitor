#!/usr/bin/env python3
"""
Ping Monitor API Server
FastAPI backend with WebSocket live feed, POI CRUD, Teams webhook alerts,
MTR and Traceroute with timestamped recording.
"""

import asyncio
import json
import time
import uuid
import os
import subprocess
import re
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ─── In-memory storage ──────────────────────────────────────────────────────

# POIs: {id: {"id", "name", "host", "interval", "teams_webhook"?}}
pois: dict[str, dict] = {}

# Live data per POI
live_data: dict[str, dict] = {}

# Ping history: {id: deque of probe records}
history: dict[str, deque] = {}

# Traceroute history: {id: deque of run records}
traceroute_history: dict[str, deque] = {}

# MTR history: {id: deque of run records}
mtr_history: dict[str, deque] = {}

# Dublin Traceroute history: {id: deque of run records}
dublin_history: dict[str, deque] = {}

# Running diagnostics lock: prevent duplicate runs
running_diag: dict[str, set] = {}   # {poi_id: {"traceroute"|"mtr"|"dublin"}}

# WebSocket connections
ws_clients: list[WebSocket] = []

# Background task handle
ping_task: Optional[asyncio.Task] = None

# Global settings
global_settings = {
    "teams_webhook": os.environ.get("TEAMS_WEBHOOK", ""),
}

MAX_PING_HISTORY    = 120  # last 120 ping probes per POI
MAX_DIAG_HISTORY    = 50   # last 50 traceroute / MTR runs per POI


# ─── Models ──────────────────────────────────────────────────────────────────

class POICreate(BaseModel):
    name: str
    host: str
    interval: int = 30
    count: int = 4
    teams_webhook: str = ""


class POIUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    interval: Optional[int] = None
    count: Optional[int] = None
    teams_webhook: Optional[str] = ""


class SettingsUpdate(BaseModel):
    teams_webhook: str = ""


# ─── Ping engine ─────────────────────────────────────────────────────────────

def ping_host(host: str, count: int = 4) -> dict:
    """Run system ping and parse results."""
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", "3", host],
            capture_output=True, text=True, timeout=count * 4 + 5
        )
        output = result.stdout

        loss_match = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
        loss_pct = float(loss_match.group(1)) if loss_match else 100.0

        rtt_match = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", output)
        if not rtt_match:
            rtt_match = re.search(r"round-trip min/avg/max/stddev = [\d.]+/([\d.]+)/", output)
        avg_rtt = float(rtt_match.group(1)) if rtt_match else None

        status = "down" if loss_pct == 100.0 else "up"
        return {"status": status, "latency_ms": avg_rtt, "loss_pct": loss_pct}

    except subprocess.TimeoutExpired:
        return {"status": "down", "latency_ms": None, "loss_pct": 100.0}
    except Exception:
        return {"status": "unknown", "latency_ms": None, "loss_pct": 100.0}


# ─── Traceroute engine ────────────────────────────────────────────────────────

def run_traceroute(host: str, max_hops: int = 30, timeout_s: int = 60) -> dict:
    """
    Run traceroute and return structured + raw output.
    Returns a dict with: ts, host, hops (list), raw, duration_s, error
    """
    ts_start = time.time()
    ts_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_start))

    try:
        result = subprocess.run(
            ["traceroute", "-m", str(max_hops), "-w", "3", "-q", "3", host],
            capture_output=True, text=True, timeout=timeout_s
        )
        raw = result.stdout.strip() or result.stderr.strip()
        duration_s = round(time.time() - ts_start, 2)

        hops = parse_traceroute(raw)

        return {
            "id": str(uuid.uuid4()),
            "ts": ts_start,
            "ts_iso": ts_iso,
            "host": host,
            "hops": hops,
            "hop_count": len(hops),
            "raw": raw,
            "duration_s": duration_s,
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {
            "id": str(uuid.uuid4()),
            "ts": ts_start,
            "ts_iso": ts_iso,
            "host": host,
            "hops": [],
            "hop_count": 0,
            "raw": f"traceroute timed out after {timeout_s}s",
            "duration_s": round(time.time() - ts_start, 2),
            "error": "timeout",
        }
    except Exception as e:
        return {
            "id": str(uuid.uuid4()),
            "ts": ts_start,
            "ts_iso": ts_iso,
            "host": host,
            "hops": [],
            "hop_count": 0,
            "raw": str(e),
            "duration_s": round(time.time() - ts_start, 2),
            "error": str(e),
        }


def parse_traceroute(raw: str) -> list:
    """Parse traceroute output into structured hop list."""
    hops = []
    # Each hop line: " 1  x.x.x.x (x.x.x.x)  1.234 ms  1.345 ms  1.456 ms"
    # or:           " 1  * * *"
    hop_re = re.compile(
        r"^\s*(\d+)\s+"          # hop number
        r"([\w\.\-\*]+)"         # hostname or *
        r"(?:\s+\([\d\.]+\))?"   # optional IP in parens
        r"(.*?)$",               # rest of line (RTTs or *)
        re.MULTILINE
    )
    rtt_re = re.compile(r"([\d.]+)\s*ms")

    for m in hop_re.finditer(raw):
        hop_num = int(m.group(1))
        host = m.group(2).strip()
        rest = m.group(3).strip()

        rtts = rtt_re.findall(rest)
        rtts_f = [float(r) for r in rtts]
        avg_rtt = round(sum(rtts_f) / len(rtts_f), 3) if rtts_f else None
        is_timeout = (host == "*" or not rtts_f)

        hops.append({
            "hop": hop_num,
            "host": host if not is_timeout else "*",
            "rtts_ms": rtts_f,
            "avg_ms": avg_rtt,
            "timeout": is_timeout,
        })

    return hops


# ─── Dublin Traceroute engine ────────────────────────────────────────────────

def run_dublin_traceroute(host: str, npaths: int = 10, max_ttl: int = 30, timeout_s: int = 90) -> dict:
    """
    Run dublin-traceroute and return structured multi-path results + raw JSON.
    Returns: {id, ts, ts_iso, host, npaths, flows, divergence_hops, raw_json, duration_s, error}
    """
    import tempfile
    ts_start = time.time()
    ts_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_start))

    with tempfile.TemporaryDirectory() as tmpdir:
        # dublin-traceroute always writes to trace.json in the cwd (no -o flag)
        out_json = os.path.join(tmpdir, "trace.json")
        try:
            result = subprocess.run(
                ["dublin-traceroute",
                 f"--npaths={npaths}",
                 "--min-ttl=1",
                 f"--max-ttl={max_ttl}",
                 host],
                capture_output=True, text=True,
                cwd=tmpdir,
                timeout=timeout_s
            )
            duration_s = round(time.time() - ts_start, 2)

            # dublin-traceroute writes trace.json in its cwd
            raw_json = {}
            if os.path.exists(out_json):
                with open(out_json) as f:
                    raw_json = json.load(f)
            else:
                # fallback: try parsing stdout as JSON
                try:
                    raw_json = json.loads(result.stdout)
                except Exception:
                    pass

            flows = parse_dublin_flows(raw_json.get("flows", {}))
            divergence_hops = find_divergence_hops(flows)

            return {
                "id":              str(uuid.uuid4()),
                "ts":              ts_start,
                "ts_iso":          ts_iso,
                "host":            host,
                "npaths":          npaths,
                "max_ttl":         max_ttl,
                "flows":           flows,
                "flow_count":      len(flows),
                "divergence_hops": divergence_hops,
                "raw_json":        raw_json,
                "duration_s":      duration_s,
                "error":           None,
            }

        except subprocess.TimeoutExpired:
            return {
                "id": str(uuid.uuid4()), "ts": ts_start, "ts_iso": ts_iso, "host": host,
                "npaths": npaths, "max_ttl": max_ttl, "flows": [], "flow_count": 0,
                "divergence_hops": [], "raw_json": {}, "duration_s": round(time.time() - ts_start, 2),
                "error": "timeout",
            }
        except Exception as e:
            return {
                "id": str(uuid.uuid4()), "ts": ts_start, "ts_iso": ts_iso, "host": host,
                "npaths": npaths, "max_ttl": max_ttl, "flows": [], "flow_count": 0,
                "divergence_hops": [], "raw_json": {}, "duration_s": round(time.time() - ts_start, 2),
                "error": str(e),
            }


def parse_dublin_flows(raw_flows: dict) -> list:
    """
    Convert dublin-traceroute JSON flows dict into a list of structured flow objects.
    Each flow: {flow_id, hops: [{ttl, name, rtt_ms, nat_id, is_last, nat_detected, flowhash}]}
    """
    parsed = []
    for flow_id, hops in raw_flows.items():
        structured_hops = []
        for i, hop in enumerate(hops):
            name = hop.get("name", "*")
            rtt_usec = hop.get("rtt_usec")
            rtt_ms = round(rtt_usec / 1000, 3) if rtt_usec else None
            nat_id = hop.get("nat_id", 0)
            # NAT detected = nat_id changed from previous hop
            prev_nat = hops[i - 1].get("nat_id", 0) if i > 0 else 0
            nat_detected = (nat_id != 0 and nat_id != prev_nat)
            sent = hop.get("sent", {})
            ttl = sent.get("ip", {}).get("ttl", i + 1)
            structured_hops.append({
                "ttl":          ttl,
                "name":         name,
                "rtt_ms":       rtt_ms,
                "nat_id":       nat_id,
                "nat_detected": nat_detected,
                "is_last":      hop.get("is_last", False),
                "flowhash":     hop.get("flowhash", 0),
                "icmp_desc":    (hop.get("received") or {}).get("icmp", {}).get("description", ""),
            })
        parsed.append({
            "flow_id":   int(flow_id),
            "hops":      structured_hops,
            "hop_count": len(structured_hops),
        })
    # Sort by flow_id for consistent ordering
    parsed.sort(key=lambda f: f["flow_id"])
    return parsed


def find_divergence_hops(flows: list) -> list:
    """
    Find TTL hops where flows diverge (different hosts across flows).
    Returns list of TTL numbers where path splits occur.
    """
    if len(flows) < 2:
        return []
    # Build {ttl: set(hosts)} across all flows
    ttl_hosts: dict[int, set] = {}
    for flow in flows:
        for hop in flow["hops"]:
            ttl = hop["ttl"]
            ttl_hosts.setdefault(ttl, set()).add(hop["name"])
    return sorted([ttl for ttl, hosts in ttl_hosts.items() if len(hosts) > 1])


# ─── MTR engine ───────────────────────────────────────────────────────────────

def run_mtr(host: str, cycles: int = 10, timeout_s: int = 90) -> dict:
    """
    Run mtr in report mode and return structured + raw output.
    Returns: ts, host, hops (list), raw, duration_s, error
    """
    ts_start = time.time()
    ts_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_start))

    try:
        # mtr --report: runs `cycles` pings per hop, then outputs table
        result = subprocess.run(
            ["mtr", "--report", "--report-cycles", str(cycles),
             "--no-dns",   # skip DNS for speed; add hostname from raw
             "-4", host],
            capture_output=True, text=True, timeout=timeout_s
        )
        raw = result.stdout.strip() or result.stderr.strip()
        duration_s = round(time.time() - ts_start, 2)

        hops = parse_mtr_report(raw)

        return {
            "id": str(uuid.uuid4()),
            "ts": ts_start,
            "ts_iso": ts_iso,
            "host": host,
            "cycles": cycles,
            "hops": hops,
            "hop_count": len(hops),
            "raw": raw,
            "duration_s": duration_s,
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {
            "id": str(uuid.uuid4()),
            "ts": ts_start,
            "ts_iso": ts_iso,
            "host": host,
            "cycles": cycles,
            "hops": [],
            "hop_count": 0,
            "raw": f"mtr timed out after {timeout_s}s",
            "duration_s": round(time.time() - ts_start, 2),
            "error": "timeout",
        }
    except Exception as e:
        return {
            "id": str(uuid.uuid4()),
            "ts": ts_start,
            "ts_iso": ts_iso,
            "host": host,
            "cycles": cycles,
            "hops": [],
            "hop_count": 0,
            "raw": str(e),
            "duration_s": round(time.time() - ts_start, 2),
            "error": str(e),
        }


def parse_mtr_report(raw: str) -> list:
    """
    Parse mtr --report output.
    Header line: HOST     Loss%   Snt   Last   Avg  Best  Wrst StDev
    Example row: "  1.|-- 10.0.0.1   0.0%    10    1.2   1.3  1.1  1.8  0.2"
    """
    hops = []
    # Match lines like: "  1.|-- hostname   0.0%  10  1.2  1.3  1.1  1.8  0.2"
    row_re = re.compile(
        r"^\s*(\d+)\.\|?-+\s+"           # hop number
        r"([\w\.\-\*]+)\s+"              # host / IP / *
        r"([\d.]+)%\s+"                  # loss %
        r"(\d+)\s+"                      # sent
        r"([\d.]+)\s+"                   # last
        r"([\d.]+)\s+"                   # avg
        r"([\d.]+)\s+"                   # best
        r"([\d.]+)\s+"                   # worst
        r"([\d.]+)",                     # stdev
        re.MULTILINE
    )

    for m in row_re.finditer(raw):
        hops.append({
            "hop":      int(m.group(1)),
            "host":     m.group(2),
            "loss_pct": float(m.group(3)),
            "sent":     int(m.group(4)),
            "last_ms":  float(m.group(5)),
            "avg_ms":   float(m.group(6)),
            "best_ms":  float(m.group(7)),
            "worst_ms": float(m.group(8)),
            "stdev_ms": float(m.group(9)),
        })

    return hops


# ─── Teams alert ─────────────────────────────────────────────────────────────

async def send_teams_alert(poi: dict, old_status: str, new_status: str, data: dict):
    webhook = poi.get("teams_webhook") or global_settings.get("teams_webhook", "")
    if not webhook:
        return

    color = "FF0000" if new_status == "down" else "00AA00"
    emoji = "🔴" if new_status == "down" else "🟢"
    title = f"{emoji} {poi['name']} — {new_status.upper()}"
    latency = f"{data['latency_ms']:.1f} ms" if data.get("latency_ms") is not None else "N/A"

    body = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": color,
        "summary": title,
        "sections": [{
            "activityTitle": title,
            "activitySubtitle": f"Host: **{poi['host']}**",
            "facts": [
                {"name": "Status", "value": new_status.upper()},
                {"name": "Previous", "value": old_status.upper()},
                {"name": "Latency", "value": latency},
                {"name": "Packet Loss", "value": f"{data['loss_pct']:.0f}%"},
                {"name": "Timestamp", "value": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())},
            ],
            "markdown": True
        }]
    }

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(webhook, json=body)
    except Exception:
        pass


# ─── Background ping loop ────────────────────────────────────────────────────

async def run_ping_loop():
    next_ping: dict[str, float] = {}
    while True:
        now = time.time()
        tasks = []
        for poi_id, poi in list(pois.items()):
            due = next_ping.get(poi_id, 0)
            if now >= due:
                tasks.append(probe_poi(poi_id, poi))
                next_ping[poi_id] = now + poi["interval"]
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(1)


async def probe_poi(poi_id: str, poi: dict):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, ping_host, poi["host"], poi.get("count", 4))

    old_status = live_data.get(poi_id, {}).get("status", "unknown")
    new_status = result["status"]

    record = {
        "ts": time.time(),
        "latency_ms": result["latency_ms"],
        "loss_pct": result["loss_pct"],
        "status": new_status,
    }

    live_data[poi_id] = {**record, "last_checked": record["ts"]}

    if poi_id not in history:
        history[poi_id] = deque(maxlen=MAX_PING_HISTORY)
    history[poi_id].append(record)

    if old_status != new_status and old_status != "unknown":
        await send_teams_alert(poi, old_status, new_status, result)

    msg = json.dumps({"type": "update", "poi_id": poi_id, "data": record})
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


# ─── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global ping_task
    ping_task = asyncio.create_task(run_ping_loop())
    yield
    if ping_task:
        ping_task.cancel()


app = FastAPI(title="Ping Monitor", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── POI REST routes ─────────────────────────────────────────────────────────

@app.get("/api/pois")
def list_pois():
    return [
        {**poi, "live": live_data.get(pid, {"status": "unknown"})}
        for pid, poi in pois.items()
    ]


@app.post("/api/pois", status_code=201)
def create_poi(body: POICreate):
    poi_id = str(uuid.uuid4())
    poi = {
        "id": poi_id,
        "name": body.name,
        "host": body.host,
        "interval": max(5, body.interval),
        "count": max(1, min(10, body.count)),
        "teams_webhook": body.teams_webhook,
        "created_at": time.time(),
    }
    pois[poi_id] = poi
    live_data[poi_id] = {"status": "unknown", "latency_ms": None, "loss_pct": 0, "last_checked": None}
    history[poi_id] = deque(maxlen=MAX_PING_HISTORY)
    traceroute_history[poi_id] = deque(maxlen=MAX_DIAG_HISTORY)
    mtr_history[poi_id] = deque(maxlen=MAX_DIAG_HISTORY)
    dublin_history[poi_id] = deque(maxlen=MAX_DIAG_HISTORY)
    running_diag[poi_id] = set()
    return poi


@app.put("/api/pois/{poi_id}")
def update_poi(poi_id: str, body: POIUpdate):
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")
    poi = pois[poi_id]
    if body.name is not None:
        poi["name"] = body.name
    if body.host is not None:
        poi["host"] = body.host
    if body.interval is not None:
        poi["interval"] = max(5, body.interval)
    if body.count is not None:
        poi["count"] = max(1, min(10, body.count))
    if body.teams_webhook is not None:
        poi["teams_webhook"] = body.teams_webhook
    return poi


@app.delete("/api/pois/{poi_id}")
def delete_poi(poi_id: str):
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")
    del pois[poi_id]
    live_data.pop(poi_id, None)
    history.pop(poi_id, None)
    traceroute_history.pop(poi_id, None)
    mtr_history.pop(poi_id, None)
    dublin_history.pop(poi_id, None)
    running_diag.pop(poi_id, None)
    return {"deleted": poi_id}


@app.get("/api/pois/{poi_id}/history")
def get_history(poi_id: str, limit: int = 60):
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")
    return list(history.get(poi_id, []))[-limit:]


@app.post("/api/pois/{poi_id}/ping-now")
async def ping_now(poi_id: str):
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")
    asyncio.create_task(probe_poi(poi_id, pois[poi_id]))
    return {"queued": True}


# ─── Traceroute routes ────────────────────────────────────────────────────────

@app.get("/api/pois/{poi_id}/traceroute")
def get_traceroute_history(poi_id: str, limit: int = 20):
    """Return list of past traceroute runs for this POI."""
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")
    return list(traceroute_history.get(poi_id, []))[-limit:]


@app.post("/api/pois/{poi_id}/traceroute")
async def run_traceroute_now(poi_id: str, max_hops: int = 30):
    """Trigger a traceroute run. Returns immediately; result pushed via WS."""
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")

    diag_set = running_diag.setdefault(poi_id, set())
    if "traceroute" in diag_set:
        raise HTTPException(status_code=409, detail="Traceroute already running for this POI")

    asyncio.create_task(_run_and_store_traceroute(poi_id, pois[poi_id]["host"], max_hops))
    return {"queued": True, "host": pois[poi_id]["host"]}


async def _run_and_store_traceroute(poi_id: str, host: str, max_hops: int = 30):
    diag_set = running_diag.setdefault(poi_id, set())
    diag_set.add("traceroute")

    # Notify clients: running
    await _broadcast({
        "type": "diag_start",
        "tool": "traceroute",
        "poi_id": poi_id,
    })

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_traceroute, host, max_hops)

    if poi_id not in traceroute_history:
        traceroute_history[poi_id] = deque(maxlen=MAX_DIAG_HISTORY)
    traceroute_history[poi_id].append(result)

    diag_set.discard("traceroute")

    await _broadcast({
        "type": "diag_result",
        "tool": "traceroute",
        "poi_id": poi_id,
        "result": result,
    })


# ─── MTR routes ──────────────────────────────────────────────────────────────

@app.get("/api/pois/{poi_id}/mtr")
def get_mtr_history(poi_id: str, limit: int = 20):
    """Return list of past MTR runs for this POI."""
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")
    return list(mtr_history.get(poi_id, []))[-limit:]


@app.post("/api/pois/{poi_id}/mtr")
async def run_mtr_now(poi_id: str, cycles: int = 10):
    """Trigger an MTR run. Returns immediately; result pushed via WS."""
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")

    diag_set = running_diag.setdefault(poi_id, set())
    if "mtr" in diag_set:
        raise HTTPException(status_code=409, detail="MTR already running for this POI")

    asyncio.create_task(_run_and_store_mtr(poi_id, pois[poi_id]["host"], cycles))
    return {"queued": True, "host": pois[poi_id]["host"]}


async def _run_and_store_mtr(poi_id: str, host: str, cycles: int = 10):
    diag_set = running_diag.setdefault(poi_id, set())
    diag_set.add("mtr")

    await _broadcast({
        "type": "diag_start",
        "tool": "mtr",
        "poi_id": poi_id,
    })

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_mtr, host, cycles)

    if poi_id not in mtr_history:
        mtr_history[poi_id] = deque(maxlen=MAX_DIAG_HISTORY)
    mtr_history[poi_id].append(result)

    diag_set.discard("mtr")

    await _broadcast({
        "type": "diag_result",
        "tool": "mtr",
        "poi_id": poi_id,
        "result": result,
    })


# ─── Dublin Traceroute routes ───────────────────────────────────────────────

@app.get("/api/pois/{poi_id}/dublin")
def get_dublin_history(poi_id: str, limit: int = 20):
    """Return list of past Dublin Traceroute runs for this POI."""
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")
    return list(dublin_history.get(poi_id, []))[-limit:]


@app.post("/api/pois/{poi_id}/dublin")
async def run_dublin_now(poi_id: str, npaths: int = 10, max_ttl: int = 30):
    """Trigger a Dublin Traceroute run. Returns immediately; result pushed via WS."""
    if poi_id not in pois:
        raise HTTPException(status_code=404, detail="POI not found")

    diag_set = running_diag.setdefault(poi_id, set())
    if "dublin" in diag_set:
        raise HTTPException(status_code=409, detail="Dublin Traceroute already running for this POI")

    asyncio.create_task(_run_and_store_dublin(poi_id, pois[poi_id]["host"], npaths, max_ttl))
    return {"queued": True, "host": pois[poi_id]["host"]}


async def _run_and_store_dublin(poi_id: str, host: str, npaths: int = 10, max_ttl: int = 30):
    diag_set = running_diag.setdefault(poi_id, set())
    diag_set.add("dublin")

    await _broadcast({
        "type": "diag_start",
        "tool": "dublin",
        "poi_id": poi_id,
    })

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_dublin_traceroute, host, npaths, max_ttl)

    if poi_id not in dublin_history:
        dublin_history[poi_id] = deque(maxlen=MAX_DIAG_HISTORY)
    dublin_history[poi_id].append(result)

    diag_set.discard("dublin")

    await _broadcast({
        "type": "diag_result",
        "tool": "dublin",
        "poi_id": poi_id,
        "result": result,
    })


# ─── Settings routes ─────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    return {"teams_webhook": global_settings.get("teams_webhook", "")}


@app.put("/api/settings")
def update_settings(body: SettingsUpdate):
    global_settings["teams_webhook"] = body.teams_webhook
    return global_settings


# ─── WebSocket ───────────────────────────────────────────────────────────────

async def _broadcast(msg: dict):
    """Send a message to all connected WebSocket clients."""
    text = json.dumps(msg)
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)

    # Full state on connect
    state = {
        "type": "init",
        "pois": {
            pid: {
                "poi": poi,
                "live": live_data.get(pid, {"status": "unknown"}),
                "history": list(history.get(pid, [])),
                "traceroute_history": list(traceroute_history.get(pid, [])),
                "mtr_history": list(mtr_history.get(pid, [])),
                "dublin_history": list(dublin_history.get(pid, [])),
            }
            for pid, poi in pois.items()
        }
    }
    await ws.send_text(json.dumps(state))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in ws_clients:
            ws_clients.remove(ws)


# ─── Serve frontend ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text(), status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
