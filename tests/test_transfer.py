"""
Tests for basic file transfer: client sends a file, server reassembles it,
and the received file matches the original.
"""

import os
import sys
import time
import socket
import struct
import tempfile
import threading
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.protocol import *
from common.checksum import compute_crc32, verify_crc32


class TestProtocol:
    """Test frame packing/unpacking and CRC32 checksums."""

    def test_crc32_computation(self):
        data = b'hello world'
        checksum = compute_crc32(data)
        assert checksum == 0x0d4a1185
        assert verify_crc32(data, checksum)

    def test_crc32_detects_corruption(self):
        data = b'hello world'
        checksum = compute_crc32(data)
        corrupted = b'hello worlD'
        assert not verify_crc32(corrupted, checksum)

    def test_pack_unpack_frame(self):
        payload = b'test payload data'
        frame = pack_frame(MSG_DATA, 3, 10, 12345, len(payload), payload)
        result = unpack_frame(frame)
        assert result is not None
        assert result['type'] == MSG_DATA
        assert result['index'] == 3
        assert result['total_chunks'] == 10
        assert result['checksum'] == 12345
        assert result['data_len'] == len(payload)
        assert result['payload'] == payload

    def test_pack_unpack_hello(self):
        frame = pack_hello('test.txt', 1024)
        result = unpack_frame(frame)
        assert result['type'] == MSG_HELLO
        info = unpack_hello(result['payload'])
        assert info['filename'] == 'test.txt'
        assert info['file_size'] == 1024

    def test_pack_unpack_ack(self):
        frame = pack_ack(42)
        result = unpack_frame(frame)
        assert result['type'] == MSG_ACK
        assert unpack_ack(result['payload']) == 42

    def test_pack_unpack_resume(self):
        frame = pack_resume(7)
        result = unpack_frame(frame)
        assert result['type'] == MSG_RESUME
        assert unpack_resume(result['payload']) == 7

    def test_invalid_magic_rejected(self):
        frame = struct.pack('!IBIIII', 0x12345678, MSG_DATA, 0, 0, 0, 0)
        result = unpack_frame(frame)
        assert result is None

    def test_short_frame_rejected(self):
        result = unpack_frame(b'\x00\x01\x02')
        assert result is None


class TestFileTransfer:
    """Integration test: client sends a file, server reassembles it."""

    @pytest.fixture
    def temp_file(self):
        """Create a temporary test file with known content."""
        content = b'A' * 16384  # 16 KB = 2 chunks
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
            f.write(content)
            path = f.name
        yield path
        os.unlink(path)

    @pytest.fixture
    def server(self):
        """Start a backup server on a random port."""
        from server.server import BackupServer
        srv = BackupServer(host='127.0.0.1', port=19000, storage_dir='/tmp/test_backup')
        t = threading.Thread(target=srv.start, daemon=True)
        t.start()
        time.sleep(0.5)
        yield srv
        srv.stop()

    def test_basic_transfer(self, temp_file, server):
        """Client sends a file, server receives it correctly."""
        from client.client import BackupClient

        client = BackupClient([('127.0.0.1', 19000)])
        result = client.send_file(temp_file)
        client.close()

        assert result is True

        # Verify file was received
        filename = os.path.basename(temp_file)
        received_path = f'/tmp/test_backup/{filename}'
        assert os.path.exists(received_path)

        with open(temp_file, 'rb') as f:
            original = f.read()
        with open(received_path, 'rb') as f:
            received = f.read()

        assert original == received

        # Cleanup
        os.unlink(received_path)

    def test_multiple_chunks(self, server):
        """File larger than one chunk is sent correctly."""
        from client.client import BackupClient

        # Create a 5-chunk file (40 KB)
        content = b'X' * (CHUNK_SIZE * 5)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
            f.write(content)
            path = f.name

        try:
            client = BackupClient([('127.0.0.1', 19000)])
            result = client.send_file(path)
            client.close()
            assert result is True

            filename = os.path.basename(path)
            received_path = f'/tmp/test_backup/{filename}'
            assert os.path.exists(received_path)

            with open(path, 'rb') as f:
                original = f.read()
            with open(received_path, 'rb') as f:
                received = f.read()
            assert original == received
            os.unlink(received_path)
        finally:
            os.unlink(path)

    def test_server_rejects_corrupted_chunk(self, server):
        """Server detects CRC32 mismatch and drops the corrupted chunk."""
        from client.client import BackupClient

        content = b'Z' * CHUNK_SIZE
        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as f:
            f.write(content)
            path = f.name

        try:
            client = BackupClient([('127.0.0.1', 19000)])
            # Manually send a corrupted frame
            client.connect()
            hello = pack_hello(os.path.basename(path), len(content))
            client.sock.sendall(hello)
            client.recv_frame()  # ACK

            # Send corrupted chunk
            bad_data = b'corrupted!'
            bad_checksum = compute_crc32(b'different data')
            frame = pack_data(0, 1, bad_data, bad_checksum)
            client.sock.sendall(frame)
            client.recv_frame()  # ACK

            client.close()
            # The corrupted chunk should have been dropped
        finally:
            os.unlink(path)
