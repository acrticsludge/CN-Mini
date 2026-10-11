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

from common.protocol import *
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
