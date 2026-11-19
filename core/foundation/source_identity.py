"""Behavioral source identity for a declared "frozen scope".

Legacy identities hash raw file bytes, so a comment or a reformat changes them.
This module provides versioned normalized source identities, ``code-token/v1``
and ``code-token/v2`` (the default for new records):

* Python files are reduced to their token stream without comments and without
  layout-only tokens (blank lines, line wrapping inside brackets, indentation
  width, spacing). V2 normalizes f-string expressions through their parsed
  structure while preserving literal values, debug text, conversions and format
  specifications. V1 retains the original f-string spelling for old records.
* Other files in scope are hashed as bytes (text types with LF normalization).

Each file also carries a ``body`` hash: the same token stream with import statements
removed (non-Python files: the normalized bytes). It never takes part in the identity
digest; it only lets a report recognize a file that was moved or whose only change is
the import path of something that moved. These are syntactic classifications;
they do not prove equivalent import targets, side effects or runtime behavior.

Nothing here replaces or relaxes an existing verifier: old frozen records keep
being checked with the scheme they were created with. This module only makes
the impact of a change on the frozen scope visible and comparable.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import subprocess
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

LEGACY_SCHEME = "code-token/v1"
SCHEME = "code-token/v2"
SUPPORTED_SCHEMES = (LEGACY_SCHEME, SCHEME)
CRLF, LF = bytes([13, 10]), bytes([10])
TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".txt"}
_SKIPPED = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING}
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_scheme(scheme: str) -> None:
    if scheme not in SUPPORTED_SCHEMES:
        raise ValueError(f"unsupported identity scheme: {scheme}")


def _ast_structure(value):
    """Stable field serialization, independent of ast.dump's version-specific omissions."""
    if isinstance(value, ast.AST):
        return [type(value).__name__, [[name, _ast_structure(item)] for name, item in ast.iter_fields(value)]]
    if isinstance(value, list):
        return [_ast_structure(item) for item in value]
    return [type(value).__name__, repr(value)]


def _string_item(text: str, scheme: str) -> str:
    prefix = text[:next(i for i, char in enumerate(text) if char in "\"'")]
    if scheme == SCHEME and "f" in prefix.lower():
        # Debug '=' text is a Constant in JoinedStr, so its visible spaces survive.
        tree = ast.parse(text, mode="eval")
        return "FSTRING " + json.dumps(_ast_structure(tree), ensure_ascii=True, separators=(",", ":"))
    return "STRING " + text


def _python_tokens(source: str, scheme: str = SCHEME) -> List[str]:
    """Canonical token items of ``source``; raises ``tokenize.TokenError``/``SyntaxError`` if unreadable."""
    _check_scheme(scheme)
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
                out.append(_string_item(text, scheme))
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
        elif kind == tokenize.STRING:
            out.append(_string_item(token.string, scheme))
        else:
            out.append(f"{tokenize.tok_name[kind]} {token.string}")
    return out


def normalize_python(source: str, *, scheme: str = SCHEME) -> str:
    """Canonical token text of ``source``; raises ``tokenize.TokenError``/``SyntaxError`` if unreadable."""
    return "\x1f".join(_python_tokens(source, scheme))


_STATEMENT_START = {"\n", "INDENT", "DEDENT", "OP ;"}


def python_body(source: str, *, scheme: str = SCHEME) -> str:
    """Canonical token text with every ``import`` / ``from ... import`` statement removed.

    Statements are recognized at a statement boundary only, so ``yield from`` and
    ``raise ... from`` are untouched. Imports can execute code and bind different
    values: this hash is only a classification aid, never an equivalence proof."""
    out: List[str] = []
    at_start, skipping = True, False
    for item in _python_tokens(source, scheme):
        if skipping:
            if item in ("\n", "OP ;"):
                skipping, at_start = False, True
            continue
        if at_start and item in ("NAME import", "NAME from"):
            skipping = True
            continue
        out.append(item)
        at_start = item in _STATEMENT_START
    return "\x1f".join(out)


@dataclass(frozen=True)
class FileIdentity:
    path: str
    raw: str      # sha256 of the stored bytes
    norm: str     # sha256 of the normalized form (equals ``raw``-derived hash for non-Python files)
    scheme: str
    body: str = ""   # sha256 of the form without import statements; "" when unknown or empty


