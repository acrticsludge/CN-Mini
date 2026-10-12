"""
TCP Backup Client — reads a file, splits into 8KB chunks, sends with CRC32 checksums.
Supports interrupted-transfer recovery via RESUME messages and multi-server failover.
"""

import socket
import json
import os
import sys
import time
import struct

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from common.protocol import (
    HEADER_SIZE, MSG_ACK, CHUNK_SIZE,
    pack_frame, unpack_header, pack_hello, pack_data, pack_resume,
)
from common.checksum import compute_crc32
from common.transport import recv_all, recv_frame


class BackupClient:
    """TCP client that sends file chunks to backup servers with integrity and resume support."""

    def __init__(self, servers, chunk_size=CHUNK_SIZE,
                 connect_timeout=10, transfer_timeout=60):
        self.servers = servers
        self.chunk_size = chunk_size
        # Two different waits: connecting must fail fast (server truly down),
        # but a live transfer must out-wait a link-failure blackhole while TCP
        # retransmits and STP unblocks the alternate path (~tens of seconds).
        self.connect_timeout = connect_timeout
        self.transfer_timeout = transfer_timeout
        self.sock = None
        self.connected = False
        self.current_server = None
        self.last_ack = -1

    def connect(self):
        """Connect to the first available server, trying each in order."""
        for host, port in self.servers:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(self.connect_timeout)
                self.sock.connect((host, port))
                self.connected = True
                self.current_server = (host, port)
                print(f"[CLIENT] Connected to {host}:{port}")
                return True
            except Exception as e:
                print(f"[CLIENT] Failed to connect to {host}:{port}: {e}")
                if self.sock:
                    self.sock.close()
                self.sock = None
        return False

    def send_file(self, filepath, resume_from=0):
        """Send a file to the server. Returns True on success."""
        if not os.path.exists(filepath):
            print(f"[CLIENT] File not found: {filepath}")
            return False

        filename = os.path.basename(filepath)
        file_size = os.path.getsize(filepath)
        total_chunks = (file_size + self.chunk_size - 1) // self.chunk_size
        if not self.connected:
            if not self.connect():
                return False

        # Widen the timeout for the transfer phase. A mid-transfer link failure
        # stalls ACKs until the controller reroutes and STP reconverges; killing
        # the run at 10 s would waste everything the reroute was meant to save.
        self.sock.settimeout(self.transfer_timeout)

        start_chunk = resume_from if resume_from > 0 else 0

        # Send HELLO. The resume offset tells the server to keep an existing
        # partial file; without it the server truncates (correct for a fresh
        # transfer, which is what resume_from=0 means).
        hello = pack_hello(filename, file_size, resume_from=start_chunk)
        try:
            self.sock.sendall(hello)
        except Exception as e:
            print(f"[CLIENT] Failed to send HELLO: {e}")
            return False

        # Receive ready ACK
        ack_frame = recv_frame(self.sock)
        if ack_frame is None or ack_frame['type'] != MSG_ACK:
            print("[CLIENT] Did not receive ready ACK")
            return False

        # If resuming, send RESUME first
        if resume_from > 0 and self.last_ack >= 0:
            resume_frame = pack_resume(self.last_ack)
            self.sock.sendall(resume_frame)
            ack_frame = recv_frame(self.sock)
            if ack_frame is None or ack_frame['type'] != MSG_ACK:
                print("[CLIENT] Did not receive RESUME ACK")
                return False
            print(f"[CLIENT] Resuming from chunk {self.last_ack + 1}")

        start_chunk = resume_from if resume_from > 0 else 0
        return self._send_chunks(filepath, start_chunk, total_chunks)

    def _send_chunks(self, filepath, start_chunk, total_chunks):
        """Send file chunks from start_chunk to total_chunks. Returns True on success."""
        bytes_sent = start_chunk * self.chunk_size
        start_time = time.time()

        with open(filepath, 'rb') as f:
            f.seek(start_chunk * self.chunk_size)
            for chunk_index in range(start_chunk, total_chunks):
                chunk_data = f.read(self.chunk_size)
                if not chunk_data:
                    break

                checksum = compute_crc32(chunk_data)
                frame = pack_data(chunk_index, total_chunks, chunk_data, checksum)

                try:
                    self.sock.sendall(frame)
                except Exception as e:
                    print(f"[CLIENT] Connection lost at chunk {chunk_index}: {e}")
                    self.last_ack = chunk_index - 1
                    return False

                # Wait for ACK
                ack_frame = recv_frame(self.sock)
                if ack_frame is None:
                    print(f"[CLIENT] No ACK received for chunk {chunk_index}")
                    self.last_ack = chunk_index - 1
                    return False

                acked_index = struct.unpack('!I', ack_frame['payload'])[0]
                self.last_ack = acked_index
                bytes_sent += len(chunk_data)

                self._report_progress(chunk_index, total_chunks, start_time, bytes_sent)

        self._report_progress(total_chunks - 1, total_chunks, start_time, bytes_sent)
        summary = {
            'filename': os.path.basename(filepath),
            'chunks': self.last_ack + 1,
            'bytes': bytes_sent,
            'throughput': round(bytes_sent / max(time.time() - start_time, 1e-6) / 1024, 1),
        }
        print('[COMPLETE] ' + json.dumps(summary), flush=True)

        elapsed = time.time() - start_time
        throughput = bytes_sent / elapsed / 1024 if elapsed > 0 else 0
        filename = os.path.basename(filepath)
        print(f"[CLIENT] Transfer complete: {filename} "
              f"({bytes_sent} bytes in {elapsed:.2f}s, {throughput:.1f} KB/s)")
        return True

    def _report_progress(self, chunk_index, total_chunks, start_time, bytes_sent):
        """Emit a machine-readable progress line for the bridge to parse."""
        elapsed = max(time.time() - start_time, 1e-6)
        payload = {
            'chunk': chunk_index + 1,
            'total': total_chunks,
            'bytes': bytes_sent,
            'throughput': round(bytes_sent / elapsed / 1024, 1),
        }
        print('[PROGRESS] ' + json.dumps(payload), flush=True)
        if chunk_index % 10 == 0:
            pct = (chunk_index + 1) / total_chunks * 100
            print(f"[CLIENT] Progress: {pct:.1f}% "
                  f"({chunk_index + 1}/{total_chunks} chunks)", flush=True)

    def close(self):
        """Close the connection."""
        if self.sock:
            self.sock.close()
        self.connected = False


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='TCP Backup Client')
    parser.add_argument('file', help='File to send')
    parser.add_argument('--servers', nargs='+', default=['10.0.0.3:9000', '10.0.0.4:9000'])
    parser.add_argument('--resume', type=int, default=0, help='Resume from chunk index')
    parser.add_argument('--connect-timeout', type=float, default=10,
                        help='Seconds to wait per server when connecting')
    parser.add_argument('--transfer-timeout', type=float, default=60,
                        help='Seconds to wait for an ACK mid-transfer (must cover '
                             'STP reconvergence so a link failure stalls, not kills)')
    args = parser.parse_args()

    servers = []
    for s in args.servers:
        host, port = s.split(':')
        servers.append((host, int(port)))

    client = BackupClient(servers, connect_timeout=args.connect_timeout,
                          transfer_timeout=args.transfer_timeout)
    try:
        ok = client.send_file(args.file, resume_from=args.resume)
    finally:
        client.close()
    # Propagate the outcome. Exiting 0 here would make a failed transfer look
    # successful to anything that only checks the exit code (the bridge does).
    sys.exit(0 if ok else 1)
