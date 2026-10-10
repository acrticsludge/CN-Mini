# Distributed Backup System — Master Runbook

**Project #24 · Jackfruit Mini Project · 40 Marks**
**Protocol:** TCP · **Emulation:** Mininet · **SDN:** Ryu · **Frontend:** Flask

---

## 1. What This Project Does

A client sends a file to a backup server over TCP. The file is split into chunks, each chunk is checksummed, and the transfer can resume after interruption. An SDN controller (Ryu) sits below the network, watching for link failures and rerouting traffic automatically. A web dashboard visualizes everything live.

**One sentence:** TCP file transfer with chunking, checksums, resume, and SDN-based failure recovery — visualized on a web dashboard.

---

## 2. Tech Stack

| Layer             | Technology                  | Version  | Role                     |
| ----------------- | --------------------------- | -------- | ------------------------ |
| Language          | Python                      | 3.8+     | Everything               |
| Network emulation | Mininet                     | 2.3+     | Virtual switches + hosts |
| SDN controller    | Ryu                         | 4.34+    | Dynamic flow management  |
| Switch dataplane  | Open vSwitch (OVS)          | 2.13+    | OpenFlow 1.3 switches    |
| Web backend       | Flask                       | 2.0+     | REST API + WebSocket     |
| Web frontend | Vanilla JS + SVG + Chart.js | — | Live dashboard |
| Checksum | zlib.crc32 | Built-in | Per-chunk integrity |
| Persistence | bind-mount (host dir → Mininet) | — | Files survive restarts |
| Testing | pytest | 7.0+ | Unit + integration tests |

**No external dependencies beyond `pip install`.** No Docker, no cloud, no paid services.

---

## 3. How It Runs

### 3.1 Startup Sequence

```bash
# Terminal 1 — Start Mininet topology
sudo python mininet/topology.py

# Terminal 2 — Start Ryu SDN controller
ryu-manager controller/sdn_controller.py

# Terminal 3 — Start Flask dashboard
python frontend/app.py

# Browser — Open dashboard
http://localhost:5000
```

### 3.2 What Each Process Does

| Process             | Runs on                | What it does                                                               |
| ------------------- | ---------------------- | -------------------------------------------------------------------------- |
| `topology.py`       | Ubuntu VM              | Creates 4 OVS switches + 4 hosts in Mininet, wires them in a partial mesh  |
| `sdn_controller.py` | Ubuntu VM              | Connects to OVS via OpenFlow, installs flow rules, watches for link events |
| `app.py`            | Ubuntu VM (host)       | Serves the dashboard, exposes REST API, talks to Mininet + Ryu             |
| `client.py`         | Inside Mininet (h1/h2) | Reads a file, chunks it, sends over TCP with checksums                     |
| `server.py`         | Inside Mininet (h3/h4) | Receives chunks, verifies CRC32, reassembles the file                      |

### 3.3 Data Flow

```
Browser ──HTTP──► Flask app ──Mininet API──► Mininet network
                     │
                     ├──REST──► Ryu controller (flow stats, topology)
                     │
                     └──subprocess──► client.py / server.py (inside Mininet hosts)
```

---

## 4. Where the Principles Are Used

### 4.1 TCP Socket Programming

- **Where:** `client/client.py`, `server/server.py`
- **What:** Raw TCP sockets (`socket.AF_INET, socket.SOCK_STREAM`), not HTTP, not UDP
- **Why it matters:** This is the core requirement — low-level socket communication with custom protocol design

### 4.2 File Chunking

- **Where:** `client/client.py` (sender), `server/server.py` (receiver)
- **What:** File split into 8 KB chunks, each sent as a separate frame
- **Why it matters:** Enables granular recovery — only lost chunks are re-sent, not the whole file

### 4.3 Checksums (CRC32)

- **Where:** `common/checksum.py`, used by both client and server
- **What:** `zlib.crc32(payload)` computed per chunk, verified on receipt
- **Why it matters:** Detects bit corruption in the emulated network — TCP alone doesn't guarantee integrity in Mininet

### 4.4 Interrupted-Transfer Recovery

- **Where:** `client/client.py` (RESUME message), `server/server.py` (checkpoint tracking)
- **What:** Server tracks last ACKed chunk index. Client reconnects with RESUME, server confirms, transfer continues from `last_ack + 1`
- **Why it matters:** True resume — no data re-sent, no restart from scratch

### 4.5 Multiple Backup Servers

