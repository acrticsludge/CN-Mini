"""
Edge case 4: storage full with auto-failover.

Backend tests (Flask app only, no Mininet) covering:
- routes-to-backup: target full + other has space -> auto-route to the other server.
- both-full-rejects: both servers full -> 409 reject.
- quota-zero-unlimited: quota 0 means unlimited.
- exact-fit-allows: size == free space is allowed.
- mid-transfer-quota-no-effect: quota change mid-run does not alter an in-flight transfer.
"""

import importlib.util
import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'app.py')
BRIDGE_SPEC = os.path.join(os.path.dirname(__file__), '..', 'mininet', 'bridge.py')


def _load_app():
    spec = importlib.util.spec_from_file_location('edge4_dashboard_under_test',
                                                   SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_bridge(name, path=BRIDGE_SPEC):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MB = 1024 * 1024


def _write_file(mod, name, mb):
    with open(os.path.join(mod.SHARED_DIR, name), 'wb') as f:
        f.write(b'x' * (mb * MB))


def _set_quota(client, mod, host, quota_mb):
    return client.post('/api/storage', json={'host': host, 'quota_mb': quota_mb})


def _free_port():
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _serve(tmp_path, host, quota_mb):
    """Start a real backup server with an optional quota. Returns (port, store)."""
    from server.server import BackupServer

    store = tmp_path / host
    store.mkdir()
    quota_file = None
    if quota_mb is not None:
        quota_file = tmp_path / 'server_storage.json'
        quota_file.write_text(json.dumps({host: {'quota_mb': quota_mb, 'used_mb': 0}}))
    port = _free_port()
    srv = BackupServer(host='127.0.0.1', port=port, storage_dir=str(store),
                       name=host,
                       quota_file=str(quota_file) if quota_file else None)
    threading.Thread(target=srv.start, daemon=True).start()
    time.sleep(0.3)
    return port, store


def test_server_refuses_file_over_quota(tmp_path):
    """The receiver, not just the dashboard, must refuse a file that cannot fit.

    This is the case the UI showed as 1024/1023 MB: a failover checked the
    quota before the server filled up, so the bytes still landed on disk.
    """
    from client.client import BackupClient

    port, store = _serve(tmp_path, 'h4', quota_mb=1)
    shared = tmp_path / 'shared'
    shared.mkdir()
    payload = shared / 'big.bin'
    payload.write_bytes(b'x' * (2 * MB))

    client = BackupClient([('127.0.0.1', port)])
    try:
        ok = client.send_file(str(payload))
    finally:
        client.close()

    assert ok is False
    assert not (store / 'big.bin').exists(), 'over-quota file was stored'


def test_server_reserves_quota_for_inflight_transfer(tmp_path):
    """A second connection cannot claim space the first one is about to use."""
    from common.protocol import MSG_ACK, MSG_ERROR, pack_hello
    from common.transport import recv_frame

    port, _ = _serve(tmp_path, 'h4', quota_mb=1)

    first = socket.create_connection(('127.0.0.1', port), timeout=5)
    first.sendall(pack_hello('a.bin', 600 * 1024))
    assert recv_frame(first)['type'] == MSG_ACK

    # Leave the first transfer stalled at HELLO: its 600 KB are reserved.
    second = socket.create_connection(('127.0.0.1', port), timeout=5)
    second.sendall(pack_hello('b.bin', 600 * 1024))
    assert recv_frame(second)['type'] == MSG_ERROR, 'reserved space was handed out twice'
    second.close()

    # Dropping the stalled client releases the reservation for the next one.
    first.close()
    deadline = time.time() + 2
    while True:
        third = socket.create_connection(('127.0.0.1', port), timeout=5)
        third.sendall(pack_hello('c.bin', 600 * 1024))
        frame = recv_frame(third)
        if frame['type'] == MSG_ACK:
            third.close()
            break
        third.close()
        assert time.time() < deadline, 'reservation never released after disconnect'
        time.sleep(0.05)


def test_server_rejects_second_transfer_for_same_name(tmp_path):
    """A second writer is told the file is busy, even without a quota.

    Two handlers on one filename share a .part path and can interleave bytes.
    The second must be refused, and the first must still produce one complete
    file.
    """
    from common.checksum import compute_crc32
    from common.protocol import (CHUNK_SIZE, MSG_ACK, MSG_ERROR, pack_data,
                                 pack_hello)
    from common.transport import recv_frame

    port, store = _serve(tmp_path, 'h4', quota_mb=None)
    content = b'A' * (CHUNK_SIZE * 2)
    total = 2

    first = socket.create_connection(('127.0.0.1', port), timeout=5)
    first.sendall(pack_hello('same.bin', len(content)))
    assert recv_frame(first)['type'] == MSG_ACK

    second = socket.create_connection(('127.0.0.1', port), timeout=5)
    second.sendall(pack_hello('same.bin', len(content)))
    busy = recv_frame(second)
    assert busy['type'] == MSG_ERROR
    assert json.loads(busy['payload'])['code'] == 'busy'
    second.close()

    # On a case-insensitive volume the same file must stay busy too.
    if os.path.normcase('A') == os.path.normcase('a'):
        variant = socket.create_connection(('127.0.0.1', port), timeout=5)
        variant.sendall(pack_hello('SAME.BIN', len(content)))
        case_busy = recv_frame(variant)
        assert case_busy['type'] == MSG_ERROR
        assert json.loads(case_busy['payload'])['code'] == 'busy'
        variant.close()

    for i in range(total):
        chunk = content[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
        first.sendall(pack_data(i, total, chunk, compute_crc32(chunk)))
        assert recv_frame(first)['type'] == MSG_ACK
    first.close()

    assert (store / 'same.bin').read_bytes() == content

    # The claim is released, so a later transfer can replace the file.
    third = socket.create_connection(('127.0.0.1', port), timeout=5)
    third.sendall(pack_hello('same.bin', len(content)))
    assert recv_frame(third)['type'] == MSG_ACK
    third.close()


def test_server_rejects_part_suffixed_filename(tmp_path):
    """A source named x.part would collide with x's in-progress copy."""
    from common.protocol import MSG_ERROR, pack_hello
    from common.transport import recv_frame

    port, store = _serve(tmp_path, 'h4', quota_mb=None)
    # Windows strips trailing dots/spaces from path components, so these alias
    # report.csv.part (x's in-progress temp file).
    for name in ('report.csv.part', 'REPORT.CSV.PART',
                 'report.csv.part.', 'report.csv.part '):
        sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        sock.sendall(pack_hello(name, 1024))
        error = recv_frame(sock)
        assert error['type'] == MSG_ERROR, name
        assert json.loads(error['payload'])['code'] == 'invalid', name
        sock.close()
    assert list(store.iterdir()) == []


def test_dashboard_hides_and_rejects_part_sources(dash):
    """The picker and the API both refuse names the receiver reserves."""
    client = dash.app.test_client()
    _write_file(dash, 'REPORT.CSV.PART', 1)

    names = [f['name'] for f in client.get('/api/browse').get_json()['files']]
    assert 'REPORT.CSV.PART' not in names

    resp = client.post('/api/start-transfer',
                       json={'filename': 'REPORT.CSV.PART'})
    assert resp.status_code == 400, resp.get_json()
    assert '.part' in resp.get_json()['error']


def test_client_labels_busy_error(tmp_path, capsys):
    """A busy file must not be reported as a disk problem."""
    from common.protocol import MSG_ERROR, MSG_HELLO, pack_frame
    from common.transport import recv_frame
    from client.client import BackupClient

    payload = tmp_path / 'a.bin'
    payload.write_bytes(b'x')
    port = _free_port()
    ready = threading.Event()
    received = {}

    def fake_server():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', port))
        srv.listen(1)
        srv.settimeout(5)
        ready.set()
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            received['hello'] = recv_frame(conn)['type']
            body = json.dumps({'reason': 'another transfer for a.bin is in progress',
                               'code': 'busy'}).encode()
            conn.sendall(pack_frame(MSG_ERROR, 0, 0, 0, len(body), body))
        srv.close()

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    assert ready.wait(5)

    client = BackupClient([('127.0.0.1', port)])
    try:
        assert client.send_file(str(payload)) is False
    finally:
        client.close()
        thread.join(timeout=5)

    assert received['hello'] == MSG_HELLO
    out = capsys.readouterr().out
    assert '[CLIENT] File busy:' in out
    assert 'Server storage error' not in out


def test_server_allows_exact_fit_and_replacement(tmp_path):
    """Exactly quota-sized is allowed, and re-sending it needs no extra space."""
    from client.client import BackupClient

    port, store = _serve(tmp_path, 'h4', quota_mb=1)
    shared = tmp_path / 'shared'
    shared.mkdir()
    payload = shared / 'fit.bin'
    payload.write_bytes(b'x' * MB)

    for _ in range(2):
        client = BackupClient([('127.0.0.1', port)])
        try:
            assert client.send_file(str(payload)) is True
        finally:
            client.close()

    assert (store / 'fit.bin').stat().st_size == MB


# ---------------------------------------------------------------------------
# Bridge-level test: the bridge must honor the host the dashboard chose
# ---------------------------------------------------------------------------

def test_bridge_routes_to_host_chosen_by_dashboard(tmp_path):
    """process_request passes the chosen host's address to run_client."""
    bridge = _load_bridge('bridge_edge4_host_route_under_test', BRIDGE_SPEC)
    shared = str(tmp_path / 'shared')
    os.makedirs(shared, exist_ok=True)
    bridge.SHARED_DIR = shared
    bridge.REQUEST_FILE = os.path.join(shared, 'transfer_request.json')
    bridge.RESULT_FILE = os.path.join(shared, 'transfer_result.json')
    bridge.PROGRESS_FILE = os.path.join(shared, 'transfer_progress.json')
    bridge.EVENTS_FILE = os.path.join(shared, 'transfer_events.json')
    bridge.CANCEL_FILE = os.path.join(shared, 'transfer_cancel.json')
    bridge.LOCK_FILE = os.path.join(shared, 'bridge.lock')
    bridge.ALIVE_FILE = os.path.join(shared, 'bridge_alive.json')
    # Default SERVERS = ['10.0.0.3:9000', '10.0.0.4:9000'] -> h3/h4.
    captured = {}

    def fake_run_client(filename, filepath, request_id=None, servers=None,
                        resume_from=0, attempt=1):
        captured['servers'] = servers
        captured['resume_from'] = resume_from
        return {'filename': filename, 'request_id': request_id,
                'status': 'done', 'error': None}

    bridge.run_client = fake_run_client
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * 100)

    res = bridge.process_request({'filename': 'a.bin', 'request_id': 'r1',
                                  'host': 'h4'})
    assert res['status'] == 'done', res
    assert captured['servers'] == ['10.0.0.4:9000'], captured

    # No host field -> full configured server order.
    captured.clear()
    res2 = bridge.process_request({'filename': 'a.bin', 'request_id': 'r2'})
    assert res2['status'] == 'done', res2
    assert captured['servers'] == bridge.SERVERS, captured

    # Custom BACKUP_SERVERS: routing is positional, not tied to a hard-coded
    # IP->name map, so h3/h4 map to SERVERS[0]/SERVERS[1] whatever they are.
    bridge.SERVERS = ['10.1.0.3:9000', '10.1.0.4:9000']
    captured.clear()
    res3 = bridge.process_request({'filename': 'a.bin', 'request_id': 'r3',
                                   'host': 'h4'})
    assert res3['status'] == 'done', res3
    assert captured['servers'] == ['10.1.0.4:9000'], captured
    captured.clear()
    res4 = bridge.process_request({'filename': 'a.bin', 'request_id': 'r4',
                                   'host': 'h3'})
    assert res4['status'] == 'done', res4
    assert captured['servers'] == ['10.1.0.3:9000'], captured


def test_bridge_rejects_h4_when_only_one_server_configured(tmp_path):
    """With a single configured server, requesting h4 must fail instead of
    silently falling back to h3's address."""
    bridge = _load_bridge('bridge_edge4_single_server_under_test', BRIDGE_SPEC)
    shared = str(tmp_path / 'shared')
    os.makedirs(shared, exist_ok=True)
    bridge.SHARED_DIR = shared
    bridge.REQUEST_FILE = os.path.join(shared, 'transfer_request.json')
    bridge.RESULT_FILE = os.path.join(shared, 'transfer_result.json')
    bridge.PROGRESS_FILE = os.path.join(shared, 'transfer_progress.json')
    bridge.EVENTS_FILE = os.path.join(shared, 'transfer_events.json')
    bridge.CANCEL_FILE = os.path.join(shared, 'transfer_cancel.json')
    bridge.LOCK_FILE = os.path.join(shared, 'bridge.lock')
    bridge.ALIVE_FILE = os.path.join(shared, 'bridge_alive.json')
    # Only one backup server configured: h4 does not exist.
    bridge.SERVERS = ['10.0.0.3:9000']
    captured = {}

    def fake_run_client(filename, filepath, request_id=None, servers=None,
                        resume_from=0, attempt=1):
        captured['servers'] = servers
        return {'filename': filename, 'request_id': request_id,
                'status': 'done', 'error': None}

    bridge.run_client = fake_run_client
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * 100)

    # h3 still works (it maps to the single configured server).
    res = bridge.process_request({'filename': 'a.bin', 'request_id': 'r1',
                                  'host': 'h3'})
    assert res['status'] == 'done', res
    assert captured['servers'] == ['10.0.0.3:9000'], captured

    # h4 is not configured -> rejected, run_client never invoked.
    captured.clear()
    res2 = bridge.process_request({'filename': 'a.bin', 'request_id': 'r2',
                                   'host': 'h4'})
    assert res2['status'] == 'failed', res2
    assert 'h4' in (res2.get('error') or '').lower(), res2
    assert captured == {}, captured  # run_client must not be called


