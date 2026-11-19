"""Prevent model fitting during the explicitly scoped code verification suite.

The guard changes function bodies in memory and restores them on exit. Changing
the original function object also protects aliases imported before installation.
Mocks, frozen model loading, prediction and pure data calculations remain usable.
This is a test-process guard, not a sandbox for arbitrary child processes.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
import importlib.util
from types import CodeType, FunctionType
from typing import Any


TRAINING_DISABLED = "ML training is disabled during code verification"


def _blocked_code(label: str, freevars: int) -> CodeType:
    """Build an always-raising body with a decorator's existing closure size."""
    cells = [f"cell_{index}" for index in range(freevars)]
    lines = ["def factory():"]
    lines.extend(f"    {name} = None" for name in cells)
    lines.append("    def blocked(*args, **kwargs):")
    if cells:
        # `args` is always a tuple; this branch exists only to retain freevars.
        lines.extend(["        if args is None:", "            _ = (" + ", ".join(cells) + ",)"])
    lines.append(f"        raise RuntimeError({(TRAINING_DISABLED + ': ' + label)!r})")
    lines.append("    return blocked")
    namespace: dict[str, Any] = {}
    exec(compile("\n".join(lines), "<ml-code-training-guard>", "exec"), namespace)
    return namespace["factory"]().__code__


class NoTrainingGuard(AbstractContextManager):
    """Block repository trainers and installed optional CPU training backends."""

    def __init__(self, *, include_optional_backends: bool = True):
        self.include_optional_backends = include_optional_backends
        self._originals: dict[FunctionType, CodeType] = {}
        self.labels: list[str] = []
        self.installed = False

    def block_function(self, value: Any, label: str) -> None:
        function = getattr(value, "__func__", value)
        if not isinstance(function, FunctionType):
            raise TypeError(f"Cannot safely guard the training API {label}")
        if function in self._originals:
            return
        original = function.__code__
        function.__code__ = _blocked_code(label, len(original.co_freevars))
        self._originals[function] = original
        self.labels.append(label)

    def install(self) -> "NoTrainingGuard":
        if self.installed:
            return self
        from research.ml_selection import models

        try:
            for label, function in (
                ("models.fit_model", models.fit_model),
                ("FeatureScaler.fit", models.FeatureScaler.fit),
                ("BernoulliPolicy.fit_scaler", models.BernoulliPolicy.fit_scaler),
                ("BernoulliPolicy.update", models.BernoulliPolicy.update),
            ):
                self.block_function(function, label)
            if self.include_optional_backends:
                self._guard_optional_backends()
        except BaseException:
            self.restore()
            raise
        self.installed = True
        return self

    def _guard_optional_backends(self) -> None:
        if importlib.util.find_spec("lightgbm") is not None:
            import lightgbm

            for label, function in (
                ("lightgbm.train", lightgbm.train),
                ("lightgbm.cv", lightgbm.cv),
                ("lightgbm.Booster.update", lightgbm.Booster.update),
                ("lightgbm.Booster.refit", lightgbm.Booster.refit),
                ("lightgbm.LGBMModel.fit", lightgbm.LGBMModel.fit),
                ("lightgbm.LGBMClassifier.fit", lightgbm.LGBMClassifier.fit),
                ("lightgbm.LGBMRegressor.fit", lightgbm.LGBMRegressor.fit),
                ("lightgbm.LGBMRanker.fit", lightgbm.LGBMRanker.fit),
            ):
                self.block_function(function, label)
        if importlib.util.find_spec("sklearn") is not None:
            from sklearn.utils import all_estimators

            for name, estimator in all_estimators():
                for method in ("fit", "partial_fit", "fit_transform", "fit_predict"):
                    value = getattr(estimator, method, None)
                    if value is None or isinstance(value, property):
                        continue
                    self.block_function(value, f"sklearn.{name}.{method}")

    def restore(self) -> None:
        for function, original in reversed(tuple(self._originals.items())):
            function.__code__ = original
        self._originals.clear()
        self.labels.clear()
        self.installed = False

    def __enter__(self) -> "NoTrainingGuard":
        return self.install()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.restore()

    # pytest configures plugins before collecting test modules or fixtures.
    def pytest_configure(self, config) -> None:
        self.install()

    def pytest_report_header(self, config) -> str:
        return f"No-training guard: {len(self.labels)} fitting/update APIs blocked"

    def pytest_unconfigure(self, config) -> None:
        self.restore()
