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


class TestRecovery:
    """Test interrupted-transfer recovery via RESUME messages."""

    @pytest.fixture
    def server(self):
        """Start a backup server on a random port."""
        from server.server import BackupServer
        srv = BackupServer(host='127.0.0.1', port=19001, storage_dir='/tmp/test_recovery')
        t = threading.Thread(target=srv.start, daemon=True)
        t.start()
        time.sleep(0.5)
        yield srv
        srv.stop()

    def test_resume_after_partial_transfer(self, server):
        """Client sends some chunks, disconnects, resumes, file completes."""
        from client.client import BackupClient

        # Create a 10-chunk file (80 KB)
        content = b'R' * (CHUNK_SIZE * 10)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
            f.write(content)
            path = f.name

        try:
            # First attempt: send only 3 chunks then "crash"
            client = BackupClient([('127.0.0.1', 19001)])
            client.connect()

            filename = os.path.basename(path)
            file_size = len(content)
            total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE

            # Send HELLO
            hello = pack_hello(filename, file_size)
            client.sock.sendall(hello)
            client.recv_frame()  # ACK

            # Send only 3 chunks
            with open(path, 'rb') as f:
                for i in range(3):
                    chunk_data = f.read(CHUNK_SIZE)
                    checksum = compute_crc32(chunk_data)
                    frame = pack_data(i, total_chunks, chunk_data, checksum)
                    client.sock.sendall(frame)
                    client.recv_frame()  # ACK

            # Simulate crash: close without finishing
            client.sock.close()

            # Resume: reconnect and continue from chunk 3
            client2 = BackupClient([('127.0.0.1', 19001)])
            client2.last_ack = 2  # Last successfully ACKed chunk
            result = client2.send_file(path, resume_from=3)
            client2.close()

            assert result is True

            # Verify complete file
            received_path = f'/tmp/test_recovery/{filename}'
            assert os.path.exists(received_path)

            with open(path, 'rb') as f:
                original = f.read()
            with open(received_path, 'rb') as f:
                received = f.read()

            assert original == received
            os.unlink(received_path)
        finally:
            os.unlink(path)

    def test_resume_from_zero(self, server):
        """Resume from chunk 0 is equivalent to a fresh transfer."""
        from client.client import BackupClient

        content = b'S' * (CHUNK_SIZE * 3)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
            f.write(content)
            path = f.name

        try:
            client = BackupClient([('127.0.0.1', 19001)])
            result = client.send_file(path, resume_from=0)
            client.close()

            assert result is True

            filename = os.path.basename(path)
            received_path = f'/tmp/test_recovery/{filename}'
            assert os.path.exists(received_path)

            with open(path, 'rb') as f:
                original = f.read()
            with open(received_path, 'rb') as f:
                received = f.read()
            assert original == received
            os.unlink(received_path)
        finally:
            os.unlink(path)

    def test_server_handles_multiple_clients(self, server):
        """Server handles concurrent client connections."""
        from client.client import BackupClient

        results = []

        def send_file(thread_id):
            content = f'client{thread_id}'.encode() * CHUNK_SIZE
            with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
                f.write(content)
                path = f.name
            try:
                client = BackupClient([('127.0.0.1', 19001)])
                result = client.send_file(path)
                client.close()
                results.append(result)
            finally:
                os.unlink(path)

        threads = []
        for i in range(3):
            t = threading.Thread(target=send_file, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        assert all(results)
        assert len(results) == 3
