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

SERVERS = os.environ.get('BACKUP_SERVERS', '10.0.0.3:9000,10.0.0.4:9000').split(',')
POLL_INTERVAL = float(os.environ.get('BRIDGE_POLL_INTERVAL', '0.5'))
CLIENT_TIMEOUT = float(os.environ.get('BRIDGE_TIMEOUT', '300'))


def _write_json(path, data):
    """Write JSON atomically so a reader never sees a half-written file."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def emit_event(message):
    """Append an event line for the dashboard to pick up."""
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


def _write_terminal_progress(filename, running=False, cancelled=False):
    """Mark the progress file terminal so a later transfer is not blocked.

    The dashboard treats a progress file with running=True as "bridge busy".
    Without this, a finished transfer leaves running=True behind and every
    subsequent click is rejected as busy.
    """
    _write_json(PROGRESS_FILE, {'filename': filename, 'running': running,
                                'cancelled': cancelled})


def run_client(filename, filepath):
    """Run the client, streaming progress. Returns (status, error)."""
    cmd = [sys.executable, CLIENT_SCRIPT, filepath, '--servers'] + SERVERS
    print(f"[BRIDGE] Running: {' '.join(cmd)}", flush=True)
    emit_event(f'Client started on h1 for {filename}')

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    stored = None
    completed = False
    last_error = None
    deadline = time.time() + CLIENT_TIMEOUT

    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            print(line, flush=True)
            if line.startswith('[PROGRESS]'):
                try:
                    p = json.loads(line[len('[PROGRESS]'):].strip())
                    p.update({'filename': filename, 'running': True})
                    _write_json(PROGRESS_FILE, p)
                except Exception:
                    pass
            elif line.startswith('[STORED]'):
                try:
                    stored = json.loads(line[len('[STORED]'):].strip())
                except Exception:
                    pass
            elif line.startswith('[COMPLETE]'):
                completed = True
                # The client's final tally — the count of chunks the server ACKed.
                try:
                    done = json.loads(line[len('[COMPLETE]'):].strip())
                    emit_event(f"Server ACKed {done.get('chunks')} chunks "
                               f"({done.get('bytes')} bytes) at {done.get('throughput')} KB/s")
                except Exception:
                    pass
            elif line.startswith('[CLIENT]') and not line.startswith('[CLIENT] Progress'):
                # Stage changes only. Progress lines are carried by transfer_progress.json;
                # emitting them as events buries the events that actually matter.
                # Keep the last failure line so the dashboard can show why it failed
                # instead of an opaque "client exited 1".
                emit_event(line)
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
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    if os.path.exists(CANCEL_FILE):
        os.unlink(CANCEL_FILE)
        _write_terminal_progress(filename, running=False, cancelled=True)
        return 'stopped', 'Cancelled by user'

    if stored:
        emit_event(f"Server stored {stored['filename']} "
                   f"({stored.get('size', 0)} bytes, {stored.get('chunks', 0)} chunks)")

    if proc.returncode == 0 and completed:
        # Exit code 0 alone is not proof: a client that never connected still
        # exits 0, and reporting that as "done" is how a failed transfer was
        # previously shown as a success. Require the [COMPLETE] tally too.
        _write_terminal_progress(filename, running=False)
        emit_event(f'Client finished {filename}')
        return 'done', None

    reason = last_error or (f'client exited {proc.returncode}'
                            if proc.returncode != 0
                            else 'client exited without completing a transfer')
    if not reason.startswith('[CLIENT]'):
        reason = f'[CLIENT] {reason}'
    _write_terminal_progress(filename, running=False)
    return 'failed', reason[:300]


def process_request(request):
    """Run the transfer for the requested file. Returns a result dict."""
    filename = request.get('filename', '')
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        return {'filename': filename, 'status': 'failed', 'error': 'Invalid filename'}

    filepath = os.path.join(SHARED_DIR, filename)
    if not os.path.isfile(filepath):
        return {'filename': filename, 'status': 'failed', 'error': 'File not found'}

    clear_transfer_files()
    try:
        status, error = run_client(filename, filepath)
    except Exception as e:
        return {'filename': filename, 'status': 'failed', 'error': str(e)}

    result = {'filename': filename, 'status': status}
    if error:
        result['error'] = error
    return result


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
    print(f'[BRIDGE] Watching {REQUEST_FILE}', flush=True)
    print(f'[BRIDGE] Servers: {SERVERS}', flush=True)
    report_namespace()
    emit_event('Bridge started on h1')
    while True:
        try:
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