@pytest.fixture()
def dash(tmp_path):
    mod = _load_app()
    shared = str(tmp_path / 'shared')
    os.makedirs(shared, exist_ok=True)
    mod.SHARED_DIR = shared
    mod.REQUEST_FILE = os.path.join(shared, 'transfer_request.json')
    mod.RESULT_FILE = os.path.join(shared, 'transfer_result.json')
    mod.PROGRESS_FILE = os.path.join(shared, 'transfer_progress.json')
    mod.EVENTS_FILE = os.path.join(shared, 'transfer_events.json')
    mod.CANCEL_FILE = os.path.join(shared, 'transfer_cancel.json')
    mod.SERVER_REQUEST_FILE = os.path.join(shared, 'server_request.json')
    mod.SERVER_RESULT_FILE = os.path.join(shared, 'server_result.json')
    mod.SERVER_STATUS_FILE = os.path.join(shared, 'server_status.json')
    mod.SERVER_STORAGE_FILE = os.path.join(shared, 'server_storage.json')
    mod.STORED_DIR = str(tmp_path / 'stored')
    for server in ('h3', 'h4'):
        os.makedirs(os.path.join(mod.STORED_DIR, server), exist_ok=True)
    mod.transfer_state.update({
        'running': False, 'filename': None, 'request_id': None, 'progress': 0,
        'chunks_sent': 0, 'total_chunks': 0, 'throughput': 0.0,
        'start_time': None, 'recovery_time': None, 'recovery_kind': None,
        'recovery_target': None, 'server_down_at': None, 'phase': None,
        'attempt': 0, 'max_attempts': 2, 'resume_from': 0, 'server': None,
        'storage_from': None, 'storage_to': None,
        'retry_wait_s': None, 'last_chunk_at': None, 'chunk_idle_s': None,
        'link_stall_warned': False, 'result': None, 'completed_at': None,
    })
    mod.event_log.clear()
    mod._events_ingested = 0
    return mod


