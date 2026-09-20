"""Freeze the repaired implementation without replacing baseline evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from analysis.strategy_review import freeze_prospective
    from scripts.run_strategy_review import DEFAULT_BATCH, digest, save
    batch = DEFAULT_BATCH
    baseline = json.loads((batch / "baseline_manifest.json").read_text(encoding="utf-8"))
    destination = batch / "revised_source"
    if destination.exists() or (batch / "revised_manifest.json").exists():
        raise ValueError("Revised freeze already exists; never replace research identity")
    files = {ROOT / rel for rel in baseline["source_hashes"]}
    for folder in ("core", "backtest", "strategies", "router", "composition", "config", "analysis", "scripts", "live_trading", "research", "tests", "docs", ".github"):
        files.update(p for p in (ROOT / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    hashes = {}
    for path in sorted(files):
        relative = path.relative_to(ROOT).as_posix()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    if hashes["config/params.yaml"] != baseline["source_hashes"]["config/params.yaml"]:
        raise ValueError("Formal strategy configuration changed")
    frozen_at = datetime.now(timezone.utc).isoformat()
    manifest = dict(schema="strategy_review_revised/v1", frozen_at=frozen_at,
                    source_hashes=hashes, input_hashes=baseline["input_hashes"],
                    source_sha256=digest(hashes), admission="paused_revalidation")
    save(batch / "revised_manifest.json", manifest)
    freeze_prospective(batch / "prospective_protocol.json", code_hash=manifest["source_sha256"],
                       config_hash=hashes["config/params.yaml"],
                       registry_hash=hashlib.sha256((batch / "review_protocol.json").read_bytes()).hexdigest(),
                       frozen_at=frozen_at)
    print(f"Frozen {len(hashes)} files: {manifest['source_sha256']}", flush=True)


if __name__ == "__main__":
    main()
