"""Password hashing: salted scrypt from the standard library, nothing to install.

Stored as ``scrypt$n$r$p$salt$hash`` (hex), so the cost can be raised later and
old hashes still verify. Comparison is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

N, R, P = 2 ** 14, 8, 1
MIN_LENGTH = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                   dklen=len(bytes.fromhex(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)
