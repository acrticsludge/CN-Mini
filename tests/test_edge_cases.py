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
import sys

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


def test_bridge_blocks_failover_when_backup_lacks_space(tmp_path):
    """Runtime storage failover must not send to a backup server with too little
    free space. process_request should fail the run instead."""
    bridge = _load_bridge('bridge_edge4_no_backup_space_under_test', BRIDGE_SPEC)
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
