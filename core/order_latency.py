"""Client-side order acknowledgement and terminal observation timing."""
from collections import deque
from datetime import datetime, timezone
import time


class OrderLatencyRecorder:
    def __init__(self, max_records=1000, *, clock=time.monotonic):
        self.records = deque(maxlen=max_records)
        self.clock = clock

    def call(self, operation, function, *args, **kwargs):
        sent = self.clock()
        row = {"operation": operation,
               "sent_at": datetime.now(timezone.utc).isoformat()}
        try:
            result = function(*args, **kwargs)
            row["outcome"] = "ack"
            if isinstance(result, dict):
                row["order_status"] = result.get("status")
                row["exchange_timestamp_ms"] = result.get("timestamp")
                row["filled"] = result.get("filled")
                row["terminal_observed"] = result.get("status") in {"closed", "canceled", "expired", "rejected"}
            return result
        except Exception as exc:
            row["outcome"], row["error_category"] = "error", type(exc).__name__
            raise
        finally:
            row["received_at"] = datetime.now(timezone.utc).isoformat()
            row["duration_seconds"] = self.clock() - sent
            self.records.append(row)

    def summary(self):
        rows = list(self.records)
        groups = {}
        for row in rows:
            groups.setdefault(row["operation"], []).append(row)
        result = {}
        for operation, samples in groups.items():
            values = sorted(row["duration_seconds"] for row in samples)
            import numpy as np
            result[operation] = {"count": len(values),
                "errors": sum(row["outcome"] == "error" for row in samples),
                "p50_seconds": float(np.quantile(values, .5)),
                "p95_seconds": float(np.quantile(values, .95))}
        return {"scope": "client request to response; terminal observation is not exchange matching time",
                "statistics": result, "records": rows}


def record_order_call(owner, operation, function, *args, **kwargs):
    recorder = getattr(owner, "order_latency", None)
    return (recorder.call(operation, function, *args, **kwargs)
            if isinstance(recorder, OrderLatencyRecorder) else function(*args, **kwargs))
