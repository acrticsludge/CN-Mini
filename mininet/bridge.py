"""
Bridge process — runs on h1 inside Mininet.

Watches shared_files/transfer_request.json (inside Mininet: /tmp/shared/).
When the dashboard writes a request, runs the TCP client against the backup
servers, streams its progress into transfer_progress.json + transfer_events.json,
and writes transfer_result.json when it finishes.

A transfer_cancel.json makes it kill a running client, so Stop really stops.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
CLIENT_SCRIPT = os.path.join(BASE_DIR, 'client', 'client.py')

# Inside Mininet, shared_files/ is bind-mounted at /tmp/shared/.
# Outside Mininet (host testing), fall back to the repo directory.
SHARED_DIR = os.environ.get(
    'SHARED_DIR',
    '/tmp/shared' if os.path.isdir('/tmp/shared') else os.path.join(BASE_DIR, 'shared_files')
)
REQUEST_FILE = os.path.join(SHARED_DIR, 'transfer_request.json')
RESULT_FILE = os.path.join(SHARED_DIR, 'transfer_result.json')
PROGRESS_FILE = os.path.join(SHARED_DIR, 'transfer_progress.json')
EVENTS_FILE = os.path.join(SHARED_DIR, 'transfer_events.json')
CANCEL_FILE = os.path.join(SHARED_DIR, 'transfer_cancel.json')
# Singleton lock: every `sudo python3 mininet/topology.py` run spawns a bridge,
# and nothing kills the old one. Two bridges on the same request file means
# duplicate transfers and interleaved progress/result writes — "stored but the
# UI says failed". The second bridge exits loudly instead of joining in.
LOCK_FILE = os.path.join(SHARED_DIR, 'bridge.lock')
# Liveness signal for the dashboard: pid, start time and last poll, so the UI
# can show whether the bridge is actually alive instead of guessing.
ALIVE_FILE = os.path.join(SHARED_DIR, 'bridge_alive.json')

SERVERS = os.environ.get('BACKUP_SERVERS', '10.0.0.3:9000,10.0.0.4:9000').split(',')
POLL_INTERVAL = float(os.environ.get('BRIDGE_POLL_INTERVAL', '0.5'))
CLIENT_TIMEOUT = float(os.environ.get('BRIDGE_TIMEOUT', '300'))
# Per-ACK wait handed to the client: must cover STP reconvergence after a link
# failure, or every mid-transfer failure becomes a failed run instead of a stall.
TRANSFER_TIMEOUT = os.environ.get('BRIDGE_TRANSFER_TIMEOUT', '60')
# Two probes, five seconds apart, before a failure is confirmed.
PROBE_INTERVAL = float(os.environ.get('SERVER_PROBE_INTERVAL', '5'))
# Heartbeats must tick on a fixed interval, not only when the client prints.
HEARTBEAT_INTERVAL = float(os.environ.get('BRIDGE_HEARTBEAT_INTERVAL', '2'))
# The watcher keeps server_status.json fresh every 0.3 s; older than this is
# unknown, not proof either way.
SERVER_STATUS_FILE = os.path.join(SHARED_DIR, 'server_status.json')
SERVER_STATUS_MAX_AGE = 10.0
# A reachable-server path can lag the controller's new route while STP unblocks
# the alternate links. Do not call the backup server dead after two TCP probes.
LINK_RECOVERY_WAIT = float(os.environ.get('BRIDGE_LINK_RECOVERY_WAIT', '60'))
SERVER_NAMES = {'10.0.0.3': 'h3', '10.0.0.4': 'h4'}


def server_name(addr):
    host = (addr or '').split(':', 1)[0]
    return SERVER_NAMES.get(host, host or '')

# Set once in main(); lets run_client heartbeat without plumbing args through.
_started_at = None


def _write_json(path, data):
    """Write JSON atomically so a reader never sees a half-written file."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read_json(path):
    """Read a JSON file, returning None when missing or unparseable."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def claim_bridge_lock(max_age=10):
    """Take the singleton lock. Returns True when this is the only bridge.

    A lockfile touched within max_age seconds means a live bridge is already
    watching this directory, so the newcomer must exit instead of racing it
    on the same request file. An older lockfile is a corpse from a dead run
    and is reclaimed. Liveness is file freshness — never a pid probe, so this
    works identically on Linux and Windows.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(LOCK_FILE, flags)
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(LOCK_FILE)
        except OSError:
            age = max_age + 1
        if age < max_age:
            return False
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass
        try:
            fd = os.open(LOCK_FILE, flags)
        except FileExistsError:
            return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def write_heartbeat(started_at=None):
    """Publish liveness for the dashboard's bridge indicator.

    Must also tick DURING a transfer: run_client blocks the main loop for the
    whole run, so without heartbeats from inside it the dashboard declares the
    bridge dead (>10s stale) in the middle of a long transfer.
    """
    try:
        _write_json(ALIVE_FILE, {
            'pid': os.getpid(),
            'started_at': started_at or _started_at or '?',
            'last_poll': time.strftime('%H:%M:%S'),
            'watching': REQUEST_FILE,
            'servers': SERVERS,
        })
        # Touch the lock too: freshness is what makes it a live lock.
        os.utime(LOCK_FILE, None)
    except Exception:
        pass


