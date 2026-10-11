"""Behavioral source identity for a declared "frozen scope".

Legacy identities hash raw file bytes, so a comment or a reformat changes them.
This module adds a second, stricter-by-meaning scheme, ``code-token/v1``:

* Python files are reduced to their token stream without comments and without
  layout-only tokens (blank lines, line wrapping inside brackets, indentation
  width, spacing). Two files with the same stream parse to the same program, so
  any change that can alter behavior changes the hash, while comment-only and
  whitespace-only edits do not.
* Other files in scope are hashed as bytes (text types with LF normalization).

Nothing here replaces or relaxes an existing verifier: old frozen records keep
being checked with the scheme they were created with. This module only makes
the impact of a change on the frozen scope visible and comparable.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional

SCHEME = "code-token/v1"
CRLF, LF = bytes([13, 10]), bytes([10])
TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".txt"}
_SKIPPED = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING}
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_python(source: str) -> str:
    """Canonical token text of ``source``; raises ``tokenize.TokenError``/``SyntaxError`` if unreadable."""
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    # offset of each line start, to cut f-string spans out of the original text
    starts, total = [], 0
    for line in lines:
        starts.append(total)
        total += len(line) + 1

    def offset(position: tuple[int, int]) -> int:
        return starts[position[0] - 1] + position[1]

    out: List[str] = []
    depth, span_start = 0, None
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        kind = token.type
        if _FSTRING_START is not None and kind == _FSTRING_START:
            if depth == 0:
                span_start = token.start
            depth += 1
            continue
        if _FSTRING_END is not None and kind == _FSTRING_END:
            depth -= 1
            if depth == 0:
                # Python 3.12+ splits f-strings into several tokens; 3.11 yields one STRING.
                text = source[offset(span_start):offset(token.end)]
                out.append("STRING " + text)
            continue
        if depth:
            continue
        if kind in _SKIPPED or kind == tokenize.ENDMARKER:
            continue
        if kind == tokenize.NEWLINE:
            out.append("\n")
        elif kind == tokenize.INDENT:
            out.append("INDENT")
        elif kind == tokenize.DEDENT:
            out.append("DEDENT")
        else:
            out.append(f"{tokenize.tok_name[kind]} {token.string}")
    return "\x1f".join(out)


@dataclass(frozen=True)
class FileIdentity:
    path: str
    raw: str      # sha256 of the stored bytes
    norm: str     # sha256 of the normalized form (equals ``raw``-derived hash for non-Python files)
    scheme: str


def identify(path: str, data: bytes) -> FileIdentity:
    raw = _sha(data)
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        try:
            text = normalize_python(data.decode("utf-8-sig"))
            return FileIdentity(path, raw, _sha(text.encode("utf-8")), SCHEME)
        except (tokenize.TokenError, SyntaxError, UnicodeDecodeError, IndentationError):
            return FileIdentity(path, raw, raw, "bytes/v1")  # unreadable: be strict
    if suffix in TEXT_SUFFIXES:
        return FileIdentity(path, raw, _sha(data.replace(b"\r\n", b"\n")), "bytes-lf/v1")
    return FileIdentity(path, raw, raw, "bytes/v1")


@dataclass
class Scope:
    """Declarative list of what counts as behavior of the frozen candidates."""
    prefixes: Dict[str, List[str]] = field(default_factory=dict)   # directory -> allowed suffixes
    files: List[str] = field(default_factory=list)                 # exact paths

    @classmethod
    def load(cls, path: Path | str) -> "Scope":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls({item["prefix"]: [s.lower() for s in item["suffixes"]] for item in raw["directories"]},
                   list(raw["files"]))

    def contains(self, path: str) -> bool:
        if path in self.files:
            return True
        suffix = Path(path).suffix.lower()
        return any(path.startswith(prefix) and suffix in suffixes
                   for prefix, suffixes in self.prefixes.items())


Reader = Callable[[str], Optional[bytes]]


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def list_paths_at(root: Path, ref: Optional[str]) -> List[str]:
    """Tracked paths at ``ref``; for the working tree (``ref=None``) the files present on disk and tracked."""
    if ref is None:
        out = _git(root, "ls-files", "-z")
    else:
        out = _git(root, "ls-tree", "-r", "-z", "--name-only", ref)
    return sorted(p.decode("utf-8") for p in out.split(b"\0") if p)


def _text_unset(root: Path, paths: List[str]) -> set:
    """Paths whose ``text`` attribute is unset (``-text``): git stores them byte-for-byte."""
    if not paths:
        return set()
    nul = bytes([0])
    out = subprocess.run(["git", "check-attr", "-z", "--stdin", "text"], cwd=root, check=True,
                         capture_output=True, input=nul.join(p.encode("utf-8") for p in paths) + nul).stdout
    fields = out.split(nul)
    return {fields[i].decode("utf-8") for i in range(0, len(fields) - 2, 3) if fields[i + 2] == b"unset"}


def reader_for(root: Path, ref: Optional[str], paths: Optional[List[str]] = None) -> Reader:
    """Read stored content. Blobs are returned as stored. Working-tree files get git's own
    line-ending normalization (CRLF -> LF) unless their ``text`` attribute is unset, so a
    Windows checkout and the committed blob yield the same bytes."""
    if ref is None:
        keep_bytes = _text_unset(root, paths or [])

        def read_tree(path: str) -> Optional[bytes]:
            file = root / path
            if not file.is_file():
                return None
            data = file.read_bytes()
            return data if path in keep_bytes else data.replace(CRLF, LF)
        return read_tree

    def read_blob(path: str) -> Optional[bytes]:
        try:
            return _git(root, "cat-file", "blob", f"{ref}:{path}")
        except subprocess.CalledProcessError:
            return None
    return read_blob


def build_manifest(root: Path | str, scope: Scope, ref: Optional[str] = None) -> Dict[str, FileIdentity]:
    root = Path(root)
    in_scope = [p for p in list_paths_at(root, ref) if scope.contains(p)]
    read = reader_for(root, ref, in_scope)
    manifest: Dict[str, FileIdentity] = {}
    for path in in_scope:
        data = read(path)
        if data is not None:
            manifest[path] = identify(path, data)
    return manifest


def identity_digest(manifest: Mapping[str, FileIdentity]) -> str:
    """One hash over the whole scope using the normalized hashes."""
    body = json.dumps({p: [i.norm, i.scheme] for p, i in sorted(manifest.items())},
                      sort_keys=True, separators=(",", ":"))
    return _sha(body.encode("utf-8"))


@dataclass
class Impact:
    behavioral: List[str] = field(default_factory=list)   # normalized form changed
    cosmetic: List[str] = field(default_factory=list)     # bytes changed, behavior form identical
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)

    @property
    def changes_identity(self) -> bool:
        return bool(self.behavioral or self.added or self.removed)


def compare(base: Mapping[str, FileIdentity], head: Mapping[str, FileIdentity]) -> Impact:
    impact = Impact()
    for path in sorted(set(base) | set(head)):
        before, after = base.get(path), head.get(path)
        if before is None:
            impact.added.append(path)
        elif after is None:
            impact.removed.append(path)
        elif before.norm != after.norm:
            impact.behavioral.append(path)
        elif before.raw != after.raw:
            impact.cosmetic.append(path)
    return impact


def receipt(manifest: Mapping[str, FileIdentity], *, ref_sha: Optional[str]) -> dict:
    return {"schema": "frozen-scope-identity/v1", "scheme": SCHEME, "git_sha": ref_sha,
            "identity_sha256": identity_digest(manifest),
            "files": {p: {"norm": i.norm, "scheme": i.scheme} for p, i in sorted(manifest.items())}}


def verify_receipt(saved: Mapping, manifest: Mapping[str, FileIdentity]) -> Impact:
    """Compare the current scope with a saved receipt (see ``receipt``)."""
    if saved.get("schema") != "frozen-scope-identity/v1" or saved.get("scheme") != SCHEME:
        raise ValueError("unsupported identity receipt")
    previous = {p: FileIdentity(p, v["norm"], v["norm"], v["scheme"]) for p, v in saved["files"].items()}
    # raw hashes are not stored in receipts, so cosmetic edits cannot be told apart here by design
    return compare(previous, {p: FileIdentity(p, i.norm, i.norm, i.scheme) for p, i in manifest.items()})
