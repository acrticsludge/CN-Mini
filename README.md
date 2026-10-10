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
pip install ryu flask flask-socketio pytest matplotlib
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

1. Click **Start Transfer** — watch the file move from client to server
2. Click **Trigger Link Failure** — watch the network reroute and the transfer resume
3. Check the **Stored Files** panel — your file is there, even after restarting

---

## Project Structure

```
mininet/       — Network topology (4 switches, 4 hosts)
controller/    — SDN controller (dynamic path selection + failure recovery)
client/        — File sender (chunking, checksum, resume)
server/        — File receiver (verify, reassemble, store)
common/        — Shared protocol + checksum utilities
frontend/      — Web dashboard (Flask + real-time updates)
stored_files/  — Where transferred files are saved
tests/         — Test suite
docs/          — Design spec, runbook, architecture, results
```

---