def emit_event(message):
    """Append an event line for the dashboard and print it to the bridge terminal."""
    print(message, flush=True)
    entry = f'[{time.strftime("%H:%M:%S")}] {message}'
    lines = []
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE) as f:
                lines = json.load(f)
        except Exception:
            lines = []
    lines.append(entry)
    _write_json(EVENTS_FILE, lines[-50:])


def clear_transfer_files():
    """Clear stale bookkeeping for a new transfer — but never the request.

    The main loop already consumed (unlinked) the request before calling this.
    Deleting REQUEST_FILE here as well raced with the dashboard: a click that
    landed between the consume and this clear had its request deleted before
    the bridge ever saw it, so the transfer never started and the user had to
    click again — the "took 5 clicks" symptom.
    """
    for p in (RESULT_FILE, PROGRESS_FILE, EVENTS_FILE, CANCEL_FILE):
        if os.path.exists(p):
            os.unlink(p)


def _write_terminal_progress(filename, request_id, running=False, cancelled=False,
                             last=None, phase=None):
    """Mark the progress file terminal so a later transfer is not blocked.

    The dashboard treats a progress file with running=True as "bridge busy".
    Without this, a finished transfer leaves running=True behind and every
    subsequent click is rejected as busy.

    The last live values ride along: without them this write would zero the
    metrics panel (chunk/total/throughput) at the exact moment the transfer
    completes, which is why the dashboard showed 0/0 and 0 KB/s after every
    successful run.
    """
    entry = {'filename': filename, 'request_id': request_id,
             'running': running, 'cancelled': cancelled}
    if phase:
        entry['phase'] = phase
    if last:
        for key in ('chunk', 'total', 'throughput', 'bytes', 'server',
                    'resume_from'):
            if key in last:
                entry[key] = last[key]
    _write_json(PROGRESS_FILE, entry)


def _heartbeat_loop(stop):
    """Write heartbeats on a timer, including while client output stalls."""
    while not stop.wait(HEARTBEAT_INTERVAL):
        write_heartbeat()


