# Distributed Backup System

A distributed backup application where clients transfer files to backup servers over TCP, with an SDN controller that dynamically selects network paths and recovers from link failures. Visualized on a web dashboard.

---

## What It Does

- Sends files from a client to backup servers over TCP
- Splits files into chunks and verifies each with a checksum
- Resumes interrupted transfers from where they left off
- Uses multiple backup servers for redundancy
- Automatically reroutes traffic when a network link fails
- Shows everything live on a web dashboard

---

## How to Run

### 1. Install dependencies

```bash
sudo apt install mininet openvswitch-switch
pip install -r requirements.txt
```

### 2. Start the system

Open three terminals:

```bash
# Terminal 1 — Network
sudo python mininet/topology.py

# Terminal 2 — SDN Controller
ryu-manager controller/sdn_controller.py

# Terminal 3 — Dashboard
python frontend/app.py
```

### 3. Open the dashboard

Go to `http://localhost:5000` in your browser.

### 4. Run a demo

1. Drop a file into `shared_files/`, pick it in the dashboard **Shared Files** panel, click **Start Transfer** — watch the file move from client to server
2. Click **Trigger Link Failure** — watch the network reroute and the transfer resume
3. Check the **Stored Files** panel — your file is there, even after restarting

---

## Project Structure

```
mininet/       — Network topology (4 switches, 4 hosts) + dashboard bridge
controller/    — SDN controller (dynamic path selection + failure recovery)
client/        — File sender (chunking, checksum, resume)
server/        — File receiver (verify, reassemble, store)
common/        — Shared protocol + checksum utilities
frontend/      — Web dashboard (Flask + real-time updates)
shared_files/  — Drop files here to pick them from the dashboard
stored_files/  — Where transferred files are saved
tests/         — Test suite
docs/          — Design spec, runbook, architecture, results
```

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Documentation

- [Architecture](docs/architecture.md) — System design and component details
- [Test Cases](docs/test-cases.md) — Test scenarios and expected results
- [Results](docs/results.md) — Performance metrics and comparison
- [Design Spec](docs/reasonix/specs/2026-01-07-distributed-backup-design.md) — Full design rationale
- [Runbook](docs/runbook/MASTER.md) — How the system runs

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.8+ |
| Network Emulation | Mininet |
| SDN Controller | Ryu |
| Switch Dataplane | Open vSwitch (OpenFlow 1.3) |
| Web Backend | Flask + Flask-SocketIO |
| Web Frontend | Vanilla JS + SVG + Chart.js |
| Checksum | zlib.crc32 |
| Persistence | bind-mount |
| Testing | pytest |
