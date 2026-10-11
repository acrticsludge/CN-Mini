"""
Ryu SDN Controller — dynamic path selection and failure recovery.

Mechanisms implemented:
  1. Routing       — Dijkstra shortest-path for all flow installations
  2. Rerouting     — Link failure triggers path recomputation and new flow rules
  3. Monitoring    — Flow stats polled every 5 seconds
  4. Recovery      — Link up event restores optimal path
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.topology.api import get_switch, get_link, get_host
import networkx as nx
from ryu.lib.packet import packet, ethernet, ether_types

class SDNController(app_manager.RyuApp):
    """Ryu application that monitors topology and manages flow rules dynamically."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SDNController, self).__init__(*args, **kwargs)
        self.topology = nx.Graph()
        self.datapaths = {}
        self.mac_to_port = {}
        # (dpid, src_mac, dst_mac) tuples this controller installed, so a
        # recompute can delete the stale ones first. Without this, a reroute
        # leaves the old flow (same match, dead out_port) next to the new one.
        self.installed_flows = set()
        self.monitor_thread = hub.spawn(self._monitor)

    # ---- Switch Features ----

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Install table-miss flow entry on switch connect."""
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        self._install_table_miss(datapath)
        self.logger.info("Switch %d connected", datapath.id)

    def _install_table_miss(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, 0, match, actions)

    # ---- Port Status Events ----

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """Handle link up/down events from switches."""
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        reason = msg.reason

        if reason == ofproto.OFPPR_MODIFY:
            port_state = msg.desc.state
            if port_state & ofproto.OFPPS_LINK_DOWN:
                self.logger.info("Link DOWN detected on switch %d port %s",
                                 datapath.id, msg.desc.port_no)
            else:
                self.logger.info("Link UP detected on switch %d port %s",
                                 datapath.id, msg.desc.port_no)

        # A handler exception in Ryu is only logged; the recompute is then
        # silently skipped and the network keeps forwarding into the dead port.
        # Log loudly instead so a failed reroute is visible, not silent.
        try:
            self._update_topology()
            self._recompute_paths()
        except Exception as e:
            self.logger.error("Reroute failed after port event: %s", e)

    # ---- Topology Management ----

    def _update_topology(self):
        """Rebuild the topology graph from LLDP discoveries.

        The graph is cleared first: get_link() only reports live links, so
        without a rebuild the edge for a dead link stays forever and every
        "recompute" keeps routing through the failure it was meant to avoid.
        """
        self.topology.clear()
        switch_list = get_switch(self)
        for sw in switch_list:
            if sw.dp.id not in self.datapaths:
                self.datapaths[sw.dp.id] = sw.dp
            self.topology.add_node(sw.dp.id)

        link_list = get_link(self)
        for link in link_list:
            src = link.src.dpid
            dst = link.dst.dpid
            src_port = link.src.port_no
            dst_port = link.dst.port_no
            self.topology.add_edge(src, dst,
                                   src_port=src_port,
                                   dst_port=dst_port,
                                   weight=1)

    def _recompute_paths(self):
        """Recompute shortest paths and install flows on all switches.

        Stale path flows are deleted first so a reroute replaces the old
        out_port instead of leaving a duplicate entry pointing at the dead
        link. One bad host pair must not abort the rest: each pair is guarded.
        """
        self._remove_installed_flows()
        host_list = get_host(self)
        host_macs = [h.mac for h in host_list]

        for src_mac in host_macs:
            for dst_mac in host_macs:
                if src_mac == dst_mac:
                    continue
                try:
                    src_dpid = self._mac_to_dpid(src_mac)
                    dst_dpid = self._mac_to_dpid(dst_mac)
                    if src_dpid is None or dst_dpid is None:
                        continue
                    path = nx.shortest_path(self.topology, src_dpid, dst_dpid,
                                            weight='weight')
                    self._install_path_flows(path, src_mac, dst_mac)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    self.logger.debug("No path for %s -> %s", src_mac, dst_mac)
                except Exception as e:
                    self.logger.error("Path install failed for %s -> %s: %s",
                                      src_mac, dst_mac, e)

    def _mac_to_dpid(self, mac):
        """Find which switch a MAC address is connected to.

        Ryu's Host.port is a single Port object, not a list — iterating it
        raised "TypeError: 'Port' object is not iterable" on every port event
        and killed the reroute. Accept both shapes defensively.
        """
        host_list = get_host(self)
        for h in host_list:
            if h.mac == mac:
                port = getattr(h, 'port', None)
                if port is None:
                    continue
                if isinstance(port, (list, tuple)):
                    if not port:
                        continue
                    return getattr(port[0], 'dpid', None)
                return getattr(port, 'dpid', None)
        return None

    def _install_path_flows(self, path, src_mac, dst_mac):
        """Install flow rules along a path to forward traffic between two hosts."""
        for i in range(len(path) - 1):
            src_dpid = path[i]
            dst_dpid = path[i + 1]

            if src_dpid not in self.datapaths:
                continue

            datapath = self.datapaths[src_dpid]

            # Get output port for the next hop
            if self.topology.has_edge(src_dpid, dst_dpid):
                out_port = self.topology[src_dpid][dst_dpid]['src_port']

                match = datapath.ofproto_parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
                actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
                self._add_flow(datapath, 1, match, actions)
                self.installed_flows.add((src_dpid, src_mac, dst_mac))

    def _remove_installed_flows(self):
        """Delete every path flow this controller installed.

        Called at the start of a recompute so the new paths replace the old
        ones instead of shadowing them. A missing datapath (switch gone) is
        skipped; its entry is still dropped from the set.
        """
        for src_dpid, src_mac, dst_mac in list(self.installed_flows):
            datapath = self.datapaths.get(src_dpid)
            self.installed_flows.discard((src_dpid, src_mac, dst_mac))
            if datapath is None:
                continue
            try:
                ofproto = datapath.ofproto
                parser = datapath.ofproto_parser
                match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
                mod = parser.OFPFlowMod(
                    datapath=datapath,
                    command=ofproto.OFPFC_DELETE,
                    out_port=ofproto.OFPP_ANY,
                    out_group=ofproto.OFPG_ANY,
                    match=match,
                )
                datapath.send_msg(mod)
            except Exception as e:
                self.logger.error("Flow delete failed on switch %s: %s",
                                  src_dpid, e)

    def _add_flow(self, datapath, priority, match, actions, buffer_id=None):
        """Install a flow rule on a switch."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst)
        datapath.send_msg(mod)
    # ---- Packet Forwarding ----

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None:
            return

        # Ignore LLDP packets used for topology discovery.
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src

        # Learn the source MAC address on this switch.
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        # Forward to a known destination, otherwise flood.
        out_port = self.mac_to_port[dpid].get(
            dst, ofproto.OFPP_FLOOD
        )

        actions = [parser.OFPActionOutput(out_port)]

        # Install a flow when the destination port is known.
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(
                in_port=in_port,
                eth_src=src,
                eth_dst=dst,
            )
            self._add_flow(datapath, 1, match, actions)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)


    # ---- Monitoring ----

    def _monitor(self):
        """Poll flow stats every 5 seconds to detect congestion."""
        while True:
            for dp in list(self.datapaths.values()):
                self._poll_flow_stats(dp)
            hub.sleep(5)

    def _poll_flow_stats(self, datapath):
        """Send a flow stats request to a switch."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        req = parser.OFPFlowStatsRequest(datapath, 0, ofproto.OFPTT_ALL,
                                         ofproto.OFPP_ANY, ofproto.OFPG_ANY,
                                         0, 0, match)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        """Report flows carrying unusual numbers of packets.

        Table-miss (priority 0, wildcard match) is excluded: it counts every
        packet the dataplane could not match, so it is a traffic gauge, not a
        flow. Reporting it as congestion buried the real events in log spam.
        """
        body = ev.msg.body
        for stat in body:
            if stat.priority == 0:
                continue
            packet_count = stat.packet_count
            if packet_count > 10000:
                self.logger.warning(
                    "Congestion detected: flow %s has %d packets",
                    stat.match, packet_count)
