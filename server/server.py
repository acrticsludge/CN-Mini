"""
TCP Backup Server — receives file chunks, verifies CRC32, reassembles files.
Supports multiple concurrent clients via threading and interrupted-transfer recovery.
"""

import socket
import threading
import os
import struct
import json
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from common.protocol import *
from common.checksum import compute_crc32, verify_crc32


class ClientHandler(threading.Thread):
    """Handles a single client connection: receives chunks, verifies, writes to disk."""

    def __init__(self, conn, addr, storage_dir):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.storage_dir = storage_dir
        self.running = True
        self.filename = None
        self.file_size = 0
        self.total_chunks = 0
        self.received_chunks = 0
        self.file_handle = None
        self.output_path = None

    def run(self):
        try:
            while self.running:
                frame = self.recv_frame()
                if frame is None:
                    break
                self.handle_frame(frame)
        except Exception as e:
            print(f"[SERVER] Error handling {self.addr}: {e}")
        finally:
            self.cleanup()

    def recv_frame(self):
        """Receive a complete frame (header + payload) from the socket."""
        header = self.recv_all(HEADER_SIZE)
        if header is None:
            return None
        result = unpack_header(header)
        if result is None:
            return None
        msg_type, index, total_chunks, checksum, data_len = result
        payload = self.recv_all(data_len) if data_len > 0 else b''
        return {
            'type': msg_type,
            'index': index,
            'total_chunks': total_chunks,
            'checksum': checksum,
            'data_len': data_len,
            'payload': payload,
        }

    def recv_all(self, n):
        """Receive exactly n bytes from the socket."""
        data = b''
        while len(data) < n:
            try:
                chunk = self.conn.recv(n - len(data))
                if not chunk:
                    return None
                data += chunk
            except OSError:
                return None
        return data

    def handle_frame(self, frame):
        """Dispatch a received frame based on its message type."""
        msg_type = frame['type']

        if msg_type == MSG_HELLO:
            self._handle_hello(frame)
        elif msg_type == MSG_DATA:
            self._handle_data(frame)
        elif msg_type == MSG_RESUME:
            self._handle_resume(frame)

    def _handle_hello(self, frame):
        """Process HELLO: prepare output file and send ready ACK."""
        info = json.loads(frame['payload'].decode('utf-8'))
        self.filename = info['filename']
        self.file_size = info['file_size']
        self.total_chunks = (self.file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        self.output_path = os.path.join(self.storage_dir, self.filename)
        os.makedirs(self.storage_dir, exist_ok=True)
        self.file_handle = open(self.output_path, 'wb')
        self.send_ack(0)
        print(f"[SERVER] Receiving '{self.filename}' ({self.file_size} bytes, "
              f"{self.total_chunks} chunks) from {self.addr}")

    def _handle_data(self, frame):
        """Process DATA: verify CRC32, write chunk, send ACK."""
        if self.file_handle is None:
            return
        payload = frame['payload']
        expected_checksum = frame['checksum']
        if not verify_crc32(payload, expected_checksum):
            print(f"[SERVER] CRC32 mismatch on chunk {frame['index']}, dropping")
            return
        self.file_handle.write(payload)
        self.file_handle.flush()
        self.received_chunks += 1
        self.send_ack(frame['index'])
        if self.received_chunks >= self.total_chunks:
            elapsed = time.time() - getattr(self, '_start_time', time.time())
            print(f"[SERVER] Transfer complete: {self.filename} "
                  f"({self.received_chunks} chunks)")

    def _handle_resume(self, frame):
        """Process RESUME: confirm the resume point to the client."""
        last_ack = struct.unpack('!I', frame['payload'])[0]
        self.send_ack(last_ack)
        print(f"[SERVER] Resume confirmed from chunk {last_ack + 1}")

    def send_ack(self, chunk_index):
        """Send an ACK frame for the given chunk index."""
        payload = struct.pack('!I', chunk_index)
        frame = pack_frame(MSG_ACK, chunk_index, 0, 0, len(payload), payload)
        self.conn.sendall(frame)

    def cleanup(self):
        """Close file handle and socket."""
        if self.file_handle:
            self.file_handle.close()
        self.conn.close()
        print(f"[SERVER] Connection closed: {self.addr}")


class BackupServer:
    """Multi-threaded TCP server that receives file chunks and reassembles files."""

    def __init__(self, host='0.0.0.0', port=9000, storage_dir='/tmp/backup'):
        self.host = host
        self.port = port
        self.storage_dir = storage_dir
        self.running = False
        self.server_socket = None

    def start(self):
        """Start the server and accept connections indefinitely."""
        os.makedirs(self.storage_dir, exist_ok=True)
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(5)
        self.running = True
        print(f"[SERVER] Listening on {self.host}:{self.port}, "
              f"storage: {self.storage_dir}")

        try:
            while self.running:
                conn, addr = self.server_socket.accept()
                handler = ClientHandler(conn, addr, self.storage_dir)
                handler.start()
        except KeyboardInterrupt:
            print("\n[SERVER] Shutting down...")
        finally:
            self.stop()

    def stop(self):
        """Stop the server and close the listening socket."""
        self.running = False
        if self.server_socket:
            self.server_socket.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='TCP Backup Server')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9000)
    parser.add_argument('--storage', default='/tmp/backup')
    args = parser.parse_args()

    server = BackupServer(args.host, args.port, args.storage)
    server.start()
