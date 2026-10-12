# D1 Demo Script — Distributed Backup System (3 speakers, in order)

Timing: ~10 minutes + questions. Two rules for all three of you:
1. Say **"reported"** where the slides say reported, **"to be verified"** where
   they say pending/unverified. Never upgrade a maybe into a done.
2. Every short form is expanded the first time you say it — if you forget the
   expansion mid-demo, just read it off this script. Nobody will mind.

## Who says what (slides in order, 5 each)

- **Arush** — slides 1–5 (open, architecture, problems, TCP sockets, socket-flow picture)
- **Aryan** — slides 6–10 (protocol, topology picture, Mininet+SDN, reroute, integration)
- **Anubhav** — slides 11–15 (scenario, live demo, performance, tests, close)

---

## ARUSH — Slide 1: Title

> Good morning. We are Arush, Aryan and Anubhav, and our Computer Networks
> mini project is a **Distributed Backup System using TCP — Transmission
> Control Protocol — Socket Programming and SDN — Software-Defined
> Networking**. In one line: we back up files reliably over a small network
> whose paths are controlled by software, not fixed wiring. Our code is public
> at the GitHub link on screen.

## ARUSH — Slide 2: Overall System Architecture

> The system has two halves — this split runs through the whole project. On the
> **data plane** — the part that carries file data — clients h1 and h2 open TCP
> connections to backup servers h3 and h4 on port 9000, and file chunks flow
> there. On the **control plane** — the part that decides *where* data goes —
> one Ryu controller program talks OpenFlow version 1.3, which is just the
> language controllers use to program switches, to all four switches. The Flask
> dashboard on port 5000 only *watches* through REST — REST means normal web
> requests like your browser makes — it never touches a single file packet.
> The flow is: client connects, DATA goes out, ACKs — acknowledgements, meaning
> "got it" messages — come back, and Ryu learns the network and reacts when
> ports go down.

## ARUSH — Slide 3: Main Problems

> Three things go wrong in backup systems, and each maps to one thing we built.
> One: bits flip while travelling, so every 8-kilobyte chunk carries a CRC —
> Cyclic Redundancy Check, a small error-detecting code — and the server
> verifies and acknowledges each chunk. Two: connections drop mid-file, so
> instead of restarting we RESUME — continue — from the last acknowledged
> chunk, and both sides track progress. Three: network links die and traffic
> goes nowhere, so the controller recomputes shortest paths when a port-down
> event arrives and pushes new flow rules — new instruction sheets — to the
> switches.

## ARUSH — Slide 4: TCP Socket Programming

> The socket story in one walk: the client calls `connect`, the server has
> already done `bind`, `listen`, `accept` — that handshake is the famous 3-way
> handshake that opens a TCP connection. Then our own conversation on top:
> HELLO with filename and size, a ready ACK — acknowledgement — then DATA
> chunk *i* plus its CRC code, ACK *i*, repeated. On failure we send RESUME
> with the last ACK number and continue from *i+1*. `close` at the end is the
> FIN exchange — FIN is the finish flag that closes a connection.
> The one line to remember: **a TCP ACK is not our app ACK**. TCP's ACK only
> means the operating system's kernel received the bytes. Our `0x02` ACK means
> the chunk passed its CRC check **and was written to disk** — and only that
> ACK moves RESUME forward.

## ARUSH — Slide 5: Socket-flow picture

> Follow the arrows top to bottom: connect, HELLO, ready, then the chunk loop
> — DATA one way, ACK the other — and clean close at the bottom. The side
> branch is the interesting one: when the connection drops, we do *not* go
> back to the top; RESUME jumps back in mid-loop at the first missing chunk.
> That is the data half of the project — over to Aryan for the protocol and
> the network half.

## ARYAN — Slide 6: Protocol and Integrity

