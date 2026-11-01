"""
Bridge retry and failover regressions — no Mininet needed.

The bridge owns the decision after a client failure: retry the same server
with the client's resume point, or restart on the backup server. These tests
pin the two paths and the cancel window.
"""

import importlib.util
import json
import os
import sys
import threading
import time

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'mininet', 'bridge.py')


def _load_bridge():
    spec = importlib.util.spec_from_file_location('bridge_retry_under_test', SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _point_at(mod, tmp_path):
    shared = str(tmp_path)
    mod.SHARED_DIR = shared
    mod.REQUEST_FILE = os.path.join(shared, 'transfer_request.json')
    mod.RESULT_FILE = os.path.join(shared, 'transfer_result.json')
    mod.PROGRESS_FILE = os.path.join(shared, 'transfer_progress.json')
    mod.EVENTS_FILE = os.path.join(shared, 'transfer_events.json')
    mod.CANCEL_FILE = os.path.join(shared, 'transfer_cancel.json')
    # Point this fixture at an empty status file location so unrelated host
    # state cannot leak into tests that do not set process state explicitly.
    mod.SERVER_STATUS_FILE = os.path.join(shared, 'server_status.json')
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * 100)
    return mod


def _result(status='failed', resume=None, server='h3', addr='10.0.0.3:9000',
            chunk=2, total=4):
    return {'status': status, 'error': None if status == 'done' else 'lost',
            'resume_point': resume, 'server': server, 'server_addr': addr,
            'chunk': chunk, 'total': total, 'throughput': 0.0}