def run_client(filename, filepath, request_id=None, servers=None, resume_from=0,
               attempt=1):
    """Run one client attempt. Returns a dict for the retry loop.

    Terminal progress is written by process_request, not here: an attempt that
    fails and is retried must not mark the run finished.
    """
    servers = servers or SERVERS
    cmd = ([sys.executable, CLIENT_SCRIPT, filepath, '--servers'] + servers
           + ['--transfer-timeout', TRANSFER_TIMEOUT])
    if resume_from > 0:
        cmd += ['--resume', str(resume_from)]
    print(f"[BRIDGE] Running: {' '.join(cmd)}", flush=True)
    emit_event(f'Client started on h1 for {filename}')

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    completed = False
    last_error = None
    deadline = time.time() + CLIENT_TIMEOUT
    result = {'filename': filename, 'request_id': request_id,
              'status': 'failed', 'error': None, 'resume_point': None,
              'server': server_name(servers[0]), 'server_addr': servers[0],
              'chunk': 0, 'total': 0, 'throughput': 0.0}
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop,
                                 args=(stop_heartbeat,), daemon=True)
    heartbeat.start()

    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            print(line, flush=True)
            if line.startswith('[PROGRESS]'):
                try:
                    p = json.loads(line[len('[PROGRESS]'):].strip())
                    result.update({'chunk': p.get('chunk', 0),
                                   'total': p.get('total', 0),
                                   'throughput': p.get('throughput', 0.0)})
                    p.update({
                        'filename': filename, 'request_id': request_id,
                        'running': True, 'phase': 'running',
                        'attempt': attempt, 'max_attempts': 2,
                        'resume_from': resume_from,
                        'server': result.get('server') or server_name(servers[0]),
                    })
                    _write_json(PROGRESS_FILE, p)
                except Exception:
                    pass
            elif line.startswith('[RESUME_POINT]'):
                try:
                    rp = json.loads(line[len('[RESUME_POINT]'):].strip())
                    nxt = int(rp.get('next_chunk', -1))
                    result['resume_point'] = nxt if nxt >= 0 else None
                    result['chunk'] = int(rp.get('last_ack', -1)) + 1
                    result['total'] = int(rp.get('total', 0)) or result['total']
                    addr = rp.get('server') or result['server_addr']
                    result['server_addr'] = addr
                    result['server'] = server_name(addr)
                except Exception:
                    pass
            elif line.startswith('[COMPLETE]'):
                completed = True
                try:
                    done = json.loads(line[len('[COMPLETE]'):].strip())
                    emit_event(f"Server ACKed {done.get('chunks')} chunks "
                               f"({done.get('bytes')} bytes) at {done.get('throughput')} KB/s")
                except Exception:
                    pass
            elif line.startswith('[CLIENT]') and not line.startswith('[CLIENT] Progress'):
                emit_event(line)
                if 'Connected to ' in line:
                    addr = line.split('Connected to ', 1)[1].strip()
                    result['server_addr'] = addr
                    result['server'] = server_name(addr)
                if 'fail' in line.lower() or 'error' in line.lower() or 'lost' in line.lower():
                    last_error = line
            if os.path.exists(CANCEL_FILE):
                emit_event(f'Stop requested — killing client for {filename}')
                proc.send_signal(signal.SIGTERM)
                break
            if time.time() > deadline:
                emit_event(f'Client timed out after {CLIENT_TIMEOUT:.0f}s')
                proc.kill()
                break
    finally:
        stop_heartbeat.set()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        heartbeat.join(timeout=2)

    if os.path.exists(CANCEL_FILE):
        os.unlink(CANCEL_FILE)
        result.update({'status': 'stopped', 'error': 'Cancelled by user',
                       'resume_point': None})
        return result

    if proc.returncode == 0 and completed:
        emit_event(f'Client finished {filename}')
        result.update({'status': 'done', 'error': None})
        return result

    reason = last_error or (f'client exited {proc.returncode}'
                            if proc.returncode != 0
                            else 'client exited without completing a transfer')
    if not reason.startswith('[CLIENT]'):
        reason = f'[CLIENT] {reason}'
    result.update({'status': 'failed', 'error': reason[:300]})
    return result


def _can_connect(addr, timeout=1):
    try:
        host, port = addr.rsplit(':', 1)
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _is_storage_error(result):
    """True when the client aborted because the target server cannot store the file.

    The server is up (reachable) but its disk is full / read-only, so retrying on
    it would only fail again. Fail straight to the backup server from chunk 0.
    """
    error = result.get('error') or ''
    return 'Server storage error' in error


def _sleep_cancel(seconds, request_id):
    end = time.time() + seconds
    while time.time() < end:
        if os.path.exists(CANCEL_FILE):
            return False
        time.sleep(min(0.5, max(0, end - time.time())))
    return True