- **Where:** `client/client.py` (server list + failover logic)
- **What:** Client configured with `[(h3_ip, port), (h4_ip, port)]`. Tries primary, fails over to backup on connection failure
- **Why it matters:** Redundancy — if one server is unreachable, the other takes over

### 4.6 SDN Dynamic Path Selection

- **Where:** `controller/sdn_controller.py`
- **What:** Ryu app that receives `OFPPortStatus` events, runs Dijkstra on the topology graph, installs new flow rules via `OFPFlowMod`
- **Why it matters:** The network adapts without the application knowing — this is the key SDN value proposition

### 4.7 Link Failure Simulation

- **Where:** Mininet CLI (`link s1 s2 down`) or dashboard button
- **What:** Mininet tears down a virtual link → OVS sends `OFPPortStatus` to Ryu → Ryu recomputes path → new flows installed
- **Why it matters:** Demonstrates the "recovery during failure" requirement in a visible, measurable way

### 4.8 Web Dashboard (Frontend)

- **Where:** `frontend/app.py`, `frontend/templates/dashboard.html`, `frontend/static/`
- **What:** Flask serves a single-page app. REST API for commands, WebSocket for live updates. SVG topology, Chart.js metrics, event log, Stored Files panel
- **Why it matters:** Teacher sees everything visually — no terminal staring required

### 4.9 File Persistence

- **Where:** `stored_files/` (host), `mininet/topology.py` (bind-mount), `frontend/app.py` (`/api/files`)
- **What:** Host directory bind-mounted into Mininet server hosts at `/tmp/backup/`. Files written by servers persist on the host disk across Mininet restarts. Flask lists files directly from the host directory.
- **Why it matters:** Files survive start/stop cycles — the system feels like a real backup service, not a one-time simulation

---

## 5. Architecture at a Glance

```
┌─────────────────────────────────────────────────────────┐
│                    Ubuntu VM (one machine)               │
│                                                          │
│  ┌─────────────┐                                        │
│  │   Browser   │  http://localhost:5000                 │
│  └──────┬──────┘                                        │
│         │ HTTP + WebSocket                               │
│  ┌──────┴──────┐                                        │
│  │ Flask app   │  frontend/app.py                       │
│  │ (host)      │  REST API + Mininet API + Ryu REST     │
│  │             │  /api/files → reads stored_files/       │
│  └──────┬──────┘                                        │
│         │                                               │
│  ┌──────┴──────────────────────────────────────┐       │
│  │              Mininet                          │       │
│  │                                               │       │
│  │  h1 ─── s1 ─── s2 ─── h3                     │       │
│  │ (client) │  \ / │ (server)                   │       │
│  │         │   X   │  /tmp/backup/ ←──┐         │       │
│  │         │  / \  │                  │         │       │
│  │  h2 ─── s3 ─── s4 ─── h4          │         │       │
│  │ (client)           (server)       │         │       │
│  │                    /tmp/backup/ ←─┘         │       │
│  │                                               │       │
│  └───────────────────┬───────────────────────────┘       │
│                      │ OpenFlow 1.3                     │
│  ┌───────────────────┴───────────────────────────┐       │
│  │         Ryu SDN Controller                     │       │
│  │         controller/sdn_controller.py           │       │
│  │         - Topology discovery (LLDP)            │       │
│  │         - Path computation (Dijkstra)          │       │
│  │         - Flow rule installation               │       │
│  │         - Port status event handling           │       │
│  └───────────────────────────────────────────────┘       │
│                                                          │
│  ┌───────────────────────────────────────────────┐       │
│  │  stored_files/  (host disk, persistent)       │       │
│  │  ├── h3/  ←── bind-mounted to h3:/tmp/backup/ │       │
│  │  └── h4/  ←── bind-mounted to h4:/tmp/backup/ │       │
│  └───────────────────────────────────────────────┘       │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## 6. File Structure

```
distributed-backup-system/
├── docs/
│   ├── runbook/MASTER.md          ← this file
│   ├── reasonix/specs/             ← design spec
│   ├── architecture.md             ← detailed architecture (to write)
│   ├── test-cases.md               ← test scenarios (to write)
│   └── results.md                  ← performance results (to write)
├── stored_files/                   ← Persistent storage (survives Mininet restarts)
│   ├── h3/                         ← Files written by server h3
│   └── h4/                         ← Files written by server h4
├── mininet/
│   └── topology.py                 ← Mininet topology + bind-mount setup
├── controller/
│   └── sdn_controller.py           ← Ryu controller (dynamic flows)
├── client/
│   └── client.py                     ← TCP client (chunk, checksum, send, resume)
├── server/
│   └── server.py                   ← TCP server (receive, verify, reassemble)
├── common/
│   ├── protocol.py                 ← Binary frame format, message types
│   └── checksum.py                 ← CRC32 utilities
├── frontend/
│   ├── app.py                      ← Flask backend (REST + WebSocket + /api/files)
│   ├── templates/
│   │   └── dashboard.html          ← Single-page dashboard + Stored Files panel
│   └── static/
│       ├── style.css               ← Dashboard styling
│       └── dashboard.js            ← Frontend logic (topology, charts, WS, files)
├── tests/
│   ├── test_transfer.py            ← Basic file transfer
│   ├── test_recovery.py            ← Interrupted-transfer recovery
│   └── test_failure.py             ← Link failure + SDN rerouting
├── README.md                       ← Setup + reproduction steps
└── requirements.txt                ← Python dependencies
```

---

## 7. Protocol Design (Quick Reference)

### Frame Format

```
┌────────┬──────┬───────┬────────────┬──────────┬──────────┬─────────┐
│ magic  │ type │ index │ total_chks │ checksum │ data_len │ payload │
│ 4B     │ 1B   │ 4B    │ 4B         │ 4B       │ 4B       │ ≤8KB    │
└────────┴──────┴───────┴────────────┴──────────┴──────────┴─────────┘