def event_text(mod):
    return '\n'.join(mod.event_log)


# ---------------------------------------------------------------------------
# Backend unit tests
# ---------------------------------------------------------------------------

def test_routes_to_backup_when_target_full(dash):
    """Target full + other has space: auto-route to the other server."""
    client = dash.app.test_client()
    _set_quota(client, dash, 'h3', 1)     # h3: 1 MB quota
    _set_quota(client, dash, 'h4', 1000)  # h4: plenty of room
    _write_file(dash, 'big.bin', 10)      # needs 10 MB

    resp = client.post('/api/start-transfer',
                       json={'filename': 'big.bin', 'host': 'h3'})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['status'] == 'queued'

    # Queued to the backup server (h4), not the full target (h3).
    with open(dash.REQUEST_FILE) as f:
        req = json.load(f)
    assert req['host'] == 'h4', req

    log = event_text(dash)
    assert '[STORAGE] h3 full for big.bin' in log, log
    assert 'routing to h4' in log, log


def test_both_full_rejects_409(dash):
    """Both servers full: reject 409."""
    client = dash.app.test_client()
    _set_quota(client, dash, 'h3', 1)
    _set_quota(client, dash, 'h4', 1)
    _write_file(dash, 'big.bin', 10)

    resp = client.post('/api/start-transfer',
                       json={'filename': 'big.bin', 'host': 'h3'})
    assert resp.status_code == 409, resp.get_json()

    log = event_text(dash)
    assert '[STORAGE] Both servers full — cannot store big.bin' in log, log