def _wait_for_server(addr, request_id):
    """Two probes, PROBE_INTERVAL apart. Returns 'up', 'down', or 'cancelled'."""
    name = server_name(addr)
    for i in (1, 2):
        emit_event(f'[RETRY] Polling {name} ({i}/2)')
        if _can_connect(addr):
            return 'up'
        if i == 1 and not _sleep_cancel(PROBE_INTERVAL, request_id):
            return 'cancelled'
    return 'down'


def _server_process_state(host, now=None):
    """Return True/False/None for a server process that is alive/dead/unknown.

    TCP probes cannot make this distinction when the network itself is down:
    they fail even when h3 is healthy. The topology watcher already records the
    process state in server_status.json.
    """
    now = time.time() if now is None else now
    info = _read_json(SERVER_STATUS_FILE)
    try:
        age = now - os.path.getmtime(SERVER_STATUS_FILE)
    except OSError:
        return None
    if age is None or age > SERVER_STATUS_MAX_AGE:
        return None
    alive = (info or {}).get(host, {}).get('alive')
    if alive is True:
        return True
    if alive is False:
        return False
    return None


def _wait_for_path(addr, server, request_id, timeout=None):
    """Wait for a live server's network path to recover, with cancel checks.

    The topology watcher updates server_status.json independently of TCP, so a
    server that dies during the wait can fail over without waiting out the
    whole link-recovery window.
    """
    if timeout is None:
        timeout = LINK_RECOVERY_WAIT
    deadline = time.time() + timeout
    while True:
        if _can_connect(addr):
            return 'up'
        if _server_process_state(server) is False:
            return 'down'
        if time.time() >= deadline:
            return 'down'
        if not _sleep_cancel(PROBE_INTERVAL, request_id):
            return 'cancelled'


def _write_retry_progress(filename, request_id, result, phase, attempt,
                          resume_from, server):
    _write_json(PROGRESS_FILE, {
        'filename': filename, 'request_id': request_id, 'running': True,
        'chunk': result.get('chunk', 0), 'total': result.get('total', 0),
        'throughput': 0.0, 'phase': phase, 'attempt': attempt,
        'max_attempts': 2, 'resume_from': resume_from, 'server': server,
    })