magic      = 0xDEADBEEF
type       = 0x01 DATA | 0x02 ACK | 0x03 RESUME | 0x04 HELLO
index      = chunk index (0-based)
total_chks = total chunks in file
checksum   = CRC32 of payload
data_len   = payload length in bytes
```

### Message Flow

```
Client ──HELLO (filename, size)──► Server
Client ◄──ACK (ready)──────────── Server
Client ──DATA (chunk 0)──────────► Server
Client ◄──ACK (0)──────────────── Server
Client ──DATA (chunk 1)──────────► Server
Client ◄──ACK (1)──────────────── Server
        ... connection drops ...
Client ──RESUME (last_ack=1)────► Server
Client ◄──ACK (resume from 2)──── Server
Client ──DATA (chunk 2)──────────► Server
Client ◄──ACK (2)──────────────── Server
        ... continues to completion ...
```

---

## 8. Key Metrics to Measure

| Metric          | How                                    | Why                              |
| --------------- | -------------------------------------- | -------------------------------- |
| Throughput      | File size / transfer time              | Shows TCP performance in Mininet |
| Recovery time   | `link down` → first packet on new path | Shows SDN value                  |
| Packet loss     | Sent chunks vs. ACKed chunks           | Shows reliability                |
| Rerouting delay | Controller event log timestamps        | Shows controller responsiveness  |

---

## 9. Demo Script (5 minutes)

| Time | Action                       | What teacher sees                                                                     |
| ---- | ---------------------------- | ------------------------------------------------------------------------------------- |
| 0:00 | Start Mininet + Ryu + Flask  | Terminals show processes starting                                                     |
| 0:30 | Open dashboard               | Topology diagram, all green                                                           |
| 1:00 | Click [Start Transfer]       | Progress bar fills, throughput chart climbs, event log scrolls                        |
| 2:00 | Click [Trigger Link Failure] | Link turns red, event log shows detection + rerouting, throughput drops then recovers |
| 3:00 | Transfer completes           | "Transfer complete! CRC32 verified"                                                   |
| 3:30 | Show comparison              | "Static: failed forever vs. Dynamic SDN: recovered in 1.3s"                           |
| 4:00 | Questions                    | —                                                                                     |

---

## 9. Reproduction Checklist

- [ ] Ubuntu VM running (20.04+)
- [ ] `sudo apt install mininet openvswitch-switch`
- [ ] `pip install ryu flask flask-socketio pytest matplotlib`
- [ ] `sudo python mininet/topology.py` — Mininet starts, `pingall` works
- [ ] `ryu-manager controller/sdn_controller.py` — controller connects, flows installed
- [ ] `python frontend/app.py` — dashboard accessible at `http://localhost:5000`
- [ ] Click [Start Transfer] — file transfers, progress visible
- [ ] Click [Trigger Link Failure] — rerouting happens, recovery time shown
- [ ] `pytest tests/` — all tests pass

---

_This is the master reference. For design rationale, see `docs/reasonix/specs/2026-01-07-distributed-backup-design.md`._
