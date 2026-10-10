"""
Binary protocol for the Distributed Backup System.

Frame format:
  +--------+------+-------+------------+----------+----------+---------+
  | magic  | type | index | total_chks | checksum | data_len | payload |
  | 4B     | 1B   | 4B    | 4B         | 4B       | 4B       | <=8KB   |
  +--------+------+-------+------------+----------+----------+---------+

  magic      = 0xDEADBEEF (sanity check)
  type       = 0x01 DATA | 0x02 ACK | 0x03 RESUME | 0x04 HELLO
  index      = chunk index (0-based)
  total_chks = total chunks in file
  checksum   = CRC32 of payload
  data_len   = payload length in bytes
"""

import struct
import json

MAGIC = 0xDEADBEEF
HEADER_FORMAT = '!IBIIII'  # magic(4), type(1), index(4), total(4), checksum(4), data_len(4)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 21 bytes

# Message types
MSG_DATA = 0x01
MSG_ACK = 0x02
MSG_RESUME = 0x03
MSG_HELLO = 0x04

CHUNK_SIZE = 8192  # 8 KB


def pack_frame(msg_type: int, index: int, total_chunks: int,
               checksum: int, data_len: int, payload: bytes = b'') -> bytes:
    """Pack a complete frame with header + payload."""
    header = struct.pack(HEADER_FORMAT, MAGIC, msg_type, index,
                         total_chunks, checksum, data_len)
    return header + payload


def unpack_header(data: bytes):
    """Unpack header bytes into a tuple. Returns None if invalid."""
    if len(data) < HEADER_SIZE:
        return None
    magic, msg_type, index, total_chunks, checksum, data_len = \
        struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    if magic != MAGIC:
        return None
    return msg_type, index, total_chunks, checksum, data_len


def unpack_frame(data: bytes):
    """Unpack a complete frame (header + payload). Returns dict or None."""
    result = unpack_header(data)
    if result is None:
        return None
    msg_type, index, total_chunks, checksum, data_len = result
    payload = data[HEADER_SIZE:HEADER_SIZE + data_len]
    return {
        'type': msg_type,
        'index': index,
        'total_chunks': total_chunks,
        'checksum': checksum,
        'data_len': data_len,
        'payload': payload,
    }


def pack_hello(filename: str, file_size: int) -> bytes:
    """Create a HELLO frame with filename and file_size as JSON payload."""
    payload = json.dumps({'filename': filename, 'file_size': file_size}).encode('utf-8')
    return pack_frame(MSG_HELLO, 0, 0, 0, len(payload), payload)


def unpack_hello(payload: bytes) -> dict:
    """Parse HELLO payload into a dict with filename and file_size."""
    return json.loads(payload.decode('utf-8'))


def pack_data(chunk_index: int, total_chunks: int, chunk_data: bytes,
              checksum: int) -> bytes:
    """Create a DATA frame with file chunk and its CRC32 checksum."""
    return pack_frame(MSG_DATA, chunk_index, total_chunks, checksum,
                      len(chunk_data), chunk_data)


def pack_ack(chunk_index: int) -> bytes:
    """Create an ACK frame acknowledging a specific chunk index."""
    payload = struct.pack('!I', chunk_index)
    return pack_frame(MSG_ACK, chunk_index, 0, 0, len(payload), payload)


def unpack_ack(payload: bytes) -> int:
    """Parse ACK payload to get the acknowledged chunk index."""
    return struct.unpack('!I', payload)[0]


def pack_resume(last_ack: int) -> bytes:
    """Create a RESUME frame with the last acknowledged chunk index."""
    payload = struct.pack('!I', last_ack)
    return pack_frame(MSG_RESUME, last_ack, 0, 0, len(payload), payload)


def unpack_resume(payload: bytes) -> int:
    """Parse RESUME payload to get the last acknowledged chunk index."""
    return struct.unpack('!I', payload)[0]
