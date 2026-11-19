"""A registered plugin loader is scoped and cannot shadow standard-library code."""

from pathlib import Path
import sys

import pytest

from scripts.paper_research_worker import (
    _RegisteredProjectFinder,
    _dispatch_in_registered_source,
    _project_import_prefixes,
)


def test_stdlib_callable_is_explicitly_refused_without_loading_local_shadow(tmp_path):
    marker = tmp_path / "executed"
    (tmp_path / "json.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        "def not_real(): return {'unsafe':True}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="stdlib callable prefix is forbidden"):
        _dispatch_in_registered_source(
            {"kind": "python_callable", "callable": "json:not_real"}, tmp_path, {}
        )
    assert not marker.exists()


def test_only_registered_prefixes_can_be_resolved_from_frozen_directory(tmp_path):
    (tmp_path / "registered_fixture.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "json.py").write_text("value = 'shadow'\n", encoding="utf-8")
    (tmp_path / "unregistered.py").write_text("value = 2\n", encoding="utf-8")
    task = {"kind": "python_callable", "callable": "registered_fixture:run"}
    finder = _RegisteredProjectFinder(tmp_path, _project_import_prefixes(task))
    assert Path(finder.find_spec("registered_fixture").origin) == tmp_path / "registered_fixture.py"
    assert finder.find_spec("json") is None
    assert finder.find_spec("unregistered") is None
    assert finder.find_spec("registered_fixture...outside") is None


def test_registered_import_does_not_change_global_module_search_path(tmp_path):
    name = "registered_worker_fixture"
    (tmp_path / (name + ".py")).write_text("def run(): return {'value':7}\n", encoding="utf-8")
    before_path, before_finders = tuple(sys.path), tuple(sys.meta_path)
    try:
        result = _dispatch_in_registered_source(
            {"kind": "python_callable", "callable": name + ":run"}, tmp_path, {}
        )
        assert result == {"value": 7}
        assert tuple(sys.path) == before_path
        assert tuple(sys.meta_path) == before_finders
    finally:
        sys.modules.pop(name, None)