def identify(path: str, data: bytes, *, scheme: str = SCHEME) -> FileIdentity:
    _check_scheme(scheme)
    raw = _sha(data)
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        try:
            source = data.decode("utf-8-sig")
            text, body = normalize_python(source, scheme=scheme), python_body(source, scheme=scheme)
            return FileIdentity(path, raw, _sha(text.encode("utf-8")), scheme,
                                _sha(body.encode("utf-8")) if body else "")
        except (tokenize.TokenError, SyntaxError, UnicodeDecodeError, IndentationError, RecursionError):
            return FileIdentity(path, raw, raw, "bytes/v1")  # unreadable: be strict
    if suffix in TEXT_SUFFIXES:
        norm = _sha(data.replace(CRLF, LF))
        return FileIdentity(path, raw, norm, "bytes-lf/v1", norm)
    return FileIdentity(path, raw, raw, "bytes/v1", raw)


@dataclass
class Scope:
    """Declarative list of what counts as behavior of the frozen candidates."""
    prefixes: Dict[str, List[str]] = field(default_factory=dict)   # directory -> allowed suffixes
    files: List[str] = field(default_factory=list)                 # exact paths

    @classmethod
    def load(cls, path: Path | str) -> "Scope":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def from_json(cls, text: str) -> "Scope":
        raw = json.loads(text)
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


def build_manifest(root: Path | str, scope: Scope, ref: Optional[str] = None, *,
                   scheme: str = SCHEME) -> Dict[str, FileIdentity]:
    _check_scheme(scheme)
    root = Path(root)
    in_scope = [p for p in list_paths_at(root, ref) if scope.contains(p)]
    read = reader_for(root, ref, in_scope)
    manifest: Dict[str, FileIdentity] = {}
    for path in in_scope:
        data = read(path)
        if data is not None:
            manifest[path] = identify(path, data, scheme=scheme)
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
    moved: List[Tuple[str, str]] = field(default_factory=list)   # (old path, new path), same body
    import_only: List[str] = field(default_factory=list)  # same path, only import statements differ

    @property
    def changes_identity(self) -> bool:
        """Strict: any difference in the identity digest, including moves and import edits."""
        return bool(self.behavioral or self.added or self.removed or self.moved or self.import_only)

    @property
    def changes_behavior(self) -> bool:
        """Conservative compatibility flag: identity changes require behavioral review.

        True means potentially changed, not proven different. Imports and moves
        can change execution and are never exempted by a matching body hash.
        """
        return self.changes_identity

    @property
    def behavior_status(self) -> str:
        return "not_verified" if self.changes_identity else "unchanged"


def _pair_moves(base: Mapping[str, FileIdentity], head: Mapping[str, FileIdentity],
                removed: List[str], added: List[str]) -> Tuple[List[Tuple[str, str]], List[str], List[str]]:
    """Match removed and added paths that hold the same body: same file name first, then path order.
    Files with an empty or unknown body (e.g. an empty ``__init__.py``) are never paired."""
    old_by_body: Dict[str, List[str]] = {}
    new_by_body: Dict[str, List[str]] = {}
    for path in removed:
        if base[path].body:
            old_by_body.setdefault(base[path].body, []).append(path)
    for path in added:
        if head[path].body:
            new_by_body.setdefault(head[path].body, []).append(path)
    moved: List[Tuple[str, str]] = []
    for body, olds in old_by_body.items():
        news = list(new_by_body.get(body, []))
        olds = list(olds)
        for old in list(olds):
            twin = next((n for n in news if Path(n).name == Path(old).name), None)
            if twin:
                moved.append((old, twin))
                olds.remove(old)
                news.remove(twin)
        moved.extend(zip(olds, news))
    gone_old = {old for old, _ in moved}
    gone_new = {new for _, new in moved}
    return (sorted(moved), [p for p in removed if p not in gone_old], [p for p in added if p not in gone_new])


def compare(base: Mapping[str, FileIdentity], head: Mapping[str, FileIdentity]) -> Impact:
    impact = Impact()
    removed: List[str] = []
    added: List[str] = []
    for path in sorted(set(base) | set(head)):
        before, after = base.get(path), head.get(path)
        if before is None:
            added.append(path)
        elif after is None:
            removed.append(path)
        elif before.norm != after.norm or before.scheme != after.scheme:
            if before.scheme == after.scheme and before.body and before.body == after.body:
                impact.import_only.append(path)
            else:
                impact.behavioral.append(path)
        elif before.raw != after.raw:
            impact.cosmetic.append(path)
    impact.moved, impact.removed, impact.added = _pair_moves(base, head, removed, added)
    return impact


