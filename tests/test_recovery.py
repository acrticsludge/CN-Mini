"""
Tests for interrupted-transfer recovery: kill client mid-transfer,
restart with RESUME, file completes correctly.
"""

import os
import sys
import time
import socket
import tempfile
import threading
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.protocol import (CHUNK_SIZE, MSG_ACK, MSG_HELLO, pack_hello, pack_data,
                              pack_ack, unpack_ack)
from common.checksum import compute_crc32
from common.transport import recv_frame


def free_port():
    """Grab an ephemeral port so repeated tests never collide."""
    import socket as s
    with s.socket(s.AF_INET, s.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def cleanup_dir(path):
    """Remove a temp storage dir, retrying.

    On Windows the server's handler thread may still hold the output file open
    when the test finishes, so an immediate unlink raises PermissionError.
    Deleting the whole directory with a short retry avoids that race.
    """
    import shutil
    for _ in range(20):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.1)


class TestRecovery:
    """Test interrupted-transfer recovery via RESUME messages."""

    @pytest.fixture
    def server(self):
        """Start a backup server on a free port; reports (server, port, storage)."""
        from server.server import BackupServer
        port = free_port()
        storage = os.path.join(tempfile.gettempdir(), f'test_recovery_{port}')
        os.makedirs(storage, exist_ok=True)
        srv = BackupServer(host='127.0.0.1', port=port, storage_dir=storage)
        t = threading.Thread(target=srv.start, daemon=True)
        t.start()
        time.sleep(0.5)
        yield srv, port, storage
        srv.stop()
        cleanup_dir(storage)

    def test_resume_after_partial_transfer(self, server):
        """Client sends some chunks, disconnects, resumes, file completes."""
        from client.client import BackupClient
        _, port, storage = server

        content = b'R' * (CHUNK_SIZE * 10)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
            f.write(content)
            path = f.name

        try:
            filename = os.path.basename(path)
            file_size = len(content)
            total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE

            # First attempt: 3 chunks, then an abrupt disconnect.
            client = BackupClient([('127.0.0.1', port)])
            client.connect()
            client.sock.sendall(pack_hello(filename, file_size))
            assert recv_frame(client.sock)['type'] == MSG_ACK

            with open(path, 'rb') as f:
                for i in range(3):
                    chunk_data = f.read(CHUNK_SIZE)
                    frame = pack_data(i, total_chunks, chunk_data,
                                      compute_crc32(chunk_data))
                    client.sock.sendall(frame)
                    ack = recv_frame(client.sock)
                    assert ack is not None and unpack_ack(ack['payload']) == i
            received_path = os.path.join(storage, filename)
            assert os.path.getsize(received_path) == 3 * CHUNK_SIZE
            client.sock.close()

            # Resume: the server must keep the first 3 chunks, not truncate them.
            client2 = BackupClient([('127.0.0.1', port)])
            client2.last_ack = 2
            result = client2.send_file(path, resume_from=3)
            client2.close()

            assert result is True
            with open(path, 'rb') as f:
                original = f.read()
            with open(received_path, 'rb') as f:
                received = f.read()
            assert original == received
        finally:
            os.unlink(path)

    def test_resume_from_zero(self, server):
        """Resume from chunk 0 is equivalent to a fresh transfer."""
        from client.client import BackupClient
        _, port, storage = server

        content = b'S' * (CHUNK_SIZE * 3)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
            f.write(content)
            path = f.name

        try:
            client = BackupClient([('127.0.0.1', port)])
            result = client.send_file(path, resume_from=0)
            client.close()

            assert result is True

            received_path = os.path.join(storage, os.path.basename(path))
            assert os.path.exists(received_path)

            with open(path, 'rb') as f:
                original = f.read()
            with open(received_path, 'rb') as f:
                received = f.read()
            assert original == received
        finally:
            os.unlink(path)

    def test_server_handles_multiple_clients(self, server):
        """Server handles concurrent client connections."""
        from client.client import BackupClient
        _, port, storage = server

        results = []

        def send_file(thread_id):
            content = f'client{thread_id}'.encode() * CHUNK_SIZE
            with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
                f.write(content)
                path = f.name
            try:
                client = BackupClient([('127.0.0.1', port)])
                results.append(client.send_file(path))
                client.close()
            finally:
                os.unlink(path)

        threads = []
        for i in range(3):
            t = threading.Thread(target=send_file, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        assert len(results) == 3
        assert all(results)

        assert all(results)
        assert len(results) == 3


class TestStallRecovery:
    """Mid-transfer blackhole: ACKs stall, then resume on the healed path.

    This is the demo scenario (link killed mid-transfer): the run must survive
    a stall longer than the old 10 s socket timeout and finish with zero
    rework, while a shorter transfer timeout still fails fast. The 12 s stall
    sits past the old death line on purpose.
    """

    @staticmethod
    def _stalling_server(port, stall, received):
        """Fake server: ready-ACKs, then holds the first DATA ACK for `stall`
        seconds (the blackhole), then serves the rest normally."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', port))
        srv.listen(1)
        conn, _ = srv.accept()
        try:
            conn.settimeout(60)
            hello = recv_frame(conn)
            assert hello is not None and hello['type'] == MSG_HELLO
            conn.sendall(pack_ack(0))
            data = recv_frame(conn)
            time.sleep(stall)
            conn.sendall(pack_ack(data['index']))
            received.append(data['payload'])
            while True:
                nxt = recv_frame(conn)
                if nxt is None:
                    break
                conn.sendall(pack_ack(nxt['index']))
                received.append(nxt['payload'])
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
            srv.close()

    def _run_against_stall(self, stall, **client_kwargs):
        from client.client import BackupClient
        content = b'S' * CHUNK_SIZE  # single chunk keeps the test fast
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
            f.write(content)
            path = f.name
        port = free_port()
        received = []
        t = threading.Thread(target=self._stalling_server,
                             args=(port, stall, received), daemon=True)
        t.start()
        time.sleep(0.3)
        try:
            client = BackupClient([('127.0.0.1', port)], **client_kwargs)
            start = time.time()
            ok = client.send_file(path)
            elapsed = time.time() - start
            client.close()
        finally:
            os.unlink(path)
            t.join(timeout=30)
        return ok, elapsed, content, received

    def test_stalled_run_survives_with_default_timeout(self):
        ok, elapsed, content, received = self._run_against_stall(12)
        assert ok is True
        assert elapsed >= 12  # it really waited out the blackhole
        assert b''.join(received) == content

    def test_stalled_run_fails_fast_with_short_timeout(self):
        ok, _, _, _ = self._run_against_stall(12, transfer_timeout=2)
        assert ok is False

    def test_transfer_phase_uses_transfer_timeout(self):
        """The socket carries the transfer timeout after connecting, not the
        connect timeout — the mechanism the stall test relies on."""
        from client.client import BackupClient
        from server.server import BackupServer
        port = free_port()
        storage = os.path.join(tempfile.gettempdir(), f'test_stall_{port}')
        os.makedirs(storage, exist_ok=True)
        srv = BackupServer(host='127.0.0.1', port=port, storage_dir=storage)
        t = threading.Thread(target=srv.start, daemon=True)
        t.start()
        time.sleep(0.5)
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
                f.write(b'Q' * 100)
                path = f.name
            try:
                client = BackupClient([('127.0.0.1', port)], connect_timeout=10,
                                      transfer_timeout=45)
                assert client.send_file(path) is True
                assert client.sock.gettimeout() == 45
                client.close()
            finally:
                os.unlink(path)
        finally:
            srv.stop()
            cleanup_dir(storage)
