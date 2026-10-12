"""
Controller reroute regressions — runnable without Mininet or Ryu installed.

Ryu is stubbed in sys.modules before import. These tests pin the three bugs
that broke the link-failure demo on the VM:
  1. _mac_to_dpid iterated Host.port as a list ("TypeError: 'Port' object is
     not iterable"), killing every port-event reroute.
  2. _update_topology only ever added edges, so a dead link stayed in the
     graph and recomputes kept routing through it.
  3. _recompute_paths never deleted old flows, leaving duplicates pointing at
     the dead port next to the new ones.
"""

import logging
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

nx = pytest.importorskip('networkx')


def _install_ryu_stubs():
    if 'ryu' in sys.modules:
        return
    ryu = types.ModuleType('ryu')

    base = types.ModuleType('ryu.base')
    app_manager = types.ModuleType('ryu.base.app_manager')

    class RyuApp:
        def __init__(self, *args, **kwargs):
            self.logger = logging.getLogger('test-ryu')

    app_manager.RyuApp = RyuApp

    controller = types.ModuleType('ryu.controller')
    ofp_event = types.ModuleType('ryu.controller.ofp_event')
    for name in ('EventOFPSwitchFeatures', 'EventOFPPortStatus',
                 'EventOFPPacketIn', 'EventOFPFlowStatsReply'):
        setattr(ofp_event, name, type(name, (), {}))

    handler = types.ModuleType('ryu.controller.handler')
    handler.CONFIG_DISPATCHER = 'config'
    handler.MAIN_DISPATCHER = 'main'

    def set_ev_cls(*args, **kwargs):
        def deco(fn):
            return fn
        return deco

    handler.set_ev_cls = set_ev_cls

    ofproto = types.ModuleType('ryu.ofproto')
    ofproto_v1_3 = types.ModuleType('ryu.ofproto.ofproto_v1_3')
    ofproto_v1_3.OFP_VERSION = 4

    lib = types.ModuleType('ryu.lib')
    hub = types.ModuleType('ryu.lib.hub')
    hub.spawn = lambda *a, **k: None
    hub.sleep = lambda *a, **k: None

    packet_mod = types.ModuleType('ryu.lib.packet')
    packet_mod.packet = types.SimpleNamespace(Packet=object)
    packet_mod.ethernet = types.SimpleNamespace(ethernet=object)
    packet_mod.ether_types = types.SimpleNamespace(ETH_TYPE_LLDP=0x88CC)

    topology = types.ModuleType('ryu.topology')
    topo_api = types.ModuleType('ryu.topology.api')
    topo_api.get_switch = lambda app: []
    topo_api.get_link = lambda app: []
    topo_api.get_host = lambda app: []

    sys.modules.update({
        'ryu': ryu,
        'ryu.base': base,
        'ryu.base.app_manager': app_manager,
        'ryu.controller': controller,
        'ryu.controller.ofp_event': ofp_event,
        'ryu.controller.handler': handler,
        'ryu.ofproto': ofproto,
        'ryu.ofproto.ofproto_v1_3': ofproto_v1_3,
        'ryu.lib': lib,
        'ryu.lib.hub': hub,
        'ryu.lib.packet': packet_mod,
        'ryu.topology': topology,
        'ryu.topology.api': topo_api,
    })


_install_ryu_stubs()

import importlib.util

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'controller', 'sdn_controller.py')


