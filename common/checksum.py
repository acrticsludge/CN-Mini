"""
CRC32 checksum utilities for per-chunk integrity verification.
Uses zlib.crc32 (built into Python, zero dependencies).
"""


def compute_crc32(data: bytes) -> int:
    """Compute CRC32 checksum of data, returned as unsigned 32-bit int."""
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


def verify_crc32(data: bytes, expected: int) -> bool:
    """Verify that data matches the expected CRC32 checksum."""
    return compute_crc32(data) == expected