def test_retry_same_server_with_resume(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    calls = []
    results = [_result(resume=2), _result(status='done', chunk=4)]

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return results.pop(0)

    monkeypatch.setattr(mod, 'run_client', fake_run)
    monkeypatch.setattr(mod, '_wait_for_server', lambda addr, rid: 'up')
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'done'
    assert out['attempts'] == 2
    assert out['resumed_from'] == 2
    assert calls[1]['resume_from'] == 2
    assert calls[1]['servers'] == ['10.0.0.3:9000']


def test_failover_to_h4_starts_at_zero(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    calls = []
    results = [_result(resume=2),
               _result(status='done', server='h4', addr='10.0.0.4:9000',
                       chunk=4)]

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return results.pop(0)

    monkeypatch.setattr(mod, 'run_client', fake_run)
    monkeypatch.setattr(mod, '_wait_for_server', lambda addr, rid: 'down')
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'done'
    assert out['restarted_on_backup'] is True
    assert out['attempts'] == 2
    assert calls[1]['resume_from'] == 0
    assert calls[1]['servers'] == ['10.0.0.4:9000']


def test_cancel_during_wait_stops(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    calls = []
    monkeypatch.setattr(mod, 'run_client',
                        lambda *a, **k: (calls.append(k), _result(resume=2))[1])
    monkeypatch.setattr(mod, '_wait_for_server', lambda addr, rid: 'cancelled')
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'stopped'
    assert len(calls) == 1


def test_failure_without_resume_point_does_not_retry(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    calls = []
    monkeypatch.setattr(mod, 'run_client',
                        lambda *a, **k: (calls.append(k), _result())[1])
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'failed'
    assert len(calls) == 1


def test_progress_holds_chunk_during_retry(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    seen = {}
    results = [_result(resume=2), _result(status='done', chunk=4)]

    def fake_run(*args, **kwargs):
        return results.pop(0)

    def fake_wait(addr, rid):
        with open(mod.PROGRESS_FILE) as f:
            seen.update(json.load(f))
        return 'up'

    monkeypatch.setattr(mod, 'run_client', fake_run)
    monkeypatch.setattr(mod, '_wait_for_server', fake_wait)
    mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert seen['phase'] == 'retrying'
    assert seen['chunk'] == 2
    assert seen['total'] == 4
    assert seen['running'] is True


def test_heartbeat_ticks_while_client_output_stalls(tmp_path, monkeypatch):
    """A stalled ACK leaves no client output; the bridge must stay alive."""
    mod = _point_at(_load_bridge(), tmp_path)
    heartbeats = []
    monkeypatch.setattr(mod, 'HEARTBEAT_INTERVAL', 0.01)
    monkeypatch.setattr(mod, 'write_heartbeat',
                        lambda started_at=None: heartbeats.append(time.time()))
    stall = threading.Event()

    class FakeStdout:
        def __iter__(self):
            yield ('[PROGRESS] {"chunk": 1, "total": 4, "bytes": 10, '
                   '"throughput": 1.0}')
            stall.wait(0.12)

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStdout()
            self.returncode = 1

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(mod.subprocess, 'Popen',
                        lambda *args, **kwargs: FakeProcess())
    out = mod.run_client('a.bin', os.path.join(str(tmp_path), 'a.bin'),
                         request_id='r-heartbeat')

    assert len(heartbeats) >= 2
    assert out['status'] == 'failed'


def test_server_process_state_distinguishes_alive_dead_unknown(tmp_path):
    mod = _point_at(_load_bridge(), tmp_path)
    with open(mod.SERVER_STATUS_FILE, 'w') as f:
        json.dump({'h3': {'alive': True}, 'h4': {'alive': False}}, f)
    assert mod._server_process_state('h3') is True
    assert mod._server_process_state('h4') is False

    old = time.time() - (mod.SERVER_STATUS_MAX_AGE + 1)
    os.utime(mod.SERVER_STATUS_FILE, (old, old))
    assert mod._server_process_state('h3') is None

    with open(mod.SERVER_STATUS_FILE, 'w') as f:
        f.write('not json')
    assert mod._server_process_state('h3') is None

    os.unlink(mod.SERVER_STATUS_FILE)
    assert mod._server_process_state('h3') is None


def test_path_wait_retries_until_recoverable(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    monkeypatch.setattr(mod, 'PROBE_INTERVAL', 0)
    attempts = []
    responses = iter([False, False, True])

    def fake_connect(addr, timeout=1):
        attempts.append(addr)
        return next(responses)

    monkeypatch.setattr(mod, '_can_connect', fake_connect)
    monkeypatch.setattr(mod, '_server_process_state', lambda host: True)

    assert mod._wait_for_path('10.0.0.3:9000', 'h3', 'r1', timeout=5) == 'up'
    assert len(attempts) == 3


def test_path_wait_honors_cancel(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    monkeypatch.setattr(mod, '_can_connect', lambda addr, timeout=1: False)
    monkeypatch.setattr(mod, '_server_process_state', lambda host: True)
    monkeypatch.setattr(mod, '_sleep_cancel', lambda seconds, rid: False)

    assert mod._wait_for_path('10.0.0.3:9000', 'h3', 'r1', timeout=5) == \
        'cancelled'


def test_process_alive_blocks_failover_to_backup(tmp_path, monkeypatch):
    """A broken path is not proof that the server process died."""
    mod = _point_at(_load_bridge(), tmp_path)
    with open(mod.SERVER_STATUS_FILE, 'w') as f:
        json.dump({'h3': {'alive': True}}, f)

    calls = []
    results = [_result(resume=2), _result(status='done', chunk=4)]

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return results.pop(0)

    monkeypatch.setattr(mod, 'run_client', fake_run)
    monkeypatch.setattr(mod, '_wait_for_server', lambda addr, rid: 'down')
    monkeypatch.setattr(mod, '_wait_for_path', lambda *args, **kwargs: 'up')
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'done'
    assert out['attempts'] == 2
    assert calls[1]['servers'] == ['10.0.0.3:9000']
    assert calls[1]['resume_from'] == 2


def test_process_dead_allows_failover_to_backup(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    with open(mod.SERVER_STATUS_FILE, 'w') as f:
        json.dump({'h3': {'alive': False}}, f)

    calls = []
    results = [_result(resume=2),
               _result(status='done', server='h4', addr='10.0.0.4:9000',
                       chunk=4)]

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return results.pop(0)

    monkeypatch.setattr(mod, 'run_client', fake_run)
    monkeypatch.setattr(mod, '_wait_for_server', lambda addr, rid: 'down')
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'done'
    assert out['restarted_on_backup'] is True
    assert calls[1]['servers'] == ['10.0.0.4:9000']