def process_request(request):
    """Run the transfer for the requested file. Returns a result dict."""
    filename = request.get('filename', '')
    request_id = request.get('request_id')
    resume_from = int(request.get('resume_from', 0) or 0)
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        return {'filename': filename, 'request_id': request_id,
                'status': 'failed', 'error': 'Invalid filename'}

    filepath = os.path.join(SHARED_DIR, filename)
    if not os.path.isfile(filepath):
        return {'filename': filename, 'request_id': request_id,
                'status': 'failed', 'error': 'File not found'}

    # Honor the server host the dashboard chose (e.g. after auto-routing
    # around a full target). h3 -> SERVERS[0], h4 -> SERVERS[1], positional so
    # runtime BACKUP_SERVERS overrides are honored without a hard-coded name map.
    # A requested host that isn't configured is rejected rather than silently
    # falling back to the other address.
    host = request.get('host')
    servers = SERVERS
    if host == 'h3':
        if not servers:
            return {'filename': filename, 'request_id': request_id,
                    'status': 'failed',
                    'error': 'No backup servers configured'}
        servers = [servers[0]]
    elif host == 'h4':
        if len(servers) < 2:
            return {'filename': filename, 'request_id': request_id,
                    'status': 'failed',
                    'error': 'Backup server h4 not configured'}
        servers = [servers[1]]
    # No host field -> use the full configured server order.

    clear_transfer_files()
    # Pickup marker BEFORE the client runs: the dashboard's busy check must see
    # this run from the moment it is claimed, not from the first [PROGRESS]
    # line — otherwise a click in that window looks idle and clobbers this run.
    _write_json(PROGRESS_FILE, {'filename': filename, 'request_id': request_id,
                                'running': True, 'chunk': 0, 'total': 0,
                                'throughput': 0.0, 'phase': 'running',
                                'attempt': 1, 'max_attempts': 2,
                                'resume_from': resume_from,
                                'server': server_name(servers[0])})
    try:
        first = run_client(filename, filepath, request_id, servers=servers,
                           resume_from=resume_from)
    except Exception as e:
        return {'filename': filename, 'request_id': request_id,
                'status': 'failed', 'error': str(e)}

    first['attempts'] = 1
    first['resumed_from'] = resume_from
    storage_error = _is_storage_error(first)
    if first['status'] != 'failed' or (first.get('resume_point') is None
                                       and not storage_error):
        _write_terminal_progress(filename, request_id, last=first,
                                 phase=first['status'])
        return first

    # A storage-full error means the target server is up but cannot store the
    # file. Do not wait for it or retry on it — fail straight to the backup
    # server (the positional opposite of the failed one), starting at chunk 0.
    if storage_error:
        server = first.get('server') or server_name(SERVERS[0])
        addr = first.get('server_addr') or SERVERS[0]
        try:
            failed_idx = SERVERS.index(addr)
        except ValueError:
            failed_idx = 0
        backup_idx = 1 if failed_idx == 0 else 0
        if backup_idx >= len(SERVERS):
            # No alternate server to fail over to; report and fail the run.
            emit_event(f'[STORAGE] {server} storage full and no alternate '
                       f'server configured — cannot store {filename}')
            _write_terminal_progress(filename, request_id, last=first,
                                     phase=first['status'])
            return first
        backup = SERVERS[backup_idx]
        backup_name = server_name(backup)
        emit_event(f'[FAILOVER] {server} storage full; restarting on '
                   f'{backup_name} from chunk 0')
        _write_retry_progress(filename, request_id, first, 'failover', 2, 0,
                              backup_name)
        second = run_client(filename, filepath, request_id, servers=[backup],
                            resume_from=0, attempt=2)
        second['attempts'] = 2
        second['resumed_from'] = 0
        second['server'] = backup_name
        second['restarted_on_backup'] = second['status'] == 'done'
        second['downtime_s'] = 0.0
        _write_terminal_progress(filename, request_id, last=second,
                                 phase=second['status'])
        return second

    fail_at = time.time()
    server = first.get('server') or server_name(SERVERS[0])
    addr = first.get('server_addr') or SERVERS[0]
    resume_from = first['resume_point']
    _write_retry_progress(filename, request_id, first, 'retrying', 1,
                          resume_from, server)
    status = _wait_for_server(addr, request_id)
    waited_for_path = False

    if status == 'down' and _server_process_state(server) is True:
        # A failed probe does not mean a failed server when the controller is
        # still rerouting or STP is still unblocking the alternate links.
        waited_for_path = True
        emit_event(f'[LINK] {server} process state says alive, but its path is '
                   f'unreachable by TCP; waiting for the network to recover')
        status = _wait_for_path(addr, server, request_id)

    if status == 'cancelled':
        _write_terminal_progress(filename, request_id, cancelled=True, last=first,
                                 phase='stopped')
        first.update({'status': 'stopped', 'error': 'Cancelled by user'})
        return first

    if status == 'up':
        emit_event(f'[RESUME] {server} back; continuing at chunk '
                   f'{resume_from}/{first.get("total", 0)}')
        _write_retry_progress(filename, request_id, first, 'resuming', 2,
                              resume_from, server)
        second = run_client(filename, filepath, request_id, servers=[addr],
                            resume_from=resume_from, attempt=2)
        second['attempts'] = 2
        second['resumed_from'] = resume_from
        second['server'] = server
        second['downtime_s'] = round(time.time() - fail_at, 1)
        if second['status'] == 'stopped':
            _write_terminal_progress(filename, request_id, cancelled=True,
                                     last=second, phase='stopped')
            return second
        if second['status'] == 'done':
            _write_terminal_progress(filename, request_id, last=second, phase='done')
            return second
        # Preserve the original resume point before falling through to failover.
        if first.get('resume_point') and not second.get('resume_point'):
            second['resume_point'] = first['resume_point']
        first = second  # same-server retry failed; fall through to h4

    backup = SERVERS[-1]
    if server == server_name(backup):
        backup = SERVERS[0]
    backup_name = server_name(backup)
    if waited_for_path:
        emit_event(f'[FAILOVER] {server} stayed unreachable after path wait; '
                   f'restarting on {backup_name} from chunk 0')
    else:
        emit_event(f'[FAILOVER] {server} down confirmed; restarting on '
                   f'{backup_name} from chunk 0')
    _write_retry_progress(filename, request_id, first, 'failover', 2, 0,
                          backup_name)
    second = run_client(filename, filepath, request_id, servers=[backup],
                        resume_from=0, attempt=2)
    second['attempts'] = 2
    second['resumed_from'] = 0
    second['server'] = backup_name
    second['restarted_on_backup'] = second['status'] == 'done'
    second['downtime_s'] = round(time.time() - fail_at, 1)
    # Preserve the resume point from the first attempt if the failover attempt
    # never got far enough to produce one (e.g. no server listening at all).
    if second.get('resume_point') is None and first.get('resume_point'):
        second['resume_point'] = first['resume_point']
    _write_terminal_progress(filename, request_id, last=second,
                             phase=second['status'])
    return second