def _check_manifest_scheme(manifest: Mapping[str, FileIdentity], scheme: str) -> None:
    _check_scheme(scheme)
    if any(i.scheme in SUPPORTED_SCHEMES and i.scheme != scheme for i in manifest.values()):
        raise ValueError(f"manifest must be built with the recorded scheme {scheme}")


def receipt(manifest: Mapping[str, FileIdentity], *, ref_sha: Optional[str], scheme: str = SCHEME) -> dict:
    _check_manifest_scheme(manifest, scheme)
    return {"schema": "frozen-scope-identity/v1", "scheme": scheme, "git_sha": ref_sha,
            "identity_sha256": identity_digest(manifest),
            "files": {p: {"norm": i.norm, "scheme": i.scheme, **({"body": i.body} if i.body else {})}
                      for p, i in sorted(manifest.items())}}


def verify_receipt(saved: Mapping, manifest: Mapping[str, FileIdentity]) -> Impact:
    """Compare the current scope with a saved receipt (see ``receipt``)."""
    if saved.get("schema") != "frozen-scope-identity/v1" or saved.get("scheme") not in SUPPORTED_SCHEMES:
        raise ValueError("unsupported identity receipt")
    previous = {p: FileIdentity(p, v["norm"], v["norm"], v["scheme"], v.get("body", ""))
                for p, v in saved["files"].items()}
    _check_manifest_scheme(previous, saved["scheme"])
    _check_manifest_scheme(manifest, saved["scheme"])
    # raw hashes are not stored in receipts, so cosmetic edits cannot be told apart here by design;
    # receipts written before ``body`` existed simply cannot recognize moves or import-only edits
    return compare(previous, {p: FileIdentity(p, i.norm, i.norm, i.scheme, i.body) for p, i in manifest.items()})


# ---- release-bound identity (infra A4, ADR 0010) -----------------------------------------

BINDING_SCHEMA = "release-bound-identity/v1"
SCOPE_FILE = "config/frozen_scope.json"


def scope_sha256(data: bytes) -> str:
    """Hash of the scope declaration with line endings normalized, so checkouts agree."""
    return _sha(data.replace(CRLF, LF))


def _tag_commit(root: Path, tag: str) -> str:
    """Commit an *annotated* tag points to; raises ``ValueError`` for missing or lightweight tags."""
    try:
        kind = _git(root, "cat-file", "-t", f"refs/tags/{tag}").decode().strip()
    except subprocess.CalledProcessError:
        raise ValueError(f"tag {tag} does not exist") from None
    if kind != "tag":
        raise ValueError(f"tag {tag} is lightweight; release tags must be annotated (git tag -a)")
    return _git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}").decode().strip()


def bind_release(root: Path | str, tag: str, *, scheme: str = SCHEME) -> dict:
    """Binding record: an annotated release tag, its commit, and the frozen-scope identity at that commit.

    The scope declaration is read from the tagged commit as well, so the record is a function of the tag alone."""
    root = Path(root)
    commit = _tag_commit(root, tag)
    scope_bytes = _git(root, "cat-file", "blob", f"{commit}:{SCOPE_FILE}")
    scope = Scope.from_json(scope_bytes.decode("utf-8"))
    manifest = build_manifest(root, scope, commit, scheme=scheme)
    return {"schema": BINDING_SCHEMA, "tag": tag, "tag_commit": commit,
            "scope_sha256": scope_sha256(scope_bytes), "receipt": receipt(manifest, ref_sha=commit, scheme=scheme)}


def verify_binding(saved: Mapping, root: Path | str, head: Mapping[str, FileIdentity],
                   current_scope_sha256: str) -> Tuple[List[str], Impact]:
    """Problems with the binding itself (tag moved or gone, scope declaration changed) plus the identity
    impact of ``head`` against the recorded receipt. ``head`` must be built with the *current* scope."""
    if saved.get("schema") != BINDING_SCHEMA:
        raise ValueError("unsupported identity binding")
    problems: List[str] = []
    try:
        commit = _tag_commit(Path(root), saved["tag"])
        if commit != saved["tag_commit"]:
            problems.append(f"tag {saved['tag']} now points to {commit[:12]}, "
                            f"the binding recorded {saved['tag_commit'][:12]}")
    except ValueError as error:
        problems.append(str(error))
    if current_scope_sha256 != saved["scope_sha256"]:
        problems.append("the frozen-scope declaration differs from the one recorded in the binding")
    return problems, verify_receipt(saved["receipt"], head)
