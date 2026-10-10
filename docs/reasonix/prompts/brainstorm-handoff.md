# Handoff Prompt — Brainstorm Session

You are picking up a student project: a **Distributed Backup System** using **Mininet + SDN + TCP socket programming** (Project #24, Jackfruit Mini Project, 40 marks).

Read `C:\Anubhav\CN-Mini\CLAUDE.md` first for the full context, then brainstorm the project.

**What exists:**
- A complete rubric (D1: 15 marks, D2: 25 marks) and feature list in CLAUDE.md
- No code yet — this is a greenfield implementation

**What you need to brainstorm:**

1. **Architecture decisions:**
   - Which SDN controller framework (Ryu vs POX vs something else) and why
   - Mininet topology design — how many switches/hosts, what kind of paths (linear, tree, mesh)
   - Client-server interaction model — how does the client discover servers, how does the SDN controller interact with the socket application

2. **Protocol design:**
   - Chunk size, checksum algorithm (CRC32 vs MD5 vs SHA-256)
   - Message format/protocol for client-server communication (JSON, binary, custom)
   - How interrupted-transfer recovery works — checkpointing, resume offsets, ACK strategy
   - How multiple backup servers are used — replication, load balancing, failover

3. **SDN integration:**
   - How the controller monitors the network (link status, congestion, packet inspection)
   - How it dynamically selects paths — what triggers rerouting
   - How path failure is simulated and recovery is demonstrated
   - The interface between the socket app and the controller (REST API, event bus, direct calls)

4. **Implementation plan:**
   - File-by-file breakdown of what to build
   - What to implement first (dependency order)
   - How to test at each stage (unit tests, integration tests, Mininet demo scripts)

5. **Challenging scenarios** (need at least 1 for D2):
   - Link failure + automatic rerouting
   - Server failover
   - Congestion-based path selection
   - Which is most impactful for the rubric and most feasible

6. **Performance evaluation plan:**
   - What metrics to measure (throughput, recovery time, packet loss)
   - What baseline to compare against (static routing vs dynamic SDN)
   - How to present results

**Constraints:**
- TCP is mandatory (not UDP)
- Everything runs in Mininet (no real network)
- Python is the language
- Must be reproducible from a GitHub repo with clear instructions
- This is a student project — keep it impressive but achievable

**Output:** A concrete architecture, file structure, implementation order, and a recommended challenging scenario with justification. Don't write code yet — agree on the design first.
