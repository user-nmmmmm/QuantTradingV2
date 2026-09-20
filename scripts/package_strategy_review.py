"""Package only this task's edits relative to its pre-edit source snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    batch = parser.parse_args().batch.resolve()
    baseline = json.loads((batch / "baseline_manifest.json").read_text(encoding="utf-8"))
    paths = set(baseline["source_hashes"])
    for directory in ("core", "backtest", "strategies", "router", "composition", "config", "analysis", "scripts", "live_trading", "research", "tests", "docs", ".github"):
        paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT / directory).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    changes, lines = [], []
    for relative in sorted(paths):
        old, new = batch / "baseline_source" / relative, ROOT / relative
        before, after = sha(old) if old.exists() else None, sha(new) if new.exists() else None
        if before == after:
            continue
        changes.append(dict(path=relative, before_sha256=before, after_sha256=after))
        old_text = old.read_text(encoding="utf-8").splitlines(keepends=True) if old.exists() else []
        new_text = new.read_text(encoding="utf-8").splitlines(keepends=True) if new.exists() else []
        lines.append(f"diff --git a/{relative} b/{relative}\n")
        if not old.exists():
            lines.append("new file mode 100644\n")
        if not new.exists():
            lines.append("deleted file mode 100644\n")
        lines.extend(difflib.unified_diff(old_text, new_text, fromfile="a/" + relative if old.exists() else "/dev/null",
                                        tofile="b/" + relative if new.exists() else "/dev/null"))
    patch = batch / "repair.patch"
    patch.write_text("".join(lines), encoding="utf-8", newline="\n")
    result = dict(created_at=datetime.now(timezone.utc).isoformat(), base="baseline_source including pre-existing P2/P3 edits",
                  patch_sha256=sha(patch), changes=changes,
                  baseline_evidence_preserved=True, formal_config_unchanged=sha(ROOT / "config/params.yaml") == baseline["source_hashes"]["config/params.yaml"])
    (batch / "patch_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Packaged {len(changes)} changed/new files; formal_config_unchanged={result['formal_config_unchanged']}", flush=True)


if __name__ == "__main__":
    main()
