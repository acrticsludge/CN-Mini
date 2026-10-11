"""
Bridge singleton-lock regressions — no Mininet needed.

Every topology.py run spawns a bridge and nothing reaps the old one. Without
the lock, two bridges watch the same request file: duplicate transfers and
interleaved progress/result writes, surfacing as "stored but UI says failed".
"""

import importlib.util
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'mininet', 'bridge.py')


def _load_bridge(name='bridge_lock_under_test'):
    spec = importlib.util.spec_from_file_location(name, SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_second_live_bridge_is_refused(tmp_path):
    mod = _load_bridge()
    mod.LOCK_FILE = str(tmp_path / 'bridge.lock')
    assert mod.claim_bridge_lock() is True
    # Touched just now: a second bridge must back off, not race.
    assert mod.claim_bridge_lock() is False


def test_stale_lock_is_reclaimed(tmp_path):
    mod = _load_bridge()
    lock = tmp_path / 'bridge.lock'
    lock.write_text('dead-pid')
    old = time.time() - 120
    os.utime(str(lock), (old, old))
    mod.LOCK_FILE = str(lock)
    assert mod.claim_bridge_lock() is True