def test_quota_zero_is_unlimited(dash):
    """quota_mb == 0 means unlimited: a large file is still accepted."""
    client = dash.app.test_client()
    _set_quota(client, dash, 'h3', 0)     # unlimited
    _set_quota(client, dash, 'h4', 1)
    _write_file(dash, 'big.bin', 10)

    resp = client.post('/api/start-transfer',
                       json={'filename': 'big.bin', 'host': 'h3'})
    assert resp.status_code == 200, resp.get_json()
    with open(dash.REQUEST_FILE) as f:
        req = json.load(f)
    assert req['host'] == 'h3', req  # not routed away (unlimited target)


def test_exact_fit_allows(dash):
    """size == free space is allowed (need_mb > free_mb is the full test)."""
    client = dash.app.test_client()
    _set_quota(client, dash, 'h3', 10)    # exactly 10 MB free
    _set_quota(client, dash, 'h4', 1000)
    _write_file(dash, 'big.bin', 10)      # needs exactly 10 MB

    resp = client.post('/api/start-transfer',
                       json={'filename': 'big.bin', 'host': 'h3'})
    assert resp.status_code == 200, resp.get_json()
    with open(dash.REQUEST_FILE) as f:
        req = json.load(f)
    assert req['host'] == 'h3', req


def test_partial_mb_usage_routes_over_quota(dash):
    """Byte-accurate check: 512 KB used + 768 KB need exceeds a 1 MB quota.

    Rounded-up MB (need 1 MB vs free 1 MB) would wrongly allow it, but the
    byte check (512 KB used leaves 512 KB free; 768 KB > 512 KB) correctly
    routes to the backup server.
    """
    client = dash.app.test_client()
    _set_quota(client, dash, 'h3', 1)     # 1 MB quota
    _set_quota(client, dash, 'h4', 1000)
    # Store 512 KB on h3: used_mb rounds to 0, but used_bytes is 512 KB.
    with open(os.path.join(dash.STORED_DIR, 'h3', 'existing.bin'), 'wb') as f:
        f.write(b'y' * (512 * 1024))
    with open(os.path.join(dash.SHARED_DIR, 'big.bin'), 'wb') as f:
        f.write(b'y' * (768 * 1024))      # 768 KB need

    resp = client.post('/api/start-transfer',
                       json={'filename': 'big.bin', 'host': 'h3'})
    assert resp.status_code == 200, resp.get_json()
    with open(dash.REQUEST_FILE) as f:
        req = json.load(f)
    assert req['host'] == 'h4', req  # h3 has too little free space


