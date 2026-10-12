# `mininet/topology.py` — the virtual network + process launcher

Builds the 4-switch full mesh with 4 hosts, then — critically — launches the
whole system (bridge + both servers) so one command brings everything up.

## Topology

- Switches `s1..s4` (OVS, `RemoteController` at 127.0.0.1:6633), hosts
  `h1=10.0.0.1` … `h4=10.0.0.4/24`. h1/h2 are clients, h3/h4 are servers.
- Full mesh between switches (6 links) + one access link per host.
- `net.start()`, then **STP enabled on every switch**: the mesh has loops and
  the controller floods broadcast (ARP), which replicates around every loop
  into a broadcast storm — table-miss counters in the hundreds of thousands,
  ARP never resolving, `connect()` failing with `Errno 113`. STP gives
  broadcast a loop-free tree while the controller picks unicast paths.

## Mounts (how files cross the namespace boundary)

- `stored_files/h3|h4` → bind-mounted at `h3|h4:/tmp/backup`: server writes
  land in the repo, which is what the dashboard lists.
- `shared_files/` → bind-mounted at `/tmp/shared` on **all** hosts: the JSON
  side-channel (requests, results, heartbeats, link files) both ways.

## Launches (the namespace lesson)

- Bridge on h1 and servers on h3/h4 via **`node.popen()`**, not
  `node.cmd('… &')`: only `popen()` attaches the long-running process to the
  host's network namespace. With `cmd`, the bridge ran in the host namespace
  where `10.0.0.0/24` is unroutable — `pingall` clean, yet every connect died
  with `Errno 101`. Servers get the repo's `server/`+`common/` copied to
  `/tmp/src` first (separate namespaces, separate filesystems) and listen on
  their own IP:9000, matching the client's default server list.
- **Stale cleanup before launching**: `popen` children survive `net.stop()`,
  so restarts accumulate duplicate bridges/servers. Startup `pkill`s both
  patterns, then **unlinks `bridge.lock`** — pkill orphans the lock seconds
  fresh, and without the unlink the new bridge assumes a live rival and exits,
  leaving no bridge at all (a real startup race fixed during build).

## Interactive service

- `request_file`/`link_result.json` cleared at boot (no stale confirmations).
- CLI runs in a **background thread** so the main thread can serve link
  requests (`serve_link_requests`) — the dashboard's Trigger/Restore buttons
  break/mend the *real* mesh, emitting the port-status events the controller
  reacts to. `finally`: join CLI thread, `net.stop()`.
- `create_topology()` wraps everything with failure cleanup
  (`mininet.clean.cleanup`): an exception after `net.start()` used to leak
  every interface, and the next run died in `addLink` with
  `RTNETLINK answers: File exists`. One broken run can no longer poison the next.
