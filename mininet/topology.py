"""
Mininet topology: 4 OVS switches in full mesh, 4 hosts.
h1, h2 = clients (senders)
h3, h4 = backup servers (receivers)

The CLI runs in a background thread so the main thread can service link
up/down requests written by the dashboard into shared_files/. Without this the
only way to break a link was typing into the Mininet prompt by hand, and the
dashboard's "Trigger Link Failure" button could not affect the real network.
"""

import os
import json
import threading
import time
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

from linkctl import apply_link_state, serve_link_requests, write_json_atomic


def create_topology():
    """Create and start the Mininet network topology."""
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6633)

    s1 = net.addSwitch('s1')
    s2 = net.addSwitch('s2')
    s3 = net.addSwitch('s3')
    s4 = net.addSwitch('s4')

    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')

    net.addLink(h1, s1)
    net.addLink(h2, s3)
    net.addLink(h3, s2)
    net.addLink(h4, s4)

    net.addLink(s1, s2)
    net.addLink(s1, s3)
    net.addLink(s1, s4)
    net.addLink(s2, s3)
    net.addLink(s2, s4)
    net.addLink(s3, s4)

    net.start()

    # Enable STP before any traffic flows. The mesh has loops, and a controller
    # that floods broadcast (ARP) replicates each frame around every loop, which
    # snowballs into a broadcast storm: table-miss counts hit the hundreds of
    # thousands, ARP never resolves, and connect() fails with
    # "[Errno 113] No route to host". STP gives the mesh a loop-free tree for
    # broadcast while leaving the controller free to pick unicast paths.
    for sw in net.switches:
        sw.cmd('ovs-vsctl set bridge', sw, 'stp-enable=true')
    print('[TOPOLOGY] STP enabled on all switches (mesh is looped; without it ARP storms)', flush=True)

    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    stored_dir = os.path.join(base_dir, 'stored_files')
    shared_dir = os.path.join(base_dir, 'shared_files')
    os.makedirs(shared_dir, exist_ok=True)

    for h in ['h3', 'h4']:
        host_dir = os.path.join(stored_dir, h)
        os.makedirs(host_dir, exist_ok=True)
        node = net.get(h)
        node.cmd('mkdir -p /tmp/backup')
        node.cmd(f'mount --bind {host_dir} /tmp/backup')

    # shared_files/ -> /tmp/shared/ on every host, for bridge + link requests.
    for h in ['h1', 'h2', 'h3', 'h4']:
        node = net.get(h)
        node.cmd('mkdir -p /tmp/shared')
        node.cmd(f'mount --bind {shared_dir} /tmp/shared')

    # Start the bridge on h1 and the backup servers on h3/h4 automatically.
    # Servers are children of the Mininet namespace: they die when Mininet does,
    # so starting them here means one `sudo python3 mininet/topology.py` brings
    # the whole system up instead of leaving ports silently unbound.
    server_src = os.path.join(base_dir, 'server', 'server.py')
    bridge_src = os.path.join(base_dir, 'mininet', 'bridge.py')

    # popen() — not cmd('... &') — is what puts a long-running process into the
    # node's own network namespace. With cmd(), the bridge ended up in the host
    # namespace where 10.0.0.0/24 is not routable, so every connect() failed
    # with "[Errno 101] Network is unreachable" even though pingall was clean.
    bridge_env = dict(os.environ, PYTHONUNBUFFERED='1')
    net.get('h1').popen([sys.executable, bridge_src],
                        stdout=open('/tmp/bridge.log', 'w'),
                        stderr=subprocess.STDOUT,
                        env=bridge_env)

    # h3 and h4 need the repo copied into their own namespaces.
    # Both listen on 9000: separate IPs, matching the client's default
    # --servers 10.0.0.3:9000 10.0.0.4:9000 so failover just works.
    for host, ip in [('h3', '10.0.0.3'), ('h4', '10.0.0.4')]:
        node = net.get(host)
        node.cmd(f'mkdir -p /tmp/src && cp -r {base_dir}/server {base_dir}/common /tmp/src/')
        node.popen([sys.executable, f'/tmp/src/server/server.py',
                    '--host', ip, '--port', '9000',
                    '--storage', '/tmp/backup'],
                   stdout=open(f'/tmp/server_{host}.log', 'w'),
                   stderr=subprocess.STDOUT,
                   env=dict(os.environ, PYTHONUNBUFFERED='1'))
        print(f'[TOPOLOGY] Backup server starting on {host} ({ip}:9000) '
              f'-> /tmp/backup  [log: /tmp/server_{host}.log]')

    print('[TOPOLOGY] Bridge on h1 launched via popen() in h1 namespace')
    print('[TOPOLOGY] Bridge log: /tmp/bridge.log  (check its IP line below)')

    print('[TOPOLOGY] Mininet started: 4 switches, 4 hosts')
    print('[TOPOLOGY] stored_files/ bind-mounted to h3,h4:/tmp/backup')
    print('[TOPOLOGY] shared_files/ bind-mounted to all hosts at /tmp/shared/')
    print('[TOPOLOGY] Bridge on h1; servers on h3:9000 and h4:9000')
    print('[TOPOLOGY] Give it ~3s to bind, then check: mininet> pingall')
    print('[TOPOLOGY] Type "exit" in the prompt to stop')

    # The interactive CLI owns this thread; the main thread services link
    # requests so the dashboard can actually break the network.
    request_file = os.path.join(shared_dir, 'link_request.json')
    result_file = os.path.join(shared_dir, 'link_result.json')
    for p in (request_file, result_file):
        if os.path.exists(p):
            os.unlink(p)

    cli_thread = threading.Thread(target=CLI, args=(net,), daemon=True)
    cli_thread.start()
    try:
        # Serve link requests until the user leaves the interactive prompt.
        serve_link_requests(net, request_file, result_file,
                            should_continue=lambda: cli_thread.is_alive())
    finally:
        cli_thread.join(timeout=2)
        net.stop()
        print('[TOPOLOGY] Mininet stopped')


if __name__ == '__main__':
    setLogLevel('info')
    create_topology()