def _load_controller():
    spec = importlib.util.spec_from_file_location('sdn_controller_under_test', SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakePort:
    def __init__(self, dpid, port_no=1):
        self.dpid = dpid
        self.port_no = port_no


class FakeHost:
    def __init__(self, mac, port):
        self.mac = mac
        self.port = port


class FakeEnd:
    def __init__(self, dpid, port_no):
        self.dpid = dpid
        self.port_no = port_no


class FakeLink:
    def __init__(self, src, dst):
        self.src = src
        self.dst = dst


class FakeDp:
    def __init__(self, dpid):
        self.id = dpid


class FakeSwitch:
    def __init__(self, dpid):
        self.dp = FakeDp(dpid)


class FakeProto:
    OFPFC_DELETE = 10
    OFPP_ANY = 0xFFFFFFFF
    OFPG_ANY = 0xFFFFFFFF
    OFPIT_APPLY_ACTIONS = 4


class FakeParser:
    def OFPMatch(self, **kwargs):
        return ('match', tuple(sorted(kwargs.items())))

    def OFPActionOutput(self, port):
        return ('out', port)

    def OFPInstructionActions(self, _type, actions):
        return ('inst', actions)

    def OFPFlowMod(self, datapath, command=None, out_port=None,
                   out_group=None, priority=None, match=None,
                   instructions=None, **kwargs):
        return {'dp': datapath.id, 'command': command,
                'priority': priority, 'match': match}


class FakeDatapath:
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = FakeProto()
        self.ofproto_parser = FakeParser()
        self.sent = []

    def send_msg(self, msg):
        self.sent.append(msg)


@pytest.fixture()
def ctrl():
    mod = _load_controller()
    c = mod.SDNController()
    return mod, c


def test_mac_to_dpid_single_port_object(ctrl, monkeypatch):
    """The VM crash: Host.port is one Port, not a list. Must not raise."""
    mod, c = ctrl
    monkeypatch.setattr(mod, 'get_host',
                        lambda app: [FakeHost('aa:bb:cc:dd:ee:01', FakePort(7))])
    assert c._mac_to_dpid('aa:bb:cc:dd:ee:01') == 7


def test_mac_to_dpid_list_shape_and_miss(ctrl, monkeypatch):
    mod, c = ctrl
    monkeypatch.setattr(mod, 'get_host', lambda app: [
        FakeHost('aa:bb:cc:dd:ee:01', [FakePort(4)]),
        FakeHost('aa:bb:cc:dd:ee:02', None),
    ])
    assert c._mac_to_dpid('aa:bb:cc:dd:ee:01') == 4
    assert c._mac_to_dpid('aa:bb:cc:dd:ee:02') is None
    assert c._mac_to_dpid('missing') is None


def test_update_topology_drops_dead_links(ctrl, monkeypatch):
    """A link that disappears from discovery must leave the graph."""
    mod, c = ctrl
    c.topology.add_edge(1, 2, src_port=10, dst_port=20, weight=1)
    monkeypatch.setattr(mod, 'get_switch', lambda app: [FakeSwitch(1), FakeSwitch(2)])
    monkeypatch.setattr(mod, 'get_link', lambda app: [])
    c._update_topology()
    assert not c.topology.has_edge(1, 2)
    assert set(c.topology.nodes) == {1, 2}


def test_recompute_replaces_stale_flows(ctrl, monkeypatch):
    """Reroute deletes old path flows before installing the new ones."""
    mod, c = ctrl
    dp1, dp2, dp3 = FakeDatapath(1), FakeDatapath(2), FakeDatapath(3)
    c.datapaths = {1: dp1, 2: dp2, 3: dp3}
    hosts = [FakeHost('00:00:00:00:00:0A', FakePort(1)),
             FakeHost('00:00:00:00:00:0B', FakePort(3))]
    monkeypatch.setattr(mod, 'get_host', lambda app: hosts)
    monkeypatch.setattr(mod, 'get_switch',
                        lambda app: [FakeSwitch(1), FakeSwitch(2), FakeSwitch(3)])

    links_v1 = [FakeLink(FakeEnd(1, 11), FakeEnd(2, 21)),
                FakeLink(FakeEnd(2, 22), FakeEnd(3, 32))]
    monkeypatch.setattr(mod, 'get_link', lambda app: list(links_v1))
    c._update_topology()
    c._recompute_paths()
    assert c.installed_flows, 'first compute installed nothing'
    assert all(m.get('command') is None for m in dp1.sent)

    # Link 1-2 dies; only a direct 1-3 edge remains.
    links_v2 = [FakeLink(FakeEnd(1, 13), FakeEnd(3, 33))]
    monkeypatch.setattr(mod, 'get_link', lambda app: list(links_v2))
    c._update_topology()
    c._recompute_paths()

    deletes = [m for dp in (dp1, dp2, dp3) for m in dp.sent
               if m.get('command') == FakeProto.OFPFC_DELETE]
    assert deletes, 'reroute installed new flows without deleting the stale ones'
    assert all(dpid != 2 for dpid, _s, _d in c.installed_flows)


def test_congestion_warns_once_per_flow(ctrl):
    """A heavy transfer re-trips the packet threshold every poll; the log must
    show each flow once instead of burying the Link lines in repeats."""
    import types
    from unittest.mock import MagicMock
    mod, c = ctrl
    c.logger = MagicMock()
    heavy_a = types.SimpleNamespace(priority=1, packet_count=20000, match='flow-A')
    heavy_b = types.SimpleNamespace(priority=1, packet_count=30000, match='flow-B')
    table_miss = types.SimpleNamespace(priority=0, packet_count=999999, match='miss')
    ev = types.SimpleNamespace(
        msg=types.SimpleNamespace(body=[heavy_a, heavy_b, table_miss]))
    c.flow_stats_reply_handler(ev)
    c.flow_stats_reply_handler(ev)
    assert c.logger.warning.call_count == 2  # A + B once each; miss skipped
