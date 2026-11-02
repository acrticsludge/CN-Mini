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
from common.protocol import (
    HEADER_SIZE, MSG_HELLO, MSG_DATA, MSG_RESUME, MSG_ACK, MSG_ERROR, CHUNK_SIZE,
    pack_frame, unpack_header, unpack_frame, pack_hello, pack_data, pack_ack,
)
from common.checksum import verify_crc32
from common.transport import recv_all, recv_frame

HOST_NAMES = {'10.0.0.3': 'h3', '10.0.0.4': 'h4'}
DEFAULT_QUOTA_FILE = '/tmp/shared/server_storage.json'

# Quota admission is check-then-write across handler threads, so a lock plus a
# per-storage reservation keeps two concurrent transfers from both claiming the
# same free space. A filename already in flight is refused too: both would write
# the same .part path, and a second writer also invalidates the first one's
# replacement accounting.
_admission_lock = threading.Lock()
_reserved_bytes = {}
_inflight_names = {}


def _reserved_for(storage_dir):
    return sum(_reserved_bytes.get(storage_dir, {}).values())


def quota_bytes(path, host):
    """Return the host's quota in bytes from the dashboard's JSON, or None.

    Accepts both the current {'h3': {'quota_mb': n}} shape and the old flat
    {'h3': n} one. None means unlimited (quota 0) or no quota file.
    """
    if not path or not host:
        return None
    try:
        with open(path) as f:
            entry = json.load(f).get(host)
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(entry, dict):
        entry = entry.get('quota_mb')
    try:
        quota_mb = int(entry)
    except (TypeError, ValueError):
        return None
    return None if quota_mb == 0 else quota_mb * 1024 * 1024


