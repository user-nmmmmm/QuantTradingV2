"""Predeclared recovery parameters; never rebase the account high-water mark."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class DrawdownRecoveryPolicy:
    enabled: bool = False
    cooldown_days: float = 30.0
    probation_risk_multiplier: float = 0.25
    probation_loss_limit: float = 0.03

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("recovery.enabled must be boolean")
        if not math.isfinite(self.cooldown_days) or self.cooldown_days <= 0:
            raise ValueError("recovery.cooldown_days must be finite and positive")
        for name in ("probation_risk_multiplier", "probation_loss_limit"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"recovery.{name} must be in (0, 1]")
