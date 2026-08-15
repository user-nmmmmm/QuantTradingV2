"""Validate exact runtime pins and the committed lock digest."""
import hashlib, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
lock = ROOT / "requirements.lock.txt"
expected, filename = (ROOT / "requirements.lock.sha256").read_text(encoding="ascii").split()
if filename != lock.name: raise SystemExit(f"unexpected checksum target: {filename}")
actual = hashlib.sha256(lock.read_bytes()).hexdigest()
if actual != expected: raise SystemExit(f"{lock.name} checksum mismatch: {actual}")
bad = [line for line in lock.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#") and not re.fullmatch(r"[A-Za-z0-9_.-]+==[^ ;]+(?:\s*;.*)?", line.strip())]
if bad: raise SystemExit(f"lock contains non-exact requirements: {bad[:3]}")
print(f"{lock.name}: exact pins and SHA-256 verified")
