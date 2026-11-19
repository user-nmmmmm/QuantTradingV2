"""B2 (foundation cluster): modules live in core/foundation/; core/ has no flat modules left."""
from __future__ import annotations

from pathlib import Path

from core.events import OrderIntent
from core.events.codec import _qualified_name, _resolve_type
from core.events.types import PAYLOAD_EVENT_TYPES
from core.foundation.domain import (FillRecord, OrderErrorCode, OrderStatus, OrderSubmissionResult,
                                    SyncResult)
from scripts import dev

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('alerting', 'clock', 'domain', 'live_safety', 'logger', 'redaction', 'reproducibility', 'runtime', 'runtime_identity', 'source_identity')


def test_modules_live_in_core_foundation_and_not_at_the_core_root():
    for name in MODULES:
        assert (ROOT / "core" / "foundation" / f"{name}.py").is_file(), name
        assert not (ROOT / "core" / f"{name}.py").exists(), f"core/{name}.py should have moved"


def test_core_root_has_no_flat_modules_left():
    assert sorted(p.name for p in (ROOT / "core").glob("*.py")) == ["__init__.py"]


def test_typed_gate_targets_exist():
    for relative in dev.TYPED_FILES:
        assert (ROOT / relative).is_file(), relative
    # CI calls the shared `types` task; test_dev_tasks verifies that workflow wiring and order.
    command, = dev.TASKS["types"].build(())
    assert command == [dev.PY, "-m", "mypy", *dev.TYPED_FILES]


def test_persisted_domain_type_names_did_not_follow_the_module_move():
    # ADR 0005: stored events carry these names, so moving core.domain must not change them.
    domain_types = {FillRecord, OrderSubmissionResult, SyncResult, OrderStatus, OrderErrorCode, OrderIntent}
    domain_types |= {cls for cls in PAYLOAD_EVENT_TYPES if cls.__module__ == "core.foundation.domain"}
    assert OrderIntent in domain_types
    for cls in domain_types:
        name = f"core.domain:{cls.__name__}"
        assert _qualified_name(cls) == name
        assert _resolve_type(name) is cls
