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


class SDNController(app_manager.RyuApp):
    """Ryu application that monitors topology and manages flow rules dynamically."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SDNController, self).__init__(*args, **kwargs)
        self.topology = nx.Graph()
        self.datapaths = {}
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

        self._update_topology()
        self._recompute_paths()

    # ---- Topology Management ----

    def _update_topology(self):
        """Rebuild the topology graph from LLDP discoveries."""
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
        """Recompute shortest paths and install flows on all switches."""
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

    def _mac_to_dpid(self, mac):
        """Find which switch port a MAC address is connected to."""
        host_list = get_host(self)
        for h in host_list:
            if h.mac == mac:
                for port in h.port:
                    return port.dpid
        return None

    def _install_path_flows(self, path, src_mac, dst_mac):
        """Install flow rules along a path to forward traffic between two hosts."""
        for i in range(len(path) - 1):
            src_dpid = path[i]
            dst_dpid = path[i + 1]

            if src_dpid not in self.datapaths:
                continue

            datapath = self.datapaths[src_dpid]
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser

            # Get output port for the next hop
            if self.topology.has_edge(src_dpid, dst_dpid):
                out_port = self.topology[src_dpid][dst_dpid]['src_port']

                match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
                actions = [parser.OFPActionOutput(out_port)]
                self._add_flow(datapath, 1, match, actions)

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
        """Process flow stats replies for congestion detection."""
        body = ev.msg.body
        for stat in body:
            packet_count = stat.packet_count
            if packet_count > 10000:
                self.logger.warning("Congestion detected: flow %s has %d packets",
                                    stat.match, packet_count)