def _make_bridge(tmp_path, name):
    """Load a bridge module pointed at a temp shared dir with both servers."""
    bridge = _load_bridge(name, BRIDGE_SPEC)
    shared = str(tmp_path / 'shared')
    os.makedirs(shared, exist_ok=True)
    bridge.SHARED_DIR = shared
    bridge.REQUEST_FILE = os.path.join(shared, 'transfer_request.json')
    bridge.RESULT_FILE = os.path.join(shared, 'transfer_result.json')
    bridge.PROGRESS_FILE = os.path.join(shared, 'transfer_progress.json')
    bridge.EVENTS_FILE = os.path.join(shared, 'transfer_events.json')
    bridge.CANCEL_FILE = os.path.join(shared, 'transfer_cancel.json')
    bridge.LOCK_FILE = os.path.join(shared, 'bridge.lock')
    bridge.ALIVE_FILE = os.path.join(shared, 'bridge_alive.json')
    bridge.SERVERS = ['10.0.0.3:9000', '10.0.0.4:9000']
    bridge.SERVER_STORAGE_FILE = os.path.join(shared, 'server_storage.json')
    return bridge, shared


def test_bridge_blocks_failover_when_backup_lacks_space(tmp_path):
    """Runtime storage failover must not send to a backup server with too little
    free space. process_request should fail the run instead."""
    bridge, shared = _make_bridge(tmp_path, 'bridge_edge4_no_backup_space_under_test')

    captured = []

    def fake_run_client(filename, filepath, request_id=None, servers=None,
                        resume_from=0, attempt=1):
        captured.append(list(servers or []))
        return {'filename': filename, 'request_id': request_id,
                'status': 'failed',
                'error': '[CLIENT] Server storage error: disk full',
                'resume_point': None}

    bridge.run_client = fake_run_client

    # h3 full (quota 1 MB), h4 also too small (quota 1 MB), file needs 10 MB.
    with open(bridge.SERVER_STORAGE_FILE, 'w') as f:
        json.dump({'h3': {'quota_mb': 1, 'used_mb': 0},
                   'h4': {'quota_mb': 1, 'used_mb': 0}}, f)
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * (10 * MB))

    # First attempt fails with storage error on h3.
    res = bridge.process_request({'filename': 'a.bin', 'request_id': 'r1',
                                  'host': 'h3'})
    assert res['status'] == 'failed', res
    # Failover to h4 must be blocked (h4 also too small): run_client must
    # never be called with h4's address.
    assert ['10.0.0.4:9000'] not in captured, captured


