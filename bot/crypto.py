"""Authenticated encryption on stdlib (HMAC-SHA256 keystream + HMAC tag).
Used only to keep the refreshed Threads token in the state branch unreadable."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os


def _keys(secret: str) -> tuple[bytes, bytes]:
    base = hashlib.sha256(("koloda-state-v1|" + secret).encode()).digest()
    return (hmac.new(base, b"enc", hashlib.sha256).digest(),
            hmac.new(base, b"mac", hashlib.sha256).digest())


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def encrypt(plain: str, secret: str) -> str:
    ek, mk = _keys(secret)
    nonce = os.urandom(16)
    data = plain.encode()
    ct = bytes(a ^ b for a, b in zip(data, _stream(ek, nonce, len(data))))
    tag = hmac.new(mk, nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + ct).decode()


def decrypt(blob: str, secret: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(blob.encode())
    except Exception:
        return None
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    ek, mk = _keys(secret)
    if not hmac.compare_digest(tag, hmac.new(mk, nonce + ct, hashlib.sha256).digest()):
        return None
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct)))).decode()


def fingerprint(value: str) -> str:
    return hashlib.sha256(("fp|" + value).encode()).hexdigest()[:16]