> Every frame — every single message — is a 21-byte header plus up to 8 KB of
> file data: a magic number `DEADBEEF` so garbage gets rejected, then type,
> index, total count, CRC code, and length. Four message types: DATA, ACK,
> RESUME, HELLO. The math that matters: a 1-megabyte file is exactly 128
> chunks of 8 KB. The server recomputes the CRC, writes the chunk, and ACKs —
> every chunk, no exceptions. Two honest limits, both on the slide: CRC catches
> accidents, not attackers — no TLS — Transport Layer Security, meaning
> encryption — and no login security yet. And resume survives restarts because
> the server's folder is bind-mounted — that means shared — into our repo, so
> half-written files are still there after a reboot.

## ARYAN — Slide 7: Topology picture

> This is our actual Mininet network — Mininet is a tool that fakes a whole
> network inside one computer. Four OVS switches — OVS is Open vSwitch, a
> software version of a network switch — wired in a full mesh, meaning every
> switch connects to every other switch: six links. h1 and h2 are clients, h3
> and h4 are servers. The command that proves it works is `pingall` — every
> host pings every other host. The mesh shape is deliberate: with only one
> path, a dead link ends the story; with six links, the controller always has
> somewhere else to send traffic.

## ARYAN — Slide 8: Mininet + SDN Controller

> Two files do all of this. `topology.py` builds the virtual network, connects
> it to the controller using OpenFlow 1.3, and shares the backup folders so
> files survive outside Mininet. `sdn_controller.py` — the SDN brain — learns
> the network using LLDP — Link Layer Discovery Protocol, which is just
> switches introducing themselves to the controller — computes Dijkstra
> shortest paths — Dijkstra is a classic shortest-path algorithm — installs
> the flow rules, handles port-up and port-down events, and checks flow
> statistics every 5 seconds. The four commands on screen are the whole
> startup: start the network, start the controller, `pingall`, and
> `dump-flows` — which prints a switch's instruction sheet so you can see the
> rules with your own eyes.

## ARYAN — Slide 9: Dynamic Routing and Failure Recovery

> Left: all links up, h1 reaches h3 through switches s1–s2. Right: the s1–s2
> link dies — the cross on the diagram. The chain is five steps: the port goes
> down, a PortStatus alert — "a port changed state" message — reaches Ryu, the
> dead link is erased from the controller's map, Dijkstra recomputes the
> shortest path, and new flow rules go to the switches. One honesty note, also
> on the slide: the alternate path shown is illustrative — real traffic may go
> via s3 or s4 instead — and we always confirm the actual path in the
> controller log. A static, hand-written flow table could never recompute like
> this; that difference is the whole point of doing it with SDN —
> Software-Defined Networking.

## ARYAN — Slide 10: Socket–SDN Integration

> The application layer — the backup program — splits files, connects,
> verifies, stores, and on error resumes or switches to the backup server h4.
> The network layer — controller plus switches — installs paths, forwards by
> the instruction sheet, and reinstalls everything when a PortStatus alert
> arrives. And here is the exam answer, word for word: **the controller never
> sees a single file chunk — there is no direct wire between controller and
> app; they interact only through the network itself.** Recovery comes in three
> independent layers: Ryu reroutes around dead links, TCP — Transmission
> Control Protocol — retransmits lost bytes, and the app resumes or fails over
> to the other server. Any one layer can save a transfer the others lost. Over
> to Anubhav for the failure demo and the results.

## ANUBHAV — Slide 11: Challenging Scenario

> Our challenge test: a 1 MB file, 128 chunks, sent from h1 to h3 over s1–s2 —
> then we kill s1–s2 in the middle of the transfer, either from the dashboard
> button or the Mininet command line. The chain fires: port alert, recompute,
> new flows. Reported outcome: traffic flowing again in 1.3 seconds, all 128
> chunks received. And now the careful part — these are **four separate
> claims**: network connectivity back in 1.3 s is *reported*; TCP keeping the
> connection alive is *unverified* — our notes credit TCP retransmission and we
> still have to confirm it in the client log; the app-level RESUME being used
> is *unverified* for the same reason; file integrity at 128 out of 128 is
> reported, and I re-check it live with `sha256sum` — a fingerprint command
> that proves two files are byte-identical — in the demo, which starts now.