class ClientHandler(threading.Thread):
    """Handles a single client connection: receives chunks, verifies, writes to disk."""

    def __init__(self, conn, addr, storage_dir, name=None, quota_file=None):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.storage_dir = storage_dir
        self.name = name
        self.quota_file = quota_file
        self.running = True
        self.filename = None
        self.file_size = 0
        self.total_chunks = 0
        self.received = set()
        self.file_handle = None
        self.output_path = None
        self.part_path = None
        self.reserved = 0
        self.claimed = False

    def run(self):
        try:
            while self.running:
                frame = recv_frame(self.conn)
                if frame is None:
                    break
                self.handle_frame(frame)
        except Exception as e:
            print(f"[SERVER] Error handling {self.addr}: {e}")
        finally:
            self.cleanup()

    def handle_frame(self, frame):
        """Dispatch a received frame based on its message type."""
        msg_type = frame['type']

        if msg_type == MSG_HELLO:
            self._handle_hello(frame)
        elif msg_type == MSG_DATA:
            self._handle_data(frame)
        elif msg_type == MSG_RESUME:
            self._handle_resume(frame)

    def _used_bytes(self):
        """Bytes of completed files in storage; .part copies are not committed yet."""
        total = 0
        for name in os.listdir(self.storage_dir):
            full = os.path.join(self.storage_dir, name)
            if os.path.isfile(full) and not name.endswith('.part'):
                try:
                    total += os.path.getsize(full)
                except OSError:
                    pass
        return total

    def _handle_hello(self, frame):
        """Process HELLO: prepare output file and send the accepted start chunk.

        A resume is accepted only when the file on disk covers that offset.
        Otherwise the client is told to start at 0, which keeps a failover to
        an empty server from writing chunk N at offset 0.
        """
        info = json.loads(frame['payload'].decode('utf-8'))
        raw_name = str(info.get('filename', ''))
        self.filename = os.path.basename(raw_name.replace('\\', '/'))
        if not self.filename or self.filename in ('.', '..'):
            print(f"[SERVER] Invalid filename from {self.addr}")
            self.conn.close()
            return
        self.file_size = int(info['file_size'])
        resume_from = int(info.get('resume_from', 0) or 0)
        self.total_chunks = (self.file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        self.output_path = os.path.join(self.storage_dir, self.filename)
        # The in-progress copy stays hidden from /api/files until it is whole.
        self.part_path = self.output_path + '.part'
        try:
            os.makedirs(self.storage_dir, exist_ok=True)
        except OSError as e:
            self.send_error(f'disk full or read-only: {e}')
            return

        # The dashboard's quota is advisory unless the receiver enforces it:
        # a failover can reach a server whose free space was checked before it
        # filled up, and a direct client never checks at all. Refuse the whole
        # file here, before truncating any partial, and let the client report
        # "server storage error" instead of writing past the quota.
        limit = quota_bytes(self.quota_file, self.name)
        if limit is not None:
            conflict = False
            with _admission_lock:
                names = _inflight_names.setdefault(self.storage_dir, set())
                if self.filename in names:
                    conflict = True
                else:
                    # Size the replacement and read usage under the same lock
                    # that _finish() holds while promoting, so a concurrent
                    # 900 KB -> 100 KB replacement cannot leave this handler
                    # sizing against the old file.
                    existing = (os.path.getsize(self.output_path)
                                if os.path.isfile(self.output_path) else 0)
                    need = max(self.file_size - existing, 0)
                    used = self._used_bytes() + _reserved_for(self.storage_dir)
                    free = max(limit - used, 0)
                    admit = need <= free
                    if admit:
                        names.add(self.filename)
                        self.claimed = True
                        if need:
                            _reserved_bytes.setdefault(self.storage_dir, {})[self] = need
                            self.reserved = need
            if conflict:
                self.send_error(f'another transfer for {self.filename} is in '
                                f'progress on {self.name}')
                return
            if not admit:
                self.send_error(f'quota exceeded: {self.file_size} bytes needed, '
                                f'{free} free on {self.name}')
                return

        accepted = 0
        if 0 < resume_from <= self.total_chunks and os.path.exists(self.part_path):
            if os.path.getsize(self.part_path) >= resume_from * CHUNK_SIZE:
                accepted = resume_from
        self.received = set(range(accepted))
        try:
            self.file_handle = open(self.part_path, 'r+b' if accepted else 'wb')
        except OSError as e:
            self.send_error(f'disk full or read-only: {e}')
            return
        if accepted:
            self.file_handle.seek(accepted * CHUNK_SIZE)

        self._start_time = time.time()
        if self.file_size == 0:
            self._finish()
        elif accepted == self.total_chunks:
            # The stored copy already covers the whole file. Drop any old tail.
            self.file_handle.truncate(self.file_size)
            self._finish()
        self.send_ack(accepted)
        mode = f'resume from chunk {accepted}' if accepted else 'fresh transfer'
        print(f"[SERVER] Receiving '{self.filename}' ({self.file_size} bytes, "
              f"{self.total_chunks} chunks, {mode}) from {self.addr}")

    def _handle_data(self, frame):
        """Process DATA: verify CRC32, write at the chunk's index, send ACK."""
        if self.file_handle is None:
            return
        index = frame['index']
        if index < 0 or index >= self.total_chunks:
            return
        # A retry after a lost ACK sends the same index again. ACK it without
        # writing a second copy.
        if index in self.received:
            self.send_ack(index)
            return
        payload = frame['payload']
        expected_checksum = frame['checksum']
        if not verify_crc32(payload, expected_checksum):
            print(f"[SERVER] CRC32 mismatch on chunk {index}, dropping")
            return
        self.file_handle.seek(index * CHUNK_SIZE)
        try:
            self.file_handle.write(payload)
            self.file_handle.flush()
        except OSError as e:
            self.send_error(f'disk full or read-only: {e}')
            return
        self.received.add(index)
        if len(self.received) >= self.total_chunks:
            # A resume can sit on a longer old part file; cut the stale tail.
            self.file_handle.truncate(self.file_size)
            elapsed = time.time() - self._start_time
            print(f"[SERVER] Transfer complete: {self.filename} "
                  f"({len(self.received)} chunks in {elapsed:.2f}s)")
            self._finish()
        self.send_ack(index)

    def _handle_resume(self, frame):
        """Process RESUME: rewind to the resume point and confirm it to the client."""
        if self.file_handle is None:
            return
        last_ack = struct.unpack('!I', frame['payload'])[0] if len(frame['payload']) == 4 else -1
        if last_ack < 0:
            print(f"[SERVER] Malformed RESUME payload ({len(frame['payload'])} bytes)")
            return
        # Skip ahead to the first chunk the client still owes us.
        self.received = set(range(last_ack + 1))
        self.file_handle.seek((last_ack + 1) * CHUNK_SIZE)
        self.send_ack(last_ack)
        print(f"[SERVER] Resume confirmed from chunk {last_ack + 1} "
              f"(offset {(last_ack + 1) * CHUNK_SIZE} bytes)")

    def _finish(self):
        """Close the part file and atomically promote it to the final name."""
        try:
            if self.file_handle:
                self.file_handle.flush()
                self.file_handle.close()
                self.file_handle = None
            # Promote and stop reserving under the admission lock: a HELLO that
            # sizes a same-name replacement must see either the old file with
            # this transfer still claimed, or the new file with no reservation.
            with _admission_lock:
                os.replace(self.part_path, self.output_path)
                self._release_admission_locked()
        except OSError as e:
            self.send_error(f'disk full or read-only: {e}')
            return
        self._report_stored()

    def _release_admission_locked(self):
        if self.claimed:
            names = _inflight_names.get(self.storage_dir)
            if names is not None:
                names.discard(self.filename)
        if self.reserved:
            _reserved_bytes.get(self.storage_dir, {}).pop(self, None)
        self.claimed = False
        self.reserved = 0

    def _release_admission(self):
        if not self.reserved and not self.claimed:
            return
        with _admission_lock:
            self._release_admission_locked()

    def _report_stored(self):
        """Emit a machine-readable receipt so the bridge can verify server-side."""
        receipt = {
            'filename': self.filename,
            'size': os.path.getsize(self.output_path),
            'chunks': len(self.received),
        }
        print('[STORED] ' + json.dumps(receipt), flush=True)

    def send_ack(self, chunk_index):
        """Send an ACK frame for the given chunk index."""
        payload = struct.pack('!I', chunk_index)
        frame = pack_frame(MSG_ACK, chunk_index, 0, 0, len(payload), payload)
        self.conn.sendall(frame)

    def send_error(self, reason):
        """Send an MSG_ERROR frame (0x05, JSON {reason}) and close the connection.

        Used when the server cannot write to disk (full / read-only) so the
        client can fail fast instead of hanging on a missing ACK.
        """
        payload = json.dumps({'reason': reason}).encode('utf-8')
        frame = pack_frame(MSG_ERROR, 0, 0, 0, len(payload), payload)
        try:
            self.conn.sendall(frame)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def cleanup(self):
        """Close file handle and socket; never leave quota reserved for a dead run."""
        self._release_admission()
        if self.file_handle:
            self.file_handle.close()
        self.conn.close()
        print(f"[SERVER] Connection closed: {self.addr}")


class BackupServer:
    """Multi-threaded TCP server that receives file chunks and reassembles files."""

    def __init__(self, host='0.0.0.0', port=9000, storage_dir='/tmp/backup',
                 name=None, quota_file=None):
        self.host = host
        self.port = port
        self.storage_dir = storage_dir
        self.name = name
        self.quota_file = quota_file
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
                try:
                    conn, addr = self.server_socket.accept()
                except OSError:
                    # stop() closes the listening socket from another thread;
                    # that wakes accept() with an error on Windows.
                    if not self.running:
                        break
                    raise
                handler = ClientHandler(conn, addr, self.storage_dir,
                                        name=self.name,
                                        quota_file=self.quota_file)
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
    parser.add_argument('--name', default=None,
                        help='Quota key in the dashboard storage file (h3/h4)')
    parser.add_argument('--quota-file', default=DEFAULT_QUOTA_FILE,
                        help='JSON file with per-server quota_mb (default: %(default)s)')
    args = parser.parse_args()

    # Inside Mininet the bind address identifies the host; pass --name when the
    # server is launched some other way.
    server = BackupServer(args.host, args.port, args.storage,
                          name=args.name or HOST_NAMES.get(args.host),
                          quota_file=args.quota_file)
    server.start()
