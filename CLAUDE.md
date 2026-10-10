# CLAUDE.md / AGENTS.md

## Project: Distributed Backup System (SDN + Socket Programming)

**Project ID:** 24 — Jackfruit Mini Project  
**Protocol:** TCP  
**Environment:** Mininet + SDN Controller + Socket Programming

---

## Goal

Design, implement, and evaluate a **distributed backup application** where clients transfer files to backup servers over **TCP**, with an **SDN controller** that dynamically selects suitable network paths and supports **recovery during failures**.

The project must demonstrate:
- Low-level TCP socket communication
- Concurrency and protocol design
- Programmable network control via SDN
- Performance evaluation in a Mininet-based environment

---

## Problem Statement

Implement a distributed backup application where:
- Clients transfer files to backup servers through **TCP**
- SDN dynamically selects suitable paths
- The system supports **recovery during failures**

### Required Features

| # | Feature |
|---|---------|
| 1 | Implement TCP file transfer |
| 2 | Divide files into chunks |
| 3 | Use checksums for integrity |
| 4 | Support interrupted-transfer recovery |
| 5 | Use multiple backup servers |
| 6 | Select suitable network paths |
| 7 | Simulate path failure |
| 8 | Measure throughput and recovery time |

---

## Mandatory Requirements

- [ ] Application must use **TCP sockets** (as specified)
- [ ] Include appropriate **client, server, worker, broker, or peer** components
- [ ] Support **multiple communicating nodes/clients**
- [ ] Deploy and test in a **Mininet-based network topology**
- [ ] Demonstrate **interaction between the socket application and the SDN-controlled network**
- [ ] Complete system runs in a controlled **Mininet + SDN controller + socket-programming** environment
- [ ] Provide **topology, source code, configuration, test cases, and instructions** to reproduce results
- [ ] Final demo code shared in a **GitHub repo** with complete documentation

---

## Evaluation Rubric (40 Marks Total)

### D1 — Initial Submission (15 marks)

| Component | Marks |
|-----------|-------|
| Problem Understanding & Architecture | 2 |
| TCP/UDP Socket Implementation | 4 |
| Mininet + SDN Implementation | 4 |
| Socket–SDN Integration | 3 |
| Initial Testing & Demo | 2 |

### D2 — Final Submission (25 marks)

| Component | Marks |
|-----------|-------|
| Complete Application Functionality | 4 |
| Dynamic SDN Functionality | 6 |
| Challenging Scenario / Robustness | 4 |
| Performance Evaluation & Comparison | 5 |
| Documentation, Final Demo & Viva | 6 |

### Detailed Criteria

**Problem Understanding & Architecture (2)**
- Clear understanding of the assigned problem
- Well-defined objectives, system architecture, components, communication flow, and expected network behavior

**TCP/UDP Socket Implementation (4)**
- Correct implementation of TCP communication using low-level sockets
- Appropriate client/server communication, connection handling, concurrency, error handling
- Application-level message exchange

**Mininet + SDN Implementation (4)**
- Correct Mininet topology
- Functional SDN controller
- Appropriate flow rules
- Proper host/switch connectivity
- Meaningful implementation of the SDN functionality required by the project

**Socket–SDN Integration (3)**
- Effective interaction between the socket application and SDN network
- Application/network events influence SDN behavior where required
- End-to-end integration is demonstrated

**Initial Testing & Demo (2)**
- Basic end-to-end system works
- Appropriate test cases are demonstrated
- Communication between application components and the SDN-controlled network is verified

**Complete Application Functionality (4)**
- All major project requirements are implemented
- Application supports expected users/clients/flows
- Functionality works correctly under normal operating conditions

**Dynamic SDN Functionality (6)**
- SDN controller dynamically responds to network/application conditions
- Mechanisms: routing, filtering, prioritization, load balancing, rerouting, monitoring, blocking, or recovery

**Challenging Scenario / Robustness (4)**
- At least one meaningful challenging scenario demonstrated (congestion, packet loss, link failure, server overload, unauthorized access, or service failure)
- System responds appropriately

**Performance Evaluation & Comparison (5)**
- Appropriate metrics measured
- Experiments conducted under multiple conditions
- Results presented clearly
- Proposed approach compared with a suitable baseline/static approach

