# Distributed Backup System — Design Spec

**Project:** #24 Jackfruit Mini Project (40 marks)
**Stack:** Python + Mininet + Ryu SDN Controller + TCP sockets + Flask web dashboard
**Date:** 2026-01-07

---

## 1. Architecture Decisions

### 1.1 SDN Controller: **Ryu**

| Criteria              | Ryu          | POX       | OpenDaylight |
| --------------------- | ------------ | --------- | ------------ |
| Language              | Python ✅    | Python ✅ | Java ❌      |
| Maintenance           | Active ✅    | Stale ⚠️  | Active       |
| Learning curve        | Low ✅       | Medium    | High ❌      |
| Custom app support    | Excellent ✅ | Good      | Complex      |
| Student-friendly docs | Good ✅      | Dated     | Enterprise   |

**Why Ryu:** Python-native (same language as the rest of the project), actively maintained, clean API for custom controller logic, and the easiest path from "I have a topology" to "I have dynamic flow rules." POX works but its docs are dated and the community has moved on. OpenDaylight is enterprise Java — overkill and a language mismatch.

### 1.2 Mininet Topology: **Partial Mesh (4 switches, 4 hosts)**

```
        h1 ─── s1 ─── s2 ─── h3
        (client)  │  \   /  │  (server)
                  │   \ /   │
                  │    X    │
                  │   / \   │
                  │  /   \  │
        h2 ─── s3 ─── s4 ─── h4
        (client)           (server)
```

- **h1, h2** = clients (senders)
- **h3, h4** = backup servers (receivers)
- **s1–s4** = Open vSwitch switches running OpenFlow 1.3
- Multiple redundant paths between every host pair → enables dynamic path selection and failure recovery

