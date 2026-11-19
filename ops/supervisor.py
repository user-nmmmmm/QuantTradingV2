"""Small cross-platform process supervisor with bounded restart backoff."""

from __future__ import annotations

import argparse
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class RestartPolicy:
    max_restarts: int = 10
    base_delay: float = 1.0
    max_delay: float = 60.0
    stable_seconds: float = 300.0


def supervise(
    command: Sequence[str],
    policy: RestartPolicy = RestartPolicy(),
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    if not command:
        raise ValueError("supervised command cannot be empty")
    restarts = 0
    while True:
        started = monotonic()
        result = runner(list(command), check=False)
        if result.returncode == 0:
            return 0
        if monotonic() - started >= policy.stable_seconds:
            restarts = 0
        restarts += 1
        if restarts > policy.max_restarts:
            return int(result.returncode or 1)
        sleep(min(policy.base_delay * (2 ** (restarts - 1)), policy.max_delay))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-restarts", type=int, default=10)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    return supervise(args.command, RestartPolicy(max_restarts=args.max_restarts))


if __name__ == "__main__":
    raise SystemExit(main())