**Documentation, Final Demo & Viva (6)**
- Complete and well-structured documentation
- Architecture, implementation, testing, and results clearly presented
- Reproducible final demonstration
- Students demonstrate individual understanding through viva

---

## Architecture Overview

```
┌─────────────┐       TCP        ┌──────────────────┐
│   Client     │ ◄──────────────► │  Backup Server  │
│  (sender)    │   file chunks   │  (receiver)     │
└─────────────┘   + checksums    └──────────────────┘
       │                                │
       │         ┌──────────────┐       │
       └────────►│  SDN Controller │◄────┘
                 │  (dynamic path  │
                 │   selection +   │
                 │   failure rec.) │
                 └──────┬───────┘
                        │
              ┌─────────┴─────────┐
              │  Mininet Topology  │
              │  (switches + hosts)│
              └───────────────────┘
```

### Key Components

1. **Client** — Divides files into chunks, sends over TCP with checksums, handles interrupted-transfer recovery
2. **Backup Server(s)** — Receives file chunks, verifies integrity via checksums, reassembles files; multiple servers for redundancy
3. **SDN Controller** — Dynamically selects suitable network paths, simulates path failure, triggers recovery
4. **Mininet Topology** — Switches and hosts providing the testbed network

---

## Tech Stack

- **Language:** Python (socket programming)
- **Network Emulation:** Mininet
- **SDN Controller:** Ryu / POX / OpenDaylight (choose one)
- **Protocol:** TCP
- **Integrity:** Checksums (e.g., MD5/SHA or CRC32 per chunk)
- **Version Control:** GitHub

---

## Project Structure (Suggested)

```
distributed-backup-system/
├── README.md                  # Setup, usage, reproduction instructions
├── docs/
│   ├── architecture.md        # System design & architecture
│   ├── test-cases.md          # Test scenarios & expected results
│   └── results.md             # Performance evaluation & comparison
├── mininet/
│   └── topology.py            # Mininet topology definition
├── controller/
│   └── sdn_controller.py      # SDN controller with dynamic path selection
├── client/
│   └── client.py                # TCP client: chunking, checksum, transfer
├── server/
│   └── server.py              # TCP server: receive, verify, reassemble
├── common/
│   ├── protocol.py            # Message format / protocol definitions
│   └── checksum.py            # Checksum utilities
└── tests/
    ├── test_transfer.py       # Basic file transfer tests
    ├── test_recovery.py       # Interrupted-transfer recovery tests
    └── test_failure.py        # Path failure & SDN rerouting tests
```

---

## Implementation Checklist

### Phase 1: Core Socket Communication
- [ ] TCP client that reads a file, divides into chunks, computes per-chunk checksums
- [ ] TCP server that receives chunks, verifies checksums, reassembles the file
- [ ] Handle partial/interrupted transfers (resume from last successful chunk)
- [ ] Support multiple concurrent clients

### Phase 2: Mininet + SDN Setup
- [ ] Define Mininet topology with multiple paths between hosts
- [ ] Implement SDN controller with flow rule management
- [ ] Verify host-to-host connectivity through the SDN-controlled network

### Phase 3: Socket–SDN Integration
- [ ] SDN controller monitors network conditions (link status, congestion)
- [ ] Dynamic path selection: controller reroutes traffic based on conditions
- [ ] Simulate path failure (link down) and demonstrate automatic recovery
- [ ] Measure throughput and recovery time

### Phase 4: Robustness & Performance
- [ ] Demonstrate at least one challenging scenario (link failure, server overload, etc.)
- [ ] Measure throughput under normal and failure conditions
- [ ] Compare dynamic SDN approach vs. static routing baseline
- [ ] Document all results

### Phase 5: Documentation & Demo
- [ ] Write complete README with reproduction steps
- [ ] Document architecture, protocol design, and test cases
- [ ] Record final demo
- [ ] Push everything to GitHub

---

## Notes for AI Assistants

- This is a **Mininet + SDN** project — all network emulation happens in Mininet, not real networks
- The SDN controller must **dynamically** respond to network events (not just static flow rules)
- **TCP is mandatory** (not UDP) for this project
- File chunking + checksums are core requirements — do not skip
- Interrupted-transfer recovery means the client should be able to resume from where it left off, not restart from scratch
- Performance evaluation must include a **comparison** with a baseline (e.g., static routing without SDN intelligence)
- The final deliverable is a **GitHub repo** with full documentation
