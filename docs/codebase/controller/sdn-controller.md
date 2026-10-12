# `controller/sdn_controller.py` — Ryu app: paths + failure recovery

A Ryu application (`SDNController`) that learns the mesh from LLDP discovery
and installs Dijkstra shortest-path flows, replacing them when links change.

## State

- `topology` — `networkx.Graph` of switch DPIDs; edges carry
  `src_port/dst_port/weight`.
- `datapaths` — switch ID → OpenFlow datapath (for sending flow mods).
- `mac_to_port` — learned `(dpid, mac) → port` from packet-ins.
- `installed_flows` — `(dpid, src_mac, dst_mac)` tuples *this controller*
  installed, so a recompute can delete the stale ones first.

## Handlers

- `switch_features_handler` — on switch connect: remember datapath, install
  table-miss (priority 0 → send to controller).
- `packet_in_handler` — ignores LLDP (discovery traffic) and empty packets;
  learns `src` MAC → `in_port`; floods unknown destinations, and installs a
  priority-1 exact-match flow once the destination port is known; forwards via
  packet-out (with buffer id handling).
- `port_status_handler` — on link up/down: logs, then `_update_topology()` +
  `_recompute_paths()` inside try/except that **logs loudly** instead of dying
  silently (a dead handler = traffic kept flowing into a dead port).
- `flow_stats_reply_handler` — warns on flows over 10k packets ("congestion"),
  **skipping priority-0 table-miss**: the miss rule counts *every* unmatched
  packet, so including it buried real events in spam.

## Core logic

- `_update_topology()` — **clears and rebuilds** the graph from
  `get_switch()`/`get_link()`. The clear is the fix: without it a dead link's
  edge survived forever and every "reroute" kept using the failed link.
- `_recompute_paths()` — deletes installed flows first, then for every host
  pair resolves DPIDs and installs `nx.shortest_path`. Each pair is guarded so
  one bad pair can't abort the rest.
- `_mac_to_dpid(mac)` — finds a host's switch. Ryu's `Host.port` is a **single
  `Port` object, not a list** — iterating it crashed every port event with
  `TypeError: 'Port' object is not iterable` and killed all rerouting. Accepts
  both shapes defensively.
- `_install_path_flows(path, src, dst)` — priority-1 `eth_src/eth_dst` matches
  with the next-hop out-port, tracked in `installed_flows`.
- `_remove_installed_flows()` — `OFPFC_DELETE` mods for tracked flows. Without
  this, a reroute leaves the old entry (same match, dead out-port) next to the
  new one.
- `_monitor()` — polls flow stats every 5 s (`hub.spawn` background thread).

## Pitfalls (hit during build)

- The three reroute bugs above arrived together and presented as one symptom:
  "link failure does nothing, controller spams tracebacks."
- STP (enabled in `topology.py`) still needs ~30–60 s to reconverge after a
  link event; transfers in that window fail *correctly* — that is switch
  physics, not a controller bug.