def test_bridge_blocks_server_down_failover_when_backup_lacks_space(tmp_path):
    """The reported case: h3 dies mid-transfer, h4 cannot hold the whole file.

    The run must fail instead of restarting on h4 from chunk 0 and writing past
    its quota.
    """
    bridge, shared = _make_bridge(tmp_path, 'bridge_edge4_down_no_space_under_test')
    captured = []

    def fake_run_client(filename, filepath, request_id=None, servers=None,
                        resume_from=0, attempt=1):
        captured.append(list(servers or []))
        return {'filename': filename, 'request_id': request_id,
                'status': 'failed',
                'error': '[CLIENT] Connection lost at chunk 1',
                'resume_point': 1, 'server': 'h3',
                'server_addr': '10.0.0.3:9000', 'chunk': 1, 'total': 256}

    bridge.run_client = fake_run_client
    bridge._wait_for_server = lambda addr, request_id: 'down'
    bridge._server_process_state = lambda host, now=None: False

    # h3 has room; h4 is one MB short of the 2 MB file.
    with open(bridge.SERVER_STORAGE_FILE, 'w') as f:
        json.dump({'h3': {'quota_mb': 10, 'used_mb': 0},
                   'h4': {'quota_mb': 1, 'used_mb': 0}}, f)
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * (2 * MB))

    res = bridge.process_request({'filename': 'a.bin', 'request_id': 'r1',
                                  'host': 'h3'})
    assert res['status'] == 'failed', res
    assert ['10.0.0.4:9000'] not in captured, captured
    with open(bridge.EVENTS_FILE) as f:
        events = '\n'.join(json.load(f))
    assert 'h4 also lacks space' in events, events


def test_mid_transfer_quota_change_no_effect(dash):
    """Quota change mid-run does not re-route an already-queued transfer."""
    client = dash.app.test_client()
    _set_quota(client, dash, 'h3', 1000)
    _set_quota(client, dash, 'h4', 1000)
    _write_file(dash, 'big.bin', 1)       # needs 1 MB, h3 has room

    resp = client.post('/api/start-transfer',
                       json={'filename': 'big.bin', 'host': 'h3'})
    assert resp.status_code == 200, resp.get_json()
    with open(dash.REQUEST_FILE) as f:
        req = json.load(f)
    assert req['host'] == 'h3', req  # in-flight transfer targets h3

    # Quota changes while the transfer is in flight.
    _set_quota(client, dash, 'h3', 1)
    with open(dash.REQUEST_FILE) as f:
        req = json.load(f)
    assert req['host'] == 'h3', req  # in-flight request not re-routed

    # A brand-new large transfer to h3 now sees the small quota and routes to h4.
    os.unlink(dash.REQUEST_FILE)
    dash.transfer_state.update({'running': False, 'filename': None,
                                'request_id': None})
    _write_file(dash, 'huge.bin', 10)     # needs 10 MB
    resp2 = client.post('/api/start-transfer',
                        json={'filename': 'huge.bin', 'host': 'h3'})
    assert resp2.status_code == 200, resp2.get_json()
    with open(dash.REQUEST_FILE) as f:
        req2 = json.load(f)
    assert req2['host'] == 'h4', req2
