"""
Server process control for the Mininet topology.

Kept separate from topology.py so it can be tested without Mininet: everything
operates on a net-like object exposing .get(host) and the Popen handles from
node.popen(). Kill by handle, never by pkill: Mininet hosts share the host PID
namespace, so a bare `pkill -f server.py` can kill both backup servers.
"""

import json
import os
import socket
import subprocess
import sys
import time

from linkctl import write_json_atomic

HOST_IP = {'h3': '10.0.0.3', 'h4': '10.0.0.4'}
SERVER_SCRIPT = '/tmp/src/server/server.py'
SERVER_PORT = 9000


def start_server(net, host):
    """Start the backup server in the host's namespace. Returns the Popen."""
    node = net.get(host)
    log = open(f'/tmp/server_{host}.log', 'a')
    try:
        proc = node.popen(
            [sys.executable, SERVER_SCRIPT, '--host', HOST_IP[host],
             '--port', str(SERVER_PORT), '--storage', '/tmp/backup'],
            stdout=log, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUNBUFFERED='1'))
    finally:
        log.close()
    return proc


def _can_connect(host, timeout=1):
    try:
        with socket.create_connection((HOST_IP.get(host, host), SERVER_PORT),
                                      timeout=timeout):
            return True
    except OSError:
        return False


def confirm_down(host, attempts=2, interval=None, log=print):
    """Probe twice, `interval` apart. True when both probes fail."""
    if interval is None:
        interval = float(os.environ.get('SERVER_PROBE_INTERVAL', '5'))
    for i in range(1, attempts + 1):
        log(f'[SERVERCTL] Polling {host} ({i}/{attempts})')
        if _can_connect(host):
            return False
        if i < attempts:
            time.sleep(interval)
    return True


def apply_server_state(net, procs, host, action, interval=None):
    """Kill or start one server. Returns (ok, message, state, confirmed)."""
    if host not in HOST_IP:
        return False, f'unknown host {host!r}', None, False
    if action not in ('down', 'up'):
        return False, f'unknown action {action!r}', None, False

    proc = procs.get(host)
    alive = proc is not None and proc.poll() is None

    if action == 'down':
        if not alive:
            return True, f'{host} already down', 'down', True
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        confirmed = confirm_down(host, interval=interval)
        if not confirmed:
            return False, f'{host} still answering', 'up', False
        return True, f'{host} down', 'down', True

    if alive:
        return True, f'{host} already up', 'up', True
    procs[host] = start_server(net, host)
    return True, f'{host} up', 'up', True


def server_status(procs):
    out = {}
    for host in HOST_IP:
        proc = procs.get(host)
        out[host] = {
            'alive': proc is not None and proc.poll() is None,
            'pid': proc.pid if proc is not None else None,
            'since': None,
        }
    return out


def serve_server_requests(net, procs, request_file, result_file, status_file,
                          poll=0.3, should_continue=None, interval=None):
    """Poll for kill/restart requests and keep server_status.json fresh."""
    prev = server_status(procs)
    while True:
        current = server_status(procs)
        for host in HOST_IP:
            if prev[host]['alive'] and not current[host]['alive']:
                print(f'[SERVERCTL] {host} exited unexpectedly', flush=True)
        prev = current
        status = {'checked_at': time.strftime('%H:%M:%S')}
        status.update(current)
        write_json_atomic(status_file, status)

        if os.path.isfile(request_file):
            try:
                with open(request_file) as f:
                    req = json.load(f)
                os.unlink(request_file)

                host, action = req.get('host'), req.get('action')
                ok, message, state, confirmed = apply_server_state(
                    net, procs, host, action, interval=interval)
                print(f'[SERVERCTL] {host} {action}: {message}', flush=True)
                prev = server_status(procs)
                result = {
                    'host': host, 'action': action, 'applied': ok,
                    'state': state, 'confirmed': confirmed,
                    'request_id': req.get('request_id'),
                    'message': message,
                    'at': time.strftime('%H:%M:%S'),
                }
                if not ok:
                    result['error'] = message
                write_json_atomic(result_file, result)
            except Exception as e:
                print(f'[SERVERCTL] request error: {e}', flush=True)

        if should_continue is not None and not should_continue():
            break
        time.sleep(poll)
