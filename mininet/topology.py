"""
Mininet topology: 4 OVS switches in full mesh, 4 hosts.
h1, h2 = clients (senders)
h3, h4 = backup servers (receivers)

Host directory stored_files/ is bind-mounted into server hosts at /tmp/backup/
so files persist across Mininet restarts.
"""

import os
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel


def create_topology():
    """Create and start the Mininet network topology."""
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink)
    net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6633,
    )

    # Add switches (OpenFlow 1.3)
    s1 = net.addSwitch('s1')
    s2 = net.addSwitch('s2')
    s3 = net.addSwitch('s3')
    s4 = net.addSwitch('s4')

    # Add hosts
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')

    # Host-switch links
    net.addLink(h1, s1)
    net.addLink(h2, s3)
    net.addLink(h3, s2)
    net.addLink(h4, s4)

    # Switch-switch links (full mesh for redundancy)
    net.addLink(s1, s2)
    net.addLink(s1, s3)
    net.addLink(s1, s4)

    net.start()

    # Create stored_files directories and bind-mount into server hosts
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    stored_dir = os.path.join(base_dir, 'stored_files')

    for h in ['h3', 'h4']:
        host_dir = os.path.join(stored_dir, h)
        os.makedirs(host_dir, exist_ok=True)
        node = net.get(h)
        node.cmd('mkdir -p /tmp/backup')
        node.cmd(f'mount --bind {host_dir} /tmp/backup')

    print("[TOPOLOGY] Mininet started: 4 switches, 4 hosts")
    print("[TOPOLOGY] stored_files/ bind-mounted to h3:/tmp/backup and h4:/tmp/backup")
    print("[TOPOLOGY] Type 'exit' or 'quit' to stop")

    # Open the Mininet CLI for interactive control
    CLI(net)

    # Cleanup on exit
    net.stop()
    print("[TOPOLOGY] Mininet stopped")


if __name__ == '__main__':
    setLogLevel('info')
    create_topology()
