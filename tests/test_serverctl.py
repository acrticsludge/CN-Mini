"""
Server control tests — no Mininet needed.

The kill path uses stored Popen handles, not pkill: Mininet hosts share the
host PID namespace, so a bare `pkill -f server.py` can kill both servers.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'mininet'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import serverctl


class FakeProc:
    def __init__(self, alive=True):
        self.pid = 1234
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def kill(self):
        self._alive = False


def test_kill_terminates_process(monkeypatch):
    proc = FakeProc()
    monkeypatch.setattr(serverctl, 'confirm_down',
                        lambda host, interval=None, log=print: True)
    ok, message, state, confirmed = serverctl.apply_server_state(
        None, {'h3': proc}, 'h3', 'down', interval=0)
    assert ok and confirmed
    assert state == 'down'
    assert proc.terminated
    assert 'down' in message


def test_kill_reports_still_answering(monkeypatch):
    proc = FakeProc()
    monkeypatch.setattr(serverctl, 'confirm_down',
                        lambda host, interval=None, log=print: False)
    ok, _, state, confirmed = serverctl.apply_server_state(
        None, {'h3': proc}, 'h3', 'down', interval=0)
    assert not ok and not confirmed
    assert state == 'up'


def test_kill_already_down_is_idempotent(monkeypatch):
    proc = FakeProc(alive=False)
    called = []
    monkeypatch.setattr(serverctl, 'confirm_down',
                        lambda *a, **k: called.append(1) or True)
    ok, message, state, confirmed = serverctl.apply_server_state(
        None, {'h3': proc}, 'h3', 'down')
    assert ok and confirmed and state == 'down'
    assert not called


def test_restart_starts_new_process(monkeypatch):
    old = FakeProc(alive=False)
    new = FakeProc()
    calls = []
    monkeypatch.setattr(serverctl, 'start_server',
                        lambda net, host: (calls.append(host), new)[1])
    procs = {'h3': old}
    ok, message, state, confirmed = serverctl.apply_server_state(
        None, procs, 'h3', 'up')
    assert ok and confirmed and state == 'up'
    assert procs['h3'] is new
    assert calls == ['h3']


def test_restart_already_up_is_idempotent(monkeypatch):
    proc = FakeProc()
    monkeypatch.setattr(serverctl, 'start_server',
                        lambda *a: (_ for _ in ()).throw(AssertionError))
    ok, message, state, _ = serverctl.apply_server_state(
        None, {'h3': proc}, 'h3', 'up')
    assert ok and state == 'up'
    assert 'already up' in message


def test_invalid_host_and_action():
    assert serverctl.apply_server_state(None, {}, 'h1', 'down')[0] is False
    assert serverctl.apply_server_state(None, {}, 'h3', 'boom')[0] is False


def test_server_status_reflects_exit():
    status = serverctl.server_status({'h3': FakeProc(), 'h4': FakeProc(False)})
    assert status['h3']['alive'] is True
    assert status['h3']['pid'] == 1234
    assert status['h4']['alive'] is False


def test_confirm_down_logs_two_probes(monkeypatch):
    monkeypatch.setattr(serverctl, '_can_connect', lambda host, timeout=1: False)
    logs = []
    assert serverctl.confirm_down('h3', interval=0, log=logs.append) is True
    assert any('1/2' in line for line in logs)
    assert any('2/2' in line for line in logs)


def test_confirm_down_stops_when_server_answers(monkeypatch):
    monkeypatch.setattr(serverctl, '_can_connect', lambda host, timeout=1: True)
    logs = []
    assert serverctl.confirm_down('h3', interval=0, log=logs.append) is False
    assert len(logs) == 1
