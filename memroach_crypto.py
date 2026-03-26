#!/usr/bin/env python3
"""MemRoach Crypto — optional column-level encryption for blob content and chunk text.

Uses AES-CBC encryption via CockroachDB's encrypt()/decrypt() SQL functions.
When encryption_enabled is false (default), all functions are pass-through.

Configuration in memroach_config.json:
    "encryption_enabled": false,
    "encryption_key": ""        # 16, 24, or 32 byte key (hex-encoded)
"""

import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "memroach_config.json"

# Gzip magic bytes for fallback detection
_GZIP_MAGIC = b'\x1f\x8b'


def _load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _get_key_bytes(config: dict) -> bytes:
    """Get the encryption key as bytes. Supports hex-encoded or raw string keys."""
    key = config.get("encryption_key", "")
    if not key:
        raise ValueError("encryption_key not set in memroach_config.json")
    # Try hex decoding first
    try:
        key_bytes = bytes.fromhex(key)
        if len(key_bytes) in (16, 24, 32):
            return key_bytes
    except ValueError:
        pass
    # Fall back to raw string encoding, padded/truncated to valid AES key length
    key_bytes = key.encode("utf-8")
    if len(key_bytes) < 16:
        key_bytes = key_bytes.ljust(16, b'\0')
    elif len(key_bytes) < 24:
        key_bytes = key_bytes[:16]
    elif len(key_bytes) < 32:
        key_bytes = key_bytes[:24]
    else:
        key_bytes = key_bytes[:32]
    return key_bytes


def is_enabled(config: dict | None = None) -> bool:
    """Check if encryption is enabled in config."""
    if config is None:
        config = _load_config()
    return bool(config.get("encryption_enabled", False))


def encrypt_blob(conn, data: bytes, config: dict) -> bytes:
    """Encrypt blob data using CockroachDB's encrypt() function.

    If encryption is disabled, returns data unchanged.
    """
    if not config.get("encryption_enabled"):
        return data
    key = _get_key_bytes(config)
    rows = conn.run(
        "SELECT encrypt(:data, :key, 'aes')",
        data=data, key=key,
    )
    return rows[0][0]


def decrypt_blob(conn, data: bytes, config: dict) -> bytes:
    """Decrypt blob data using CockroachDB's decrypt() function.

    If encryption is disabled, returns data unchanged.
    Falls back to returning raw data if decryption fails (for pre-encryption blobs).
    """
    if not config.get("encryption_enabled"):
        return data
    key = _get_key_bytes(config)
    # Check if data is already unencrypted gzip
    if data[:2] == _GZIP_MAGIC:
        return data
    try:
        rows = conn.run(
            "SELECT decrypt(:data, :key, 'aes')",
            data=data, key=key,
        )
        result = rows[0][0]
        # Verify decryption produced valid gzip
        if result[:2] == _GZIP_MAGIC:
            return result
        return data  # Decryption didn't produce gzip — data wasn't encrypted
    except Exception:
        return data  # Fallback for unencrypted data


def encrypt_text(conn, text: str, config: dict) -> str:
    """Encrypt a text string (e.g., chunk_text in embeddings).

    Returns hex-encoded encrypted bytes prefixed with 'enc:' marker.
    If encryption is disabled, returns text unchanged.
    """
    if not config.get("encryption_enabled"):
        return text
    key = _get_key_bytes(config)
    raw = text.encode("utf-8")
    rows = conn.run(
        "SELECT encrypt(:data, :key, 'aes')",
        data=raw, key=key,
    )
    return "enc:" + rows[0][0].hex()


def decrypt_text(conn, text: str, config: dict) -> str:
    """Decrypt a text string encrypted by encrypt_text().

    If text doesn't have the 'enc:' prefix, returns it unchanged (unencrypted data).
    If encryption is disabled, strips enc: prefix if present and decrypts.
    """
    if not text.startswith("enc:"):
        return text  # Not encrypted
    key = _get_key_bytes(config)
    try:
        encrypted = bytes.fromhex(text[4:])
        rows = conn.run(
            "SELECT decrypt(:data, :key, 'aes')",
            data=encrypted, key=key,
        )
        return rows[0][0].decode("utf-8")
    except Exception:
        return text  # Can't decrypt — return as-is
