"""The code-only runner rejects fitting before any original body is executed."""

from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from scripts import check_ml_code
from scripts.ml_training_guard import NoTrainingGuard, TRAINING_DISABLED


def test_imported_alias_remains_blocked_after_caught_exception_and_restores():
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return "original body"

    alias = original
    old_code = original.__code__
    guard = NoTrainingGuard(include_optional_backends=False)
    guard.block_function(original, "test-only fake trainer")
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match=TRAINING_DISABLED):
                alias("would fit")
        assert calls == []
    finally:
        guard.restore()
    assert original.__code__ is old_code
    assert alias("after restore") == "original body"


def test_decorated_training_api_closure_is_blocked_before_wrapper_body():
    calls = []

    def factory(label):
        def decorated(*args, **kwargs):
            calls.append(label)
            return label
        return decorated

    decorated = factory("not executed")
    guard = NoTrainingGuard(include_optional_backends=False)
    guard.block_function(decorated, "decorated fake trainer")
    try:
        with pytest.raises(RuntimeError, match=TRAINING_DISABLED):
            decorated()
        assert calls == []
    finally:
        guard.restore()
    assert decorated() == "not executed"


def test_repository_training_aliases_are_blocked_and_mock_trainer_is_allowed(monkeypatch):
    from research.ml_selection import models

    alias = models.fit_model
    original_code = alias.__code__
    with NoTrainingGuard(include_optional_backends=False):
        for function in (alias, models.FeatureScaler.fit, models.BernoulliPolicy.fit_scaler,
                         models.BernoulliPolicy.update):
            with pytest.raises(RuntimeError, match=TRAINING_DISABLED):
                function()  # No original body executes, even with invalid arguments.
        trainer = Mock(return_value={"mocked": True})
        monkeypatch.setattr(models, "fit_model", trainer)
        assert models.fit_model("mock inputs") == {"mocked": True}
        trainer.assert_called_once_with("mock inputs")
        with pytest.raises(RuntimeError, match=TRAINING_DISABLED):
            alias()
    assert alias.__code__ is original_code


def test_frozen_coefficients_predict_normally_with_training_guard_installed():
    from research.ml_selection.models import FeatureScaler, RidgeModel

    scaler = FeatureScaler(("x",), np.array([0.]), np.array([1.]), np.array([2.]))
    model = RidgeModel(scaler, [3.], .5, {"source": "fixed_test_coefficients"})
    with NoTrainingGuard(include_optional_backends=False):
        np.testing.assert_allclose(model.predict(pd.DataFrame({"x": [1., 3.]})), [.5, 3.5])


def test_optional_backend_aliases_are_blocked_without_executing_fit():
    lightgbm = pytest.importorskip("lightgbm")
    pytest.importorskip("sklearn")
    from sklearn.linear_model import LinearRegression

    train_alias = lightgbm.train
    fit_alias = LinearRegression.fit
    with NoTrainingGuard():
        for function in (train_alias, lightgbm.cv, lightgbm.Booster.update, fit_alias):
            with pytest.raises(RuntimeError, match=TRAINING_DISABLED):
                function()


def test_default_manifest_is_explicit_and_excludes_mixed_training_modules(capsys):
    assert len(check_ml_code.DEFAULT_TESTS) == len(set(check_ml_code.DEFAULT_TESTS))
    assert not any("*" in target for target in check_ml_code.DEFAULT_TESTS)
    assert not set(check_ml_code.DEFAULT_TESTS) & {
        "tests/test_ml_selection_models.py", "tests/test_ml_selection_next_supervised.py",
        "tests/test_ml_selection_next_rl.py", "tests/test_ml_selection_pipeline.py",
    }
    assert check_ml_code.main(["--list-tests"]) == 0
    assert capsys.readouterr().out.splitlines() == list(check_ml_code.DEFAULT_TESTS)


def test_runner_uses_guarded_pytest_with_explicit_target_and_restores_cwd(monkeypatch, tmp_path):
    import pytest as pytest_module

    called = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pytest_module, "main", lambda args, *, plugins:
                        called.append((args, plugins)) or 0)
    node = "tests/test_ml_code_training_guard.py::test_default_manifest_is_explicit_and_excludes_mixed_training_modules"
    assert check_ml_code.main([node, "--pytest-arg=-q"]) == 0
    assert Path.cwd() == tmp_path
    assert called[0][0] == ["-p", "no:cacheprovider", node, "-q"]
    assert isinstance(called[0][1][0], NoTrainingGuard)


@pytest.mark.parametrize("target", ["../elsewhere.py", "research/ml_selection/models.py",
                                         "tests/missing.py"])
def test_runner_rejects_missing_or_non_test_files(target):
    with pytest.raises(SystemExit) as error:
        check_ml_code.main([target])
    assert error.value.code == 2
