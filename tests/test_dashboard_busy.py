"""
Dashboard busy-guard + metrics-sync regressions — no Mininet needed.

Pins two behaviours behind the "works 10% of clicks / metrics always zero"
reports:
  1. A second POST while the bridge still has work must be rejected (409), not
     silently overwrite the live run's request/progress/result.
  2. /api/metrics must reflect the bridge's latest progress, not stale state.
"""

import importlib.util
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'app.py')


def _load_app():
    spec = importlib.util.spec_from_file_location('dashboard_under_test', SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    mod.transfer_state.update({
        'running': False, 'filename': None, 'request_id': None, 'progress': 0,
        'chunks_sent': 0, 'total_chunks': 0, 'throughput': 0.0,
        'start_time': None, 'recovery_time': None, 'result': None,
        'completed_at': None,
    })
    mod.event_log.clear()
    mod._events_ingested = 0
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * 100)
    return mod


def test_second_click_while_pending_is_rejected(dash):
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    second = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    assert second.status_code == 409
    assert 'already running' in second.get_json()['error'].lower()
    # The first request must still be intact for the bridge to pick up.
    with open(dash.REQUEST_FILE) as f:
        assert json.load(f)['filename'] == 'a.bin'


def test_second_click_while_running_is_rejected(dash):
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    os.unlink(dash.REQUEST_FILE)  # bridge picked it up
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 1,
                   'total': 10, 'throughput': 5.0}, f)
    second = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    assert second.status_code == 409


def test_new_transfer_allowed_after_terminal_progress(dash):
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    os.unlink(dash.REQUEST_FILE)
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': False}, f)
    with open(dash.RESULT_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'status': 'done'}, f)
    retry = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    assert retry.status_code == 200


def test_metrics_reflect_bridge_progress(dash):
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 3,
                   'total': 10, 'throughput': 42.0}, f)
    metrics = client.get('/api/metrics').get_json()
    assert metrics['throughput'] == 42.0


def test_result_with_foreign_request_id_is_ignored(dash):
    """A result from another run (e.g. a duplicate bridge) must not settle this one."""
    client = dash.app.test_client()
    first = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    my_id = first.get_json()['request_id']
    with open(dash.RESULT_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'request_id': 'someone-elses-run',
                   'status': 'failed', 'error': '[CLIENT] boom'}, f)
    state = client.get('/api/status').get_json()['transfer']
    assert state['result'] is None
    assert state['running'] is True
    with open(dash.RESULT_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'request_id': my_id, 'status': 'done'}, f)
    state = client.get('/api/status').get_json()['transfer']
    assert state['result']['status'] == 'done'
    assert state['running'] is False


def test_legacy_files_without_request_id_still_match(dash):
    """Files written by an older bridge (no id) match by filename, not orphaned."""
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 1,
                   'total': 10, 'throughput': 1.0}, f)
    with open(dash.RESULT_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'status': 'done'}, f)
    state = client.get('/api/status').get_json()['transfer']
    assert state['result']['status'] == 'done'


def test_browse_hides_internal_bookkeeping_files(dash):
    """bridge.lock / heartbeats / link files live in shared_files/ via the bind
    mount but are not transferable files — the picker must not offer them."""
    import pathlib
    shared = pathlib.Path(dash.SHARED_DIR)
    (shared / 'bridge.lock').write_text('1234')
    (shared / 'bridge_alive.json').write_text('{}')
    (shared / 'link_request.json').write_text('{}')
    (shared / 'link_result.json').write_text('{}')
    names = {f['name'] for f in dash.app.test_client().get('/api/browse').get_json()['files']}
    assert 'a.bin' in names
    assert not (names & {'bridge.lock', 'bridge_alive.json',
                         'link_request.json', 'link_result.json'})


def test_progress_percent_follows_chunks(dash):
    """The bar reads state.progress: it must track chunk/total mid-run, not sit
    at 0 until done (the 1 GB symptom: chunks flowing, bar at 0.0%)."""
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 59772,
                   'total': 131072, 'throughput': 13070.7}, f)
    state = client.get('/api/status').get_json()['transfer']
    assert state['progress'] == round(59772 / 131072 * 100, 1)
    assert state['chunks_sent'] == 59772
    assert state['total_chunks'] == 131072


def test_recovery_measured_on_advance_after_link_down(dash):
    """Chunks moving again after a confirmed link-down stamps recovery_time and
    logs the [RECOVERY] line once — the teacher's number."""
    import time
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 5,
                   'total': 10, 'throughput': 1.0}, f)
    client.get('/api/status')
    # Simulate the confirmed link-down, aged past the straggler window.
    dash.transfer_state['link_down_at'] = time.time() - 10
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 6,
                   'total': 10, 'throughput': 1.0}, f)
    state = client.get('/api/status').get_json()['transfer']
    assert state['recovery_time'] is not None
    assert state['recovery_time'] >= 0
    assert any('[RECOVERY]' in e for e in dash.event_log)
    # A further advance must not re-measure or double-log.
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 7,
                   'total': 10, 'throughput': 1.0}, f)
    client.get('/api/status')
    assert sum('[RECOVERY]' in e for e in dash.event_log) == 1


def test_straggler_advance_does_not_spend_recovery(dash):
    """An in-flight ACK landing just after the down-confirm is not recovery:
    the clock keeps running until a genuinely later advance (the 0.3 s
    bogus-measurement from the VM log)."""
    import time
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 5,
                   'total': 10, 'throughput': 1.0}, f)
    client.get('/api/status')
    dash.transfer_state['link_down_at'] = time.time()
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 6,
                   'total': 10, 'throughput': 1.0}, f)
    state = client.get('/api/status').get_json()['transfer']
    assert state['recovery_time'] is None
    assert not any('[RECOVERY]' in e for e in dash.event_log)
    assert dash.transfer_state['link_down_at'] is not None
    # Age the clock; the next advance is real resumption and measures.
    dash.transfer_state['link_down_at'] = time.time() - 30
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 7,
                   'total': 10, 'throughput': 1.0}, f)
    state = client.get('/api/status').get_json()['transfer']
    assert state['recovery_time'] is not None
    assert state['recovery_time'] >= 30
    assert sum('[RECOVERY]' in e for e in dash.event_log) == 1


def test_done_logs_duration_summary(dash):
    """First sight of done logs one [DONE] line with chunks, seconds, speed."""
    client = dash.app.test_client()
    first = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    my_id = first.get_json()['request_id']
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'request_id': my_id, 'running': True,
                   'chunk': 10, 'total': 10, 'throughput': 42.0}, f)
    client.get('/api/status')
    with open(dash.RESULT_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'request_id': my_id, 'status': 'done'}, f)
    state = client.get('/api/status').get_json()['transfer']
    assert state['duration'] is not None
    done = [e for e in dash.event_log if '[DONE]' in e]
    assert len(done) == 1
    assert '10/10 chunks' in done[0]
    # Second sight must not log again.
    client.get('/api/status')
    assert sum('[DONE]' in e for e in dash.event_log) == 1


def test_failed_logs_duration_summary(dash):
    client = dash.app.test_client()
    first = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    my_id = first.get_json()['request_id']
    with open(dash.RESULT_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'request_id': my_id, 'status': 'failed',
                   'error': '[CLIENT] boom'}, f)
    client.get('/api/status')
    fail = [e for e in dash.event_log if '[FAIL]' in e]
    assert len(fail) == 1
    assert 'boom' in fail[0]
