"""CSV cache identities with explicit LF policy and legacy newline compatibility.

Raw artifact hashes elsewhere retain their byte-exact contracts.
"""
from __future__ import annotations

import hashlib
from typing import Mapping

LF_HASH_POLICY = "sha256-lf/v1"
CRLF, LF = b"\r\n", b"\n"


def sha256_lf_bytes(data: bytes) -> str:
    return hashlib.sha256(data.replace(CRLF, LF)).hexdigest()


def cache_hash_matches(data: bytes, record: Mapping[str, object]) -> bool:
    """Accept only newline-equivalent legacy CSVs; unknown declared policies fail."""
    expected, policy = record.get("sha256"), record.get("hash_policy")
    if not isinstance(expected, str):
        return False
    if policy == LF_HASH_POLICY:
        return expected == sha256_lf_bytes(data)
    if policy is not None:
        return False
    normalized = data.replace(CRLF, LF)
    return expected in {hashlib.sha256(value).hexdigest()
                        for value in (data, normalized, normalized.replace(LF, CRLF))}
