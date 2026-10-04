"""Targeted minute imports retain actual availability, gaps and immutable inputs."""
import json

import pandas as pd
import pytest

from scripts.fetch_spot_refinement import download_refinement, refinement_days, MINUTE_MS, sha256


def labels_file(tmp_path, rows=None):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"outcomes": rows or [
        {"candidate_id": "a", "symbol": "BTC/USDT", "exit_bar": "2020-01-01T00:00:00Z", "ambiguous": True},
        {"candidate_id": "b", "symbol": "BTC/USDT", "exit_bar": "2020-01-01T01:00:00Z", "ambiguous": True},
        {"candidate_id": "resolved", "symbol": "ETH/USDT", "exit_bar": "2020-02-01T00:00:00Z", "ambiguous": False},
    ]}), encoding="utf-8")
    return path


class Client:
    status = 200
    retry_after = None

    def __init__(self, fail_at=None):
        self.calls, self.fail_at = [], fail_at

    def fetch(self, params, timeout_seconds):
        self.calls.append(dict(params))
        if len(self.calls) == self.fail_at:
            raise TimeoutError("sensitive transport details must not be persisted")
        count = min(1000, (params["endTime"] + 1 - params["startTime"]) // MINUTE_MS)
        return [[at, "100", "101", "99", "100", "2", at + MINUTE_MS - 1, "200", 10, "1", "100", "0"]
                for at in range(params["startTime"], params["startTime"] + count * MINUTE_MS, MINUTE_MS)]


def test_day_selection_deduplicates_candidates_and_ignores_resolved(tmp_path):
    path = labels_file(tmp_path)
    targets = refinement_days(json.loads(path.read_text()))
    assert len(targets) == 1
    assert targets[0]["candidate_ids"] == ["a", "b"]
    assert targets[0]["utc_day"] == "2020-01-01T00:00:00+00:00"
    assert targets[0]["end_exclusive_ms"] - targets[0]["start_ms"] == 1440 * MINUTE_MS


@pytest.mark.parametrize("change,pattern", [
    ({"exit_bar": "2020-01-01"}, "aware"),
    ({"exit_bar": "2099-01-01T00:00:00Z"}, "completed historical"),
    ({"symbol": "DOGE/USDT"}, "BTC/ETH"),
    ({"candidate_id": None}, "identity"),
])
def test_unregistered_or_unbounded_requests_fail_closed(tmp_path, change, pattern):
    row = {"candidate_id": "a", "symbol": "BTC/USDT", "exit_bar": "2020-01-01T00:00:00Z", "ambiguous": True}
    row.update(change)
    with pytest.raises(ValueError, match=pattern):
        refinement_days({"outcomes": [row]})


def test_1440_minutes_hashes_actual_availability_and_zero_network_resume(tmp_path):
    client, output = Client(), tmp_path / "download"
    path = labels_file(tmp_path)
    result = download_refinement(path, output, client=client)
    assert result["status"] == "completed" and result["request_count"] == 2
    ready = json.loads((output / "ready_manifest.json").read_text())
    assert ready["timeframe"] == "1m" and ready["retrospective_only"] is True
    assert ready["historical_pit_available_at"] is None
    descriptor = ready["symbols"]["BTC/USDT"]
    csv_path = output / descriptor["file"]
    assert sha256(csv_path.read_bytes()) == descriptor["sha256"]
    frame = pd.read_csv(csv_path)
    assert len(frame) == 1440 and frame.is_complete_bar.all()
    assert pd.to_datetime(frame.available_at, utc=True).min() > pd.Timestamp("2020-01-02T00:00:00Z")
    assert frame.historical_pit_available_at.isna().all()
    originals = {p: p.read_bytes() for p in output.rglob("*.json") if p.name != "checkpoint.json"}
    resumed = download_refinement(path, output, client=client, resume=True)
    assert resumed["request_count"] == 0 and resumed["resumed_pages"] == 2
    assert len(client.calls) == 2 and all(p.read_bytes() == content for p, content in originals.items())


def test_interrupted_import_only_fetches_missing_pages(tmp_path):
    path, output = labels_file(tmp_path), tmp_path / "download"
    failed = download_refinement(path, output, client=Client(fail_at=2))
    assert failed["status"] == "incomplete" and not (output / "ready_manifest.json").exists()
    assert failed["error"]["exception_chain"] == ["TimeoutError"] and "sensitive" not in json.dumps(failed)
    client = Client()
    result = download_refinement(path, output, client=client, resume=True)
    assert result["status"] == "completed" and result["resumed_pages"] == 1 and len(client.calls) == 1
    assert client.calls[0]["startTime"] % (1440 * MINUTE_MS) == 1000 * MINUTE_MS


def test_missing_and_partial_minutes_are_preserved_without_fabrication(tmp_path):
    class GapClient(Client):
        def fetch(self, params, timeout_seconds):
            rows = super().fetch(params, timeout_seconds)
            if len(self.calls) == 1:
                rows.pop(5)
                rows[10][6] -= 1000
            return rows
    output = tmp_path / "download"
    result = download_refinement(labels_file(tmp_path), output, client=GapClient())
    assert result["status"] == "incomplete_coverage"
    assert result["days"][0]["missing_minutes"] == ["2020-01-01T00:05:00+00:00"]
    assert result["days"][0]["incomplete_minutes"] == ["2020-01-01T00:11:00+00:00"]
    frame = pd.read_csv(output / result["symbols"]["BTC/USDT"]["file"])
    assert len(frame) == 1439 and (~frame.is_complete_bar).sum() == 1


def test_changed_input_or_tampered_pages_fail_closed(tmp_path):
    path, output = labels_file(tmp_path), tmp_path / "download"
    download_refinement(path, output, client=Client())
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(ValueError, match="unchanged"):
        download_refinement(path, output, client=Client(), resume=True)
    path.write_bytes(original)
    raw = next(p for p in (output / "raw").rglob("*.json") if ".metadata." not in p.name)
    raw.write_text("[]", encoding="utf-8")
    result = download_refinement(path, output, client=Client(), resume=True)
    assert result["status"] == "incomplete" and result["request_count"] == 0


def test_429_wait_is_bounded_and_403_never_retries(tmp_path, monkeypatch):
    from scripts import fetch_spot_refinement as module
    waits = []
    monkeypatch.setattr(module.time, "sleep", waits.append)
    class Limited(Client):
        def fetch(self, params, timeout_seconds):
            if not self.calls:
                self.calls.append(dict(params))
                self.status, self.retry_after = 429, "2"
                raise RuntimeError("limited")
            self.status = 200
            return super().fetch(params, timeout_seconds)
    path = labels_file(tmp_path)
    result = download_refinement(path, tmp_path / "limited", client=Limited())
    assert result["status"] == "completed" and waits == [2.]
    class Forbidden(Client):
        def fetch(self, params, timeout_seconds):
            self.calls.append(dict(params))
            self.status = 403
            raise RuntimeError("refused")
    denied = Forbidden()
    result = download_refinement(path, tmp_path / "denied", client=denied)
    assert result["status"] == "incomplete" and result["http_status"] == 403 and len(denied.calls) == 1
