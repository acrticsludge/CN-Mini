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


def _load_app():
    spec = importlib.util.spec_from_file_location('edge4_dashboard_under_test',
                                                   SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MB = 1024 * 1024


def _write_file(mod, name, mb):
    with open(os.path.join(mod.SHARED_DIR, name), 'wb') as f:
        f.write(b'x' * (mb * MB))


def _set_quota(client, mod, host, quota_mb):
    return client.post('/api/storage', json={'host': host, 'quota_mb': quota_mb})


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
