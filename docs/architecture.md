# Architecture — Distributed Backup System

## Overview

A distributed backup application where clients transfer files to backup servers over TCP, with an SDN controller (Ryu) that dynamically selects network paths and recovers from link failures. Visualized on a web dashboard.

## Components

```
┌─────────────────────────────────────────────────────────────┐
│                     Ubuntu VM (one machine)                │
│                                                             │
│  ┌─────────────┐                                            │
│  │   Browser   │  http://localhost:5000                     │
│  └──────┬──────┘                                            │
│         │ HTTP + WebSocket                                   │
│  ┌──────┴──────┐                                            │
│  │ Flask app   │  frontend/app.py                           │
│  │ (host)      │  REST API + WebSocket + /api/files         │
│  └──────┬──────┘                                            │
│         │                                                   │
│  ┌──────┴──────────────────────────────────────┐           │
│  │              Mininet                          │           │
│  │                                               │           │
│  │  h1 ─── s1 ─── s2 ─── h3                     │           │
│  │ (client) │  \ / │ (server)                   │           │
│  │         │   X   │  /tmp/backup/ ←──┐         │           │
│  │         │  / \  │                  │         │           │
│  │  h2 ─── s3 ─── s4 ─── h4          │         │           │
│  │ (client)           (server)       │         │           │
│  │                    /tmp/backup/ ←─┘         │           │
│  │                                               │           │
│  └───────────────────┬───────────────────────────┘           │
│                      │ OpenFlow 1.3                         │
│  ┌───────────────────┴───────────────────────────┐           │
│  │         Ryu SDN Controller                     │           │
│  │         controller/sdn_controller.py           │           │
│  │         - Topology discovery (LLDP)            │           │
│  │         - Path computation (Dijkstra)          │           │
│  │         - Flow rule installation               │           │
│  │         - Port status event handling           │           │
│  └───────────────────────────────────────────────┘           │
│                                                             │
│  ┌───────────────────────────────────────────────┐           │
│  │  stored_files/  (host disk, persistent)       │           │
│  │  ├── h3/  ←── bind-mounted to h3:/tmp/backup/ │           │
│  │  └── h4/  ←── bind-mounted to h4:/tmp/backup/ │           │
│  └───────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. Client (`client/client.py`)

- Reads a file and splits it into 8 KB chunks
- Computes CRC32 checksum for each chunk
- Sends chunks over TCP using a binary protocol
- Supports RESUME for interrupted transfers
- Primary-backup failover: tries h3, then h4

### 2. Server (`server/server.py`)

- Multi-threaded TCP server listening on port 9000
- Receives chunks, verifies CRC32, writes to disk
- Reassembles files from chunks
- Handles multiple concurrent clients
- Tracks last ACKed chunk for resume support

### 3. SDN Controller (`controller/sdn_controller.py`)

- Ryu application with OpenFlow 1.3
- Discovers topology via LLDP packets
- Runs Dijkstra's algorithm for shortest-path routing
- Installs flow rules on all switches
- Monitors port status events for link failures
- Automatically reroutes traffic when links fail
- Polls flow stats every 5 seconds for congestion monitoring

### 4. Mininet Topology (`mininet/topology.py`)

- 4 OVS switches in full mesh (6 inter-switch links)
- 4 hosts: h1/h2 (clients), h3/h4 (servers)
- `stored_files/` bind-mounted into server hosts at `/tmp/backup/`
- Files persist across Mininet restarts

### 5. Web Dashboard (`frontend/`)

- Flask + Flask-SocketIO backend
- Single-page HTML dashboard with vanilla JS
- SVG topology diagram with live link status
- Chart.js for real-time throughput visualization
- REST API for controlling transfers and link events
- WebSocket for live event streaming

## Protocol Design

### Frame Format

```
+--------+------+-------+------------+----------+----------+---------+
| magic  | type | index | total_chks | checksum | data_len | payload |
| 4B     | 1B   | 4B    | 4B         | 4B       | 4B       | <=8KB   |
+--------+------+-------+------------+----------+----------+---------+
```

### Message Types

| Type | Name | Purpose |
|------|------|---------|
| 0x01 | DATA | File chunk with CRC32 checksum |
| 0x02 | ACK | Acknowledgment of received chunk |
| 0x03 | RESUME | Request to resume from specific chunk |
| 0x04 | HELLO | Initial handshake with filename and size |

### Transfer Flow

```
Client                              Server
  |                                   |
  |--- HELLO (filename, file_size) -->|
  |<-------------- ACK (ready) --------|
  |                                   |
  |--- DATA (chunk 0) --------------->|
  |<-------------- ACK (0) ------------|
  |--- DATA (chunk 1) --------------->|
  |<-------------- ACK (1) ------------|
  |                                   |
  |  ... connection drops ...          |
  |                                   |
  |--- RESUME (last_ack=1) ---------->|
  |<------ ACK (resume from 2) -------|
  |                                   |
  |--- DATA (chunk 2) --------------->|
  |<-------------- ACK (2) ------------|
  |--- ... continues to completion ... |
```

## SDN Integration

### Socket-SDN Coupling

The socket application and SDN controller are **decoupled** — they do not communicate directly. Integration is behavioral:

1. **Network event → SDN action:** Link down event triggers controller rerouting
2. **Application state → Dashboard:** Transfer progress shown alongside network state
3. **End-to-end demo:** Link failure during transfer → rerouting → transfer resumes

### Dynamic Mechanisms

| Mechanism | Implementation |
|-----------|---------------|
| **Routing** | Dijkstra shortest-path for all flow installations |
| **Rerouting** | Port status event → topology update → Dijkstra recomputation → new flows |
| **Monitoring** | Flow stats polled every 5 seconds |
| **Recovery** | Link up event → optimal path restored |

## Data Persistence

Files are written to `/tmp/backup/` inside Mininet hosts, which is bind-mounted to `stored_files/h3/` and `stored_files/h4/` on the host machine. This ensures files survive Mininet stop/start cycles.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.8+ |
| Network Emulation | Mininet 2.3+ |
| SDN Controller | Ryu 4.34+ |
| Switch Dataplane | Open vSwitch 2.13+ |
| Web Backend | Flask 2.0+ |
| Web Frontend | Vanilla JS + SVG + Chart.js |
| Checksum | zlib.crc32 |
| Persistence | bind-mount |
| Testing | pytest 7.0+ |
