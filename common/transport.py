"""
Shared TCP transport utilities for the Distributed Backup System.
Used by both client and server to send/receive protocol frames.
"""

import struct
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from common.protocol import HEADER_SIZE, unpack_header


def recv_all(sock, n):
    """Receive exactly n bytes from the socket. Returns None on disconnect."""
    data = b''
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        except OSError:
            return None
    return data


def recv_frame(sock):
    """Receive a complete frame (header + payload) from the socket."""
    header = recv_all(sock, HEADER_SIZE)
    if header is None:
        return None
    result = unpack_header(header)
    if result is None:
        return None
    msg_type, index, total_chunks, checksum, data_len = result
    payload = recv_all(sock, data_len) if data_len > 0 else b''
    return {
        'type': msg_type,
        'index': index,
        'total_chunks': total_chunks,
        'checksum': checksum,
        'data_len': data_len,
        'payload': payload,
    }