**Why this topology:** A pure linear topology has no alternate paths (can't demonstrate rerouting). A full mesh of 4 switches has 6 links — enough redundancy to make path selection meaningful without being visually confusing during a demo. The X-cross in the middle gives us a natural "primary path" vs "alternate path" story.

### 1.3 Client-Server Interaction Model

```
┌────────┐         TCP          ┌──────────┐
│ Client │ ◄──────────────────► │ Server   │
│ (h1/h2)│   chunks + ACKs      │ (h3/h4)  │
└────────┘                      └──────────┘
     │                               │
     │     ┌───────────────┐         │
     └────►│ SDN Controller│◄────────┘
           │ (Ryu)         │
           │ Observes &    │
           │ controls      │
           └───────┬───────┘
                   │
           ┌───────┴───────┐
           │  Mininet OVS  │
           │  (s1–s4)      │
           └───────────────┘
```

**Key insight:** The socket application (client/server) does **NOT** talk directly to the SDN controller. The controller sits below, observing network events (link up/down, packet flows) and installing/modifying flow rules. This is the correct SDN architecture — the application layer and control layer are decoupled. The "integration" is demonstrated by showing that when the controller changes flow rules, the TCP traffic reroutes (visible via `tcpdump` or throughput changes).

### 1.4 Persistence: **Bind-Mounted Host Directory**

Files must survive Mininet stop/start cycles. The solution: bind-mount a host directory into the Mininet server hosts.

```
Host Machine (Ubuntu VM)
├── stored_files/                         ← survives Mininet restarts
│   ├── h3/                               ← bind-mounted to h3:/tmp/backup/
│   │   └── photo.jpg                     ← written by server, persists forever
│   └── h4/                               ← bind-mounted to h4:/tmp/backup/
│       └── photo.jpg
│
└── Mininet (start/stop as many times as you want)
    ├── h3 writes to /tmp/backup/  ──► appears at stored_files/h3/ on host
    └── h4 writes to /tmp/backup/  ──► appears at stored_files/h4/ on host
```

**Implementation in topology.py:**
```python
import os
os.makedirs('stored_files/h3', exist_ok=True)
os.makedirs('stored_files/h4', exist_ok=True)

# After starting the network:
h3.cmd('mkdir -p /tmp/backup')
h3.cmd(f'mount --bind {os.path.abspath("stored_files/h3")} /tmp/backup')
h4.cmd('mkdir -p /tmp/backup')
h4.cmd(f'mount --bind {os.path.abspath("stored_files/h4")} /tmp/backup')
```

**Flask reads files directly from host (no Mininet API needed):**
```python
@app.route('/api/files')
def list_files():
    files = {}
    for server in ['h3', 'h4']:
        path = f'stored_files/{server}'
        if os.path.exists(path):
            files[server] = [
                {'name': f, 'size': os.path.getsize(os.path.join(path, f))}
                for f in os.listdir(path)
            ]
        else:
            files[server] = []
    return jsonify(files)
```

### 1.5 Frontend: **Flask Web Dashboard (runs on host)**

The teacher requires a frontend. For a network/systems project, this is a **web-based dashboard** that visualizes and controls the system — not a tkinter GUI.

```
┌─────────────────────────────────────────────────┐
│              Web Dashboard (browser)             │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Topology │  │ Transfer │  │   Metrics    │  │
│  │  (SVG)   │  │ Control  │  │  (charts)    │  │
│  │          │  │          │  │              │  │
│  │  s1──s2  │  │ [Start]  │  │ Throughput   │  │
│  │  │╲ ╱│   │  │ [Kill]   │  │ ▁▂▃▅▇▅▃▂▁   │  │
│  │  │ ╳ │   │  │ [Resume] │  │              │  │
│  │  │╱ ╲│   │  │ [Fail    │  │ Recovery     │  │
│  │  s3──s4  │  │  Link]   │  │ Time: 1.2s   │  │
│  │          │  │          │  │              │  │
│  └──────────┘  └──────────┘  └──────────────┘  │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │        Stored Files                       │   │
│  │                                          │   │
│  │  Server h3 (10.0.0.3):                   │   │
│  │    📄 photo.jpg (1.0 MB)                 │   │
│  │    📄 data.csv (256 KB)                  │   │
│  │                                          │   │
│  │  Server h4 (10.0.0.4):                   │   │
│  │    📄 photo.jpg (1.0 MB)                 │   │
│  │    📄 backup.zip (2.1 MB)                │   │
│  │                                          │   │
│  │  [Clear All]                             │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │           Event Log                       │   │
│  │  > Transfer complete: photo.jpg           │   │
│  │  > Stored on h3 and h4                    │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
         │
         │ REST API + WebSocket
         ▼
┌─────────────────────────────────────────────────┐
│           Flask Backend (Python, on host)        │
│                                                  │
│  /api/start-transfer    /api/stop-transfer       │
│  /api/resume            /api/link-down           │
│  /api/link-up           /api/status              │
│  /api/metrics           /api/events (WebSocket)  │
│                                                  │
│  Talks to: Mininet Python API, Ryu controller,  │
│            client/server processes               │
└─────────────────────────────────────────────────┘
```

**Panels:**

| Panel                | What it shows                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------------- |
| **Topology**         | Live SVG diagram — switches/hosts, links colored by status (green=up, red=down, yellow=congested) |
| **Transfer Control** | Buttons: Start Transfer, Kill Client, Resume, Trigger Link Failure                                |
| **Metrics**          | Real-time throughput chart, recovery time, chunk progress bar                                     |
| **Event Log**        | Scrolling log — link down → controller detected → rerouted → resumed                              |

**Tech stack:**

- **Backend:** Flask + Flask-SocketIO (Python, matches the project)
- **Frontend:** Single HTML page, vanilla JS + SVG for topology, Chart.js for metrics
- **Communication:** REST API for commands, WebSocket for live updates
- **Runs on:** Host machine (not inside Mininet) — uses Mininet Python API to control the network

**Why this over tkinter:** The teacher can open it in a browser, it looks professional, and it's shareable (send the URL or screenshot). Tkinter looks dated and can't be screenshotted as cleanly for the report.

---

## 2. Protocol Design

### 2.1 Chunking

- **Chunk size:** 8 KB (8192 bytes)
- **Why 8 KB:** Large enough to be efficient (not too much per-chunk overhead), small enough that retransmitting a lost chunk is cheap. Matches typical TCP window behavior in Mininet.

### 2.2 Checksum: **CRC32**

| Algorithm | Speed      | Collision Resistance                        | Fit |
| --------- | ---------- | ------------------------------------------- | --- |
| CRC32     | Fastest ✅ | Adequate for network bit-error detection ✅ | ✅  |
| MD5       | Slow       | Overkill (cryptographic) ❌                 | ❌  |
| SHA-256   | Slowest    | Overkill ❌                                 | ❌  |

**Why CRC32:** This is network integrity, not security. CRC32 catches the bit-flip errors that TCP might miss in an emulated environment, and it's built into Python (`zlib.crc32`) with zero dependencies. MD5/SHA-256 are cryptographic hashes — wrong tool for the job.

### 2.3 Message Format: **Binary Header + Payload**

```
┌─────────────────────────────────────────────────┐
│                  FRAME FORMAT                    │
├──────────┬──────────┬──────────┬────────────────�│
│  magic   │  type    │  index   │  total_chunks  │
│  4 bytes │  1 byte  │  4 bytes │  4 bytes       │
├──────────┼──────────┼──────────┼────────────────┤
│  checksum    │  data_len   │  payload         │
│  4 bytes     │  4 bytes     │  variable       │
└──────────┴──────────┴──────────┴────────────────┘

magic      = 0xDEADBEEF (sanity check)
type       = 0x01 (DATA), 0x02 (ACK), 0x03 (RESUME), 0x04 (HELLO)
index      = chunk index (0-based)
total_chunks = total number of chunks in this file
checksum   = CRC32 of payload
data_len   = length of payload in bytes
payload    = actual file data (up to 8 KB)
```

**Why binary:** More impressive than JSON, more efficient, and demonstrates real protocol design. JSON would be easier but looks like a web project, not a systems project.

### 2.4 Interrupted-Transfer Recovery

```
Client                              Server
  │                                   │
  │── HELLO (filename, file_size) ───►│
  │◄──────── ACK (ready) ────────────│
  │                                   │
  │── DATA (chunk 0) ───────────────►│
  │◄──────── ACK (0) ───────────────│
  │── DATA (chunk 1) ───────────────►│
  │◄──────── ACK (1) ───────────────│
  │                                   │
  │  ... connection drops ...          │
  │                                   │
  │── RESUME (last_ack=1) ──────────►│
  │◄──────── ACK (resume from 2) ───│
  │                                   │
  │── DATA (chunk 2) ───────────────►│
  │◄──────── ACK (2) ───────────────│
  │── ... continues ...              │
```

**Mechanism:**

1. Server maintains a write offset (last successfully written chunk index)
2. On disconnect, server keeps the partial file + metadata (last ACKed index)
3. Client reconnects, sends `RESUME` with the last index it received an ACK for
4. Server confirms the resume point, client continues from `last_ack + 1`
5. No data is re-sent — true resume, not restart

### 2.5 Multiple Backup Servers

- Client is configured with a list of server addresses: `[(h3_ip, port), (h4_ip, port)]`
- **Strategy:** Primary-backup with automatic failover
  - Client tries primary server first
  - If primary is unreachable (connection refused/timeout), client fails over to backup
  - Both servers store the same file (redundancy)
- **Why not load balancing:** Load balancing splits one file across servers, which complicates reassembly. Primary-backup is simpler to implement correctly and still demonstrates the "multiple servers" requirement. The SDN controller can also route to different servers based on network conditions.

---

## 3. SDN Integration

### 3.1 How the Controller Monitors the Network

| Mechanism           | What it detects           | OpenFlow message      |
| ------------------- | ------------------------- | --------------------- |
| Port status events  | Link up/down              | `OFPPortStatus`       |
| Flow stats requests | Traffic volume per flow   | `OFPFlowStatsRequest` |
| Packet-in events    | Unknown/unmatched packets | `OFPPacketIn`         |

The controller runs a background thread that polls flow stats every 5 seconds to detect congestion (high packet count on a path = potential bottleneck).

### 3.2 Dynamic Path Selection

**Algorithm:** Dijkstra's shortest path, with link-cost weights that increase when congestion is detected.

```
Normal state:
  h1 → s1 → s2 → h3  (cost: 3 hops)

Congestion detected on s1→s2:
  h1 → s1 → s3 → s4 → s2 → h3  (cost: 5 hops, but uncongested)

Link failure (s1→s2 down):
  h1 → s1 → s3 → s4 → s2 → h3  (only available path)
```

The controller maintains a topology graph (built from LLDP packets + port status events) and recomputes paths when:

- A port status change event fires (link up/down)
- Flow stats exceed a congestion threshold

### 3.3 Failure Simulation

- **Mininet CLI:** `mininet> link s1 s2 down` — triggers `OFPPortStatus` event
- **Controller reaction:** Receives event → updates topology graph → recomputes paths → installs new flow rules on affected switches
- **Recovery time metric:** Time from `link down` event to new flows installed and traffic flowing

### 3.4 App ↔ Controller Interface

**No direct interface.** This is intentional and architecturally correct:

- The socket app (client/server) operates at Layer 4 (TCP) — it knows nothing about the SDN controller
- The controller operates at Layer 2 (OpenFlow) — it knows nothing about the file transfer protocol
- The integration is **behavioral:** when the controller changes flow rules, the TCP packets take a different path, which is observable via:
  - `tcpdump` on the switches (showing packets on different interfaces)
  - Throughput changes (if the new path is congested)
  - Recovery time (how fast traffic resumes after rerouting)

This is the cleanest demonstration of SDN value: the network adapts without the application knowing.

---

## 4. Implementation Plan

### 4.1 File Structure

```
distributed-backup-system/
├── README.md
├── docs/
│   ├── architecture.md
│   ├── test-cases.md
│   └── results.md
├── stored_files/              # Persistent file storage (survives Mininet restarts)
│   ├── h3/                    # Files written by server h3
│   └── h4/                    # Files written by server h4
├── mininet/
│   └── topology.py            # Mininet topology + bind-mount setup
├── controller/
│   └── sdn_controller.py      # Ryu controller app
├── client/
│   └── client.py                # TCP client: chunking, checksum, transfer
├── server/
│   └── server.py              # TCP server: receive, verify, reassemble
├── common/
│   ├── protocol.py            # Frame format, message types
│   └── checksum.py            # CRC32 utilities
├── frontend/
│   ├── app.py                 # Flask backend: REST API + WebSocket + /api/files
│   ├── templates/
│   │   └── dashboard.html     # Single-page web dashboard + Stored Files panel
│   └── static/
│       ├── style.css          # Dashboard styling
│       └── dashboard.js       # Frontend logic: topology, charts, WS, file listing
├── tests/
│   ├── test_transfer.py       # Basic file transfer tests
│   ├── test_recovery.py       # Interrupted-transfer recovery tests
│   └── test_failure.py        # Path failure & SDN rerouting tests
└── requirements.txt
```

### 4.2 Implementation Order (Dependency-First)

| Phase | What                                                                         | Depends on    | Test                                                             |
| ----- | ---------------------------------------------------------------------------- | ------------- | ---------------------------------------------------------------- |
| **1** | `common/protocol.py` + `common/checksum.py`                                  | nothing       | Unit tests for frame pack/unpack, CRC32                          |
| **2** | `server/server.py`                                                           | phase 1       | Start server, connect with netcat, verify it receives data       |
| **3** | `client/client.py`                                                           | phase 1, 2    | Client sends file, server reassembles, diff original vs received |
| **4** | `mininet/topology.py`                                                        | phase 2, 3    | Start Mininet, ping between hosts, run client/server inside      |
| **5** | `controller/sdn_controller.py`                                               | phase 4       | Controller installs flows, ping works through controller         |
| **6** | Recovery logic (client + server)                                             | phase 3       | Kill client mid-transfer, restart, verify resume                 |
| **7** | Dynamic path selection                                                       | phase 5       | `link down`, verify rerouting via tcpdump                        |
| **8** | `frontend/app.py` + `frontend/templates/dashboard.html` + `frontend/static/` | phase 4, 5    | Open dashboard in browser, see topology, click buttons           |
| **9** | Performance evaluation                                                       | phase 6, 7, 8 | Run experiments, collect metrics, write results                  |

### 4.3 Testing at Each Stage

- **Phase 1-3:** Run on localhost (no Mininet needed) — `python server.py &` then `python client.py testfile.bin`
- **Phase 4:** `sudo python topology.py` — verify with `pingall` in Mininet CLI
- **Phase 5:** Start Ryu controller, then Mininet — verify flows installed via `ovs-ofctl dump-flows s1`
- **Phase 6:** Start transfer, `kill -9` the client mid-transfer, restart with `--resume` flag
- **Phase 7:** During transfer, `link s1 s2 down` in Mininet CLI, watch `tcpdump` on s3/s4
- **Phase 8:** Scripted runs with `time` and throughput calculation, results saved to `docs/results.md`

---

## 5. Challenging Scenario

### Selected: **Link Failure + Automatic Rerouting**

**Why this one:**

- Most impactful for the rubric (directly demonstrates "Dynamic SDN Functionality" — 6 marks)
- Most feasible (Mininet's `link down` + Ryu's port status event is well-documented)
- Most visual for a demo (teacher sees traffic stop, then resume on a different path)
- Naturally produces a measurable metric (recovery time)

**Honorable mentions:**

- Server failover: Also good, but the client-side failover logic is application-level, not SDN-level. Less impressive for the "Dynamic SDN" criterion.
- Congestion-based path selection: Harder to simulate realistically in Mininet (need to generate background traffic), and the visual difference is subtle.

---

## 6. Performance Evaluation Plan

### 6.1 Metrics

| Metric          | How to measure                                   | Tool                   |
| --------------- | ------------------------------------------------ | ---------------------- |
| Throughput      | File size / transfer time                        | Python`time` in client |
| Recovery time   | Time from`link down` to first packet on new path | `tcpdump` timestamps   |
| Packet loss     | Sent chunks vs. ACKed chunks                     | Client/server logs     |
| Rerouting delay | Controller event log timestamps                  | Ryu controller logs    |

### 6.2 Experiments

| Experiment                   | Condition                                | What it shows                                              |
| ---------------------------- | ---------------------------------------- | ---------------------------------------------------------- |
| **A: Baseline**              | Static flows, no controller intelligence | Normal throughput                                          |
| **B: Dynamic SDN**           | Controller actively manages flows        | Throughput under normal conditions (should match baseline) |
| **C: Link failure**          | `link s1 s2 down` during transfer        | Recovery time with dynamic rerouting                       |
| **D: Link failure + static** | Same failure, but no controller reaction | Transfer fails or stalls (shows SDN value)                 |

### 6.3 Comparison: Dynamic SDN vs. Static Routing

- **Static baseline:** Install flow rules manually with `ovs-ofctl`, no controller running. When a link fails, traffic stops.
- **Dynamic SDN:** Controller detects failure and reroutes automatically.
- **Key comparison:** Recovery time (dynamic: ~1-2 seconds vs. static: never/infinite)

### 6.4 Presentation

- Results table in `docs/results.md`
- Bar chart: throughput (normal vs. failure) — generated with Python `matplotlib`
- Timeline diagram: link down → event detected → flows installed → traffic resumes
- Demo video showing the full flow

---

## 7. Rubric Alignment

| Rubric Component | Marks | How This Project Addresses It |
|------------------|-------|-------------------------------|
| Problem Understanding & Architecture | 2 | Clear architecture (section 1), well-defined components, communication flow, expected network behavior |
| TCP/UDP Socket Implementation | 4 | Low-level TCP sockets, client/server model, threading for concurrency, error handling on all ops, custom binary protocol with message types |
| Mininet + SDN Implementation | 4 | 4-switch partial mesh, Ryu controller with LLDP discovery, OpenFlow 1.3 flow rules, `pingall` verified |
| Socket–SDN Integration | 3 | Controller observes network events and reroutes TCP traffic; dashboard shows both simultaneously; end-to-end demo: link failure during transfer → rerouting → resume |
| Initial Testing & Demo | 2 | `pytest tests/` covers transfer, recovery, failure; all pass before demo |
| Complete Application Functionality | 4 | All 8 required features: TCP transfer, chunking, checksums, resume, multiple servers, path selection, failure simulation, metrics |
| Dynamic SDN Functionality | 6 | **Rerouting** (link failure → Dijkstra recomputation), **monitoring** (flow stats polling), **recovery** (path restoration), **routing** (Dijkstra shortest path) |
| Challenging Scenario / Robustness | 4 | Link failure during active transfer — controller detects, reroutes, transfer resumes with zero data loss |
| Performance Evaluation & Comparison | 5 | Throughput + recovery time measured; experiments: normal, link failure with SDN, link failure without SDN (static baseline); comparison table |
| Documentation, Final Demo & Viva | 6 | README, architecture.md, test-cases.md, results.md, MASTER.md, spec — all written; reproducible demo; viva-ready |

### Dynamic SDN Mechanisms Implemented

The rubric lists: routing, filtering, prioritization, load balancing, rerouting, monitoring, blocking, recovery.

| Mechanism | Implemented | How |
|-----------|-------------|-----|
| **Rerouting** | Yes | Link failure → Dijkstra recomputation → new flow rules |
| **Monitoring** | Yes | Flow stats polled every 5s for congestion detection |
| **Recovery** | Yes | Link up event → controller restores optimal path |
| **Routing** | Yes | Dijkstra shortest-path for all flow installations |
| Filtering | No | Not needed for this project |
| Prioritization | No | Not needed for this project |
| Load balancing | No | Primary-backup failover at app level instead |
| Blocking | No | Not needed for this project |

### Socket-SDN Integration Points

1. **Network event → SDN action:** Link down event triggers controller rerouting (visible during active transfer)
2. **Application state → Dashboard:** Transfer progress, chunk count, throughput shown live alongside network state
3. **End-to-end demonstration:** "Trigger Link Failure" during transfer → controller reroutes → transfer resumes — both layers visible simultaneously

---

## 8. Out of Scope (Explicitly)

- UDP (TCP is mandatory)
- Real network deployment (everything runs in Mininet)
- Encryption/security (checksums are for integrity, not security)
- Tkinter/desktop GUI (web dashboard is the frontend)
- Actual distributed consensus (this is a backup system, not a distributed database)

---

## 8. Open Questions to Resolve Before Coding

1. **Ryu version:** Latest stable (4.34) — confirm compatibility with Python 3.x on the Ubuntu VM
2. **OpenFlow version:** 1.3 (widely supported by both Ryu and Mininet's OVS)
3. **File for testing:** A ~1 MB file gives ~125 chunks at 8 KB — enough to demonstrate chunking without slow tests
4. **Port numbers:** Server listens on port 9000 (arbitrary, avoid well-known ports)
5. **Mininet install:** Use the official Mininet VM image or `apt install mininet` on Ubuntu

---

_This spec is the agreed design. Implementation begins once all open questions are resolved._