def report_namespace():
    """Log which network namespace this process is actually in.

    A bridge launched in the host namespace instead of h1's cannot reach
    10.0.0.0/24 at all, and every transfer then fails with
    "[Errno 101] Network is unreachable" while pingall still looks fine.
    Printing the observed IP makes that misconfiguration obvious instead of
    leaving it to be inferred from failed connects.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.connect(('10.0.0.3', 9000))
            ip = s.getsockname()[0]
    except OSError as e:
        ip = f'unknown ({e.__class__.__name__}: {e})'

    expected = SERVERS[0].split(':')[0]
    print(f'[BRIDGE] Namespace IP: {ip} (h1 inside Mininet = 10.0.0.1)', flush=True)
    if ip == 'unknown' or ip.startswith('unknown'):
        print('[BRIDGE] WARNING: no route to the backup servers — this process '
              'is probably NOT in h1 network namespace. Start it from '
              'mininet/topology.py, which uses node.popen().', flush=True)
    elif ip != '10.0.0.1':
        print(f'[BRIDGE] WARNING: expected 10.0.0.1 (h1), got {ip}', flush=True)
    else:
        print(f'[BRIDGE] Namespace OK; clients will reach {expected}', flush=True)
    return ip


def main():
    global _started_at
    started_at = time.strftime('%H:%M:%S')
    _started_at = started_at
    if not claim_bridge_lock():
        print('[BRIDGE] Another bridge is already watching '
              f'{SHARED_DIR} — exiting instead of racing it. '
              'If no bridge should be running, delete '
              f'{LOCK_FILE} and restart.', flush=True)
        sys.exit(1)
    print(f'[BRIDGE] Watching {REQUEST_FILE} (lock held, pid {os.getpid()})', flush=True)
    print(f'[BRIDGE] Servers: {SERVERS}', flush=True)
    report_namespace()
    emit_event('Bridge started on h1')
    while True:
        try:
            write_heartbeat(started_at)
            if os.path.isfile(REQUEST_FILE):
                try:
                    with open(REQUEST_FILE, 'r') as f:
                        request = json.load(f)
                except Exception as e:
                    # Half-written or corrupt request (e.g. dashboard killed
                    # mid-write). Drop it instead of retrying it forever.
                    print(f"[BRIDGE] Bad request file, dropping it: {e}", flush=True)
                    try:
                        os.unlink(REQUEST_FILE)
                    except OSError:
                        pass
                    continue
                os.unlink(REQUEST_FILE)
                result = process_request(request)
                result['timestamp'] = time.strftime('%H:%M:%S')
                _write_json(RESULT_FILE, result)
                emit_event(
                    f"Transfer {result['status']}: {result['filename']}"
                    + (f" — {result.get('error')}" if result.get('error') else '')
                )
                print(f"[BRIDGE] Result: {result}", flush=True)
        except Exception as e:
            print(f"[BRIDGE] Error: {e}", flush=True)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