## ANUBHAV — Slide 12: LIVE DEMO (Anubhav drives, Aryan narrates the network)

1. `mininet> pingall` — all reachable. (Aryan: "Baseline is green.")
2. Dashboard: pick the 1 MB file, **Start Transfer** — point at the progress bar.
3. Mid-transfer: **Trigger Link Failure** — (Aryan, watching the controller
   terminal: "Port-down alert arrived — recomputing — new flows installed.")
4. Transfer completes 128/128 → `sha256sum` the original and the stored copy:
   identical fingerprints.
5. `sh ovs-ofctl -O OpenFlow13 dump-flows s1` — print the new path, on screen.
>
> If anything stalls: say what you see ("holding at N chunks while the
> controller recomputes") — narrating a hiccup scores more than hiding it.

## ANUBHAV — Slide 13: Performance and Baseline

> The 1.3 seconds split into three stages: 0.3 to *notice* the dead link, 0.2
> to *recompute* paths, 0.8 until traffic actually flows — the physical
> network settling down is the slow part, not our code. Against the static,
> non-smart baseline: same speed when healthy, 842 versus 850 kilobytes per
> second — but after a failure, our dynamic setup recovers in 1.3 s while the
> static one fails completely with every chunk lost. Stated honestly: the
> static result is pass-or-fail, so a fair speed comparison is impossible, and
> bigger files and loss percentages are still to be measured.

## ANUBHAV — Slide 14: Testing and Results

> Walk the table row by row, and read the status column *exactly as written* —
> most unit-test rows say *pending runs*, the Mininet rows say *not documented
> yet*, and only the live reroute row says *reported, to re-verify*. That
> column is our work plan for phase 2, not a weakness: every test case already
> defines its starting condition and its expected result before we run it.

## ANUBHAV — Slide 15: Conclusion + close

> What we demonstrated: TCP sockets with a custom message protocol, CRC codes
> with per-chunk ACKs and resume, Mininet plus OpenFlow 1.3 plus the Ryu
> controller, and Dijkstra rerouting on link failure — strongest result, 1.3
> seconds recovery with 128 of 128 chunks, reported. Limitations, plainly:
> app and controller are not directly connected, controller tests are at the
> map level only, small file with few runs, CRC only with no security, static
> baseline speed unmeasured, and failover to one server rather than true
> copying to both. Each limitation maps to a future item: bigger test sweeps,
> SHA — Secure Hash Algorithm — fingerprinting plus TLS encryption, a REST —
> web-request — hook so the app can talk to the controller, choosing paths by
> measured bandwidth, and storing every chunk on both servers. Code is on
> GitHub — thank you, and we welcome questions.

---

## Likely teacher questions (one-line answers, expansions included)

- **Why TCP — Transmission Control Protocol — and not UDP?** — TCP gives
  ordered, reliable bytes plus free retransmission; our ACKs add
  storage-level certainty on top. (Arush)
- **TCP ACK versus your ACK?** — Kernel receipt versus CRC-checked-and-stored;
  only ours moves resume forward. (Arush)
- **Why 8 KB chunks?** — Small enough to resume cheaply, big enough to avoid
  drowning in per-chunk ACKs; 1 MB ÷ 8 KB = 128 frames. (Aryan)
- **CRC — Cyclic Redundancy Check — versus SHA?** — CRC is fast for per-chunk
  checks; it detects corruption, not attackers (a listed limitation). (Aryan)
- **Server h3 dies mid-transfer?** — The client tries servers in order, so h4
  takes over (failover). (Aryan — or Anubhav, whoever gets asked)
- **How do app and controller interact?** — They don't, directly — only
  through the network itself (slide 10). (Aryan)
- **The controller program itself dies?** — Switches keep their last
  instructions; new events go unhandled — an honest gap, listed as future work.
- **Static baseline numbers?** — Pass-or-fail outcome, so no fair speed
  comparison — stated on slide 13. (Anubhav)
- **Prove the reroute path?** — Controller log plus `dump-flows`, live in
  the demo. (Anubhav)
