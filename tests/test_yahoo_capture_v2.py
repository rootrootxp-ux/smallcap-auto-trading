"""Offline regression checks for the versioned Yahoo capture policy.

All responses and clocks are synthetic: these tests neither contact Yahoo nor
sleep.  Diagnostic probes must never become prospective price observations.
"""
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research.forward_capture_v2 import collector
from research.forward_capture_v2.policy import load_policy
from research.forward_capture_v2.rate_limit import RateController
from research.forward_study import collector as legacy
from research.forward_study.protocol import symbols as fixed_symbols
from research.forward_study.store import add_receipt, audit_store, select_primary


JST = ZoneInfo("Asia/Tokyo")
SYMBOLS = tuple(fixed_symbols())
SESSION = "2026-11-09"
NOW = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)
DEFAULT_DATES = object()


def stamp(day=SESSION, clock="09:00"):
    return int(datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=JST).timestamp())


def chart_body(symbol=SYMBOLS[0], day=SESSION, *, timestamps=None, quote=None):
    if timestamps is None:
        timestamps = [stamp(day), stamp(day, "09:05")]
    if quote is None:
        quote = {"open": [100, 101], "high": [102, 103], "low": [99, 100],
                 "close": [101, 102], "volume": [1000, 2000]}
    document = {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "exchangeTimezoneName": "Asia/Tokyo",
                 "dataGranularity": "5m", "instrumentType": "EQUITY"},
        "timestamp": timestamps, "indicators": {"quote": [quote]},
    }]}}
    return (" \n" + json.dumps(document) + "\n").encode()


def response(status=200, body=None, *, symbol=SYMBOLS[0], day=SESSION, headers=None):
    return legacy.FetchResponse(status, headers or {"Content-Type": "application/json"},
                                chart_body(symbol, day) if body is None else body,
                                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}")


class FakeClock:
    def __init__(self, start=NOW):
        self.start = start
        self.elapsed = 0.0
        self.sleeps = []

    def now(self, _fixed=None):
        return self.start + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("negative wait")
        self.sleeps.append(seconds)
        self.elapsed += seconds


class FakeFetcher:
    def __init__(self, clock, *replies):
        self.clock = clock
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((self.clock.monotonic(), url, dict(params)))
        if not self.replies:
            raise AssertionError("unexpected HTTP request")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(url, params) if callable(reply) else reply


class YahooCaptureV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "archive"
        self.clock = FakeClock()
        self.symbol_patch = patch.object(collector, "symbols", return_value=list(SYMBOLS[:1]))
        self.symbol_patch.start()
        self.addCleanup(self.symbol_patch.stop)

    def collect(self, fetch, *, dates=DEFAULT_DATES, identity="test-v2", probe=False):
        dates = [SESSION] if dates is DEFAULT_DATES else dates
        with patch.object(legacy, "_clock", side_effect=self.clock.now):
            return collector.collect(
                self.store, session_dates=dates, now=self.clock.now(), fetcher=fetch,
                capture_id=identity, probe=probe, sleeper=self.clock.sleep,
                monotonic_clock=self.clock.monotonic,
            )

    def saved(self, manifest, relative):
        return Path(manifest["capture_dir"]) / relative

    def only_record(self, manifest):
        self.assertEqual(len(manifest["records"]), 1)
        return manifest["records"][0]

    def test_first_429_stops_all_150_symbol_days_without_fake_failures(self):
        raw = b"Too Many Requests\n\xff"
        headers = {"Retry-After": "120", "X-RateLimit-Remaining": "0",
                   "Content-Type": "text/plain", "Set-Cookie": "fixture=value",
                   "Authorization": "synthetic fixture", "X-RateLimit-Token": "fixture"}
        fetch = FakeFetcher(self.clock, response(429, raw, headers=headers))
        with patch.object(collector, "symbols", return_value=list(SYMBOLS)):
            result = self.collect(fetch, dates=["2026-11-09", "2026-11-10", "2026-11-11"])
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(len(result["records"]), 150)
        first, *remaining = result["records"]
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["attempts"][0]["http_status"], 429)
        self.assertEqual(self.saved(result, first["attempts"][0]["raw_path"]).read_bytes(), raw)
        for row in remaining:
            self.assertEqual(row["status"], "not_attempted_rate_limited")
            self.assertEqual(row["attempts"], [])
            self.assertIsNone(row["received_at"])
            self.assertIsNone(row["csv_path"])
        self.assertEqual(result["rate_limit"]["actual_requests"], 1)
        self.assertEqual(result["status"], "failed")
        metadata = json.loads(self.saved(result, first["attempts"][0]["metadata_path"]).read_text())
        kept = metadata["response"]["headers"]
        self.assertEqual(kept["retry-after"], "120")
        self.assertEqual(kept["x-ratelimit-remaining"], "0")
        self.assertNotIn("set-cookie", kept)
        self.assertNotIn("authorization", kept)
        self.assertNotIn("x-ratelimit-token", kept)
        self.assertEqual(len(audit_store(self.store)["captures"]), 1)

    def test_success_before_429_is_retained_and_all_later_symbols_are_unattempted(self):
        good = chart_body()
        fetch = FakeFetcher(self.clock, response(body=good), response(429, b"limited"))
        with patch.object(collector, "symbols", return_value=list(SYMBOLS[:3])):
            result = self.collect(fetch)
        self.assertEqual(len(fetch.calls), 2)
        first, failed, skipped = result["records"]
        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(first["status"], "captured")
        self.assertTrue(self.saved(result, first["csv_path"]).is_file())
        self.assertEqual(self.saved(result, first["attempts"][0]["raw_path"]).read_bytes(), good)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(skipped["status"], "not_attempted_rate_limited")
        self.assertGreaterEqual(fetch.calls[1][0] - fetch.calls[0][0], 5)

    def test_long_503_retry_after_stops_without_shortening_server_wait(self):
        raw = b"temporarily unavailable"
        fetch = FakeFetcher(self.clock, response(503, raw, headers={"Retry-After": "3601"}))
        with patch.object(collector, "symbols", return_value=list(SYMBOLS[:3])):
            result = self.collect(fetch)
        first, *remaining = result["records"]
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(len(first["attempts"]), 1)
        self.assertEqual(self.saved(result, first["attempts"][0]["raw_path"]).read_bytes(), raw)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(result["stop_reason"], "provider_deferred")
        blocked = datetime.fromisoformat(result["rate_limit"]["blocked_until"])
        self.assertEqual(blocked, self.clock.now() + timedelta(seconds=3601))
        for row in remaining:
            self.assertEqual(row["status"], "not_attempted_provider_deferred")
            self.assertEqual(row["attempts"], [])
            self.assertIsNone(row["received_at"])

    def test_401_and_403_stop_globally_without_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.store = Path(self.temp.name) / str(status)
                fetch = FakeFetcher(self.clock, response(status, b"blocked"))
                with patch.object(collector, "symbols", return_value=list(SYMBOLS[:3])):
                    result = self.collect(fetch, identity=f"http-{status}")
                self.assertEqual(len(fetch.calls), 1)
                self.assertEqual(result["records"][0]["status"], "failed")
                for row in result["records"][1:]:
                    self.assertEqual(row["status"], "not_attempted_authentication_blocked")
                    self.assertEqual(row["attempts"], [])

    def test_nonretryable_400_and_404_allow_the_next_symbol(self):
        for status in (400, 404):
            with self.subTest(status=status):
                self.store = Path(self.temp.name) / str(status)
                fetch = FakeFetcher(self.clock, response(status, b"not available"),
                                    response(symbol=SYMBOLS[1]))
                with patch.object(collector, "symbols", return_value=list(SYMBOLS[:2])):
                    result = self.collect(fetch, identity=f"http-{status}")
                self.assertEqual(len(fetch.calls), 2)
                self.assertEqual([len(r["attempts"]) for r in result["records"]], [1, 1])
                self.assertEqual([r["status"] for r in result["records"]], ["failed", "captured"])
                self.assertEqual(result["status"], "partial_success")

    def test_5xx_retries_once_with_backoff_and_preserves_both_bodies(self):
        raw = b"temporary upstream failure\x00\n"
        fetch = FakeFetcher(self.clock, response(503, raw), response())
        result = self.collect(fetch)
        row = self.only_record(result)
        self.assertEqual(len(fetch.calls), 2)
        self.assertGreaterEqual(fetch.calls[1][0] - fetch.calls[0][0], 5)
        self.assertGreaterEqual(sum(self.clock.sleeps), 5)
        self.assertEqual([a["http_status"] for a in row["attempts"]], [503, 200])
        self.assertEqual(self.saved(result, row["attempts"][0]["raw_path"]).read_bytes(), raw)
        self.assertEqual(row["status"], "captured")

    def test_transient_failures_never_make_a_third_attempt(self):
        for name, replies in (
            ("server", (response(500, b"one"), response(502, b"two"))),
            ("transport", (legacy.CaptureTransportError("one"), legacy.CaptureTransportError("two"))),
        ):
            with self.subTest(kind=name):
                self.store = Path(self.temp.name) / name
                fetch = FakeFetcher(self.clock, *replies)
                result = self.collect(fetch, identity=name)
                row = self.only_record(result)
                self.assertEqual(len(fetch.calls), 2)
                self.assertEqual(len(row["attempts"]), 2)
                self.assertEqual(row["status"], "failed")
                for attempt in row["attempts"]:
                    self.assertTrue(self.saved(result, attempt["metadata_path"]).is_file())
                    if name == "transport":
                        self.assertIsNone(attempt["raw_path"])

    def test_empty_or_malformed_200_retries_with_delay(self):
        empty = chart_body(timestamps=[], quote={key: [] for key in
                           ("open", "high", "low", "close", "volume")})
        wrong_interval = json.loads(chart_body())
        wrong_interval["chart"]["result"][0]["meta"]["dataGranularity"] = "1d"
        for name, raw in (("empty", empty), ("malformed", b"not json\xff"),
                          ("wrong-interval", json.dumps(wrong_interval).encode())):
            with self.subTest(kind=name):
                self.store = Path(self.temp.name) / name
                fetch = FakeFetcher(self.clock, response(body=raw), response())
                result = self.collect(fetch, identity=name)
                row = self.only_record(result)
                self.assertEqual(len(fetch.calls), 2)
                self.assertGreaterEqual(fetch.calls[1][0] - fetch.calls[0][0], 5)
                self.assertEqual(row["status"], "captured")
                self.assertEqual(self.saved(result, row["attempts"][0]["raw_path"]).read_bytes(), raw)

    def test_schema_valid_partial_data_is_kept_without_fetching_a_better_version(self):
        times = [stamp(clock="08:55"), stamp(), stamp(), stamp(clock="09:05") + 1]
        quote = {"open": [99, 100, None, 101], "high": [100, 101, None, 102],
                 "low": [98, 99, None, 100], "close": [99, 100, None, 101],
                 "volume": [100, 1000, None, 1000]}
        raw = chart_body(timestamps=times, quote=quote)
        fetch = FakeFetcher(self.clock, response(body=raw), response())
        result = self.collect(fetch)
        row = self.only_record(result)
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(row["status"], "captured")
        self.assertEqual(row["rows"], 4)
        saved = list(csv.DictReader(io.StringIO(self.saved(result, row["csv_path"]).read_text())))
        self.assertEqual(len(saved), 4)
        self.assertEqual(saved[1]["Datetime"], saved[2]["Datetime"])
        self.assertEqual(saved[2]["Open"], "")
        self.assertIn("08:55", saved[0]["Datetime"])
        self.assertIn("09:05:01", saved[3]["Datetime"])
        self.assertEqual(self.saved(result, row["attempts"][0]["raw_path"]).read_bytes(), raw)

    def test_successful_requests_are_spaced_by_at_least_five_seconds(self):
        fetch = FakeFetcher(self.clock, *(response(symbol=code) for code in SYMBOLS[:3]))
        with patch.object(collector, "symbols", return_value=list(SYMBOLS[:3])):
            result = self.collect(fetch)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(fetch.calls), 3)
        for before, after in zip(fetch.calls, fetch.calls[1:]):
            self.assertGreaterEqual(after[0] - before[0], 5)
        for _, url, params in fetch.calls:
            self.assertTrue(url.startswith("https://query1.finance.yahoo.com/v8/finance/chart/"))
            self.assertEqual(params["interval"], "5m")
            self.assertEqual(params["includePrePost"], "false")
            self.assertEqual(params["includeAdjustedClose"], "false")

    def test_fixed_exponential_backoff_is_five_ten_twenty_then_capped_at_thirty(self):
        gate = RateController({}, load_policy(), clock=self.clock.now,
                              monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        for attempt in range(1, 6):
            self.assertTrue(gate.retry_wait(attempt, 2700 - self.clock.elapsed))
        self.assertEqual(self.clock.sleeps, [5, 10, 20, 30, 30])

    def test_http_wrapper_keeps_ten_second_timeout_verified_tls_and_no_redirects(self):
        raw = b"provider redirect body\x00\xff\n"
        original = SimpleNamespace(status_code=302, headers={"Location": "https://example.invalid"},
                                   content=raw, url="https://query1.finance.yahoo.com/fixture")
        with patch("curl_cffi.requests.get", return_value=original) as get:
            saved = collector._fetch("https://query1.finance.yahoo.com/fixture", {"interval": "5m"})
        self.assertEqual(saved.body, raw)
        self.assertEqual(saved.status, 302)
        args, kwargs = get.call_args
        self.assertEqual(args, ("https://query1.finance.yahoo.com/fixture",))
        self.assertEqual(kwargs["timeout"], 10)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertIsNot(kwargs.get("verify", True), False)
        self.assertEqual(set(kwargs), {"params", "timeout", "allow_redirects"})

    def test_http_wrapper_converts_only_known_transport_exceptions(self):
        from curl_cffi import requests as curl_requests

        with patch("curl_cffi.requests.get", side_effect=curl_requests.RequestsError("fixture timeout")):
            with self.assertRaises(legacy.CaptureTransportError):
                collector._fetch("https://query1.finance.yahoo.com/fixture", {})
        with patch("curl_cffi.requests.get", side_effect=RuntimeError("fixture programming error")):
            with self.assertRaisesRegex(RuntimeError, "fixture programming error"):
                collector._fetch("https://query1.finance.yahoo.com/fixture", {})

    def test_probe_200_is_one_request_without_derived_csv_or_primary_observation(self):
        fetch = FakeFetcher(self.clock, response())
        with patch.object(collector, "symbols", return_value=list(SYMBOLS)):
            result = self.collect(fetch, probe=True)
        row = self.only_record(result)
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(row["symbol"], SYMBOLS[0])
        self.assertEqual(result["status"], "probe_only")
        self.assertEqual(row["status"], "probe_only")
        self.assertEqual(row["probe_result"], "available")
        self.assertIsNone(row["csv_path"])
        self.assertFalse(any(path.startswith("derived/") for path in result["files"]))
        add_receipt(self.store, [result["capture_id"]], "a" * 40, result["completed_at"])
        self.assertEqual(select_primary(self.store, as_of=self.clock.now())["selected"], [])

    def test_probe_429_and_503_never_retry_or_create_observations(self):
        for status, expected in ((429, "rate_limited"), (503, "unavailable")):
            with self.subTest(status=status):
                self.store = Path(self.temp.name) / str(status)
                fetch = FakeFetcher(self.clock, response(status, b"diagnostic only"))
                result = self.collect(fetch, probe=True, identity=f"probe-{status}")
                row = self.only_record(result)
                self.assertEqual(len(fetch.calls), 1)
                self.assertEqual(row["status"], "probe_only")
                self.assertEqual(row["probe_result"], expected)
                self.assertIsNone(row["csv_path"])
                self.assertEqual(len(row["attempts"]), 1)

    def test_probe_during_persisted_cooldown_seals_zero_request_diagnostic(self):
        first = self.collect(FakeFetcher(self.clock, response(429, b"limited",
                            headers={"Retry-After": "120"})), identity="rate-limit")
        fetch = FakeFetcher(self.clock)
        probe = self.collect(fetch, identity="cooldown-probe", probe=True)
        self.assertEqual(fetch.calls, [])
        row = self.only_record(probe)
        self.assertEqual(row["status"], "probe_only")
        self.assertIsNone(row["csv_path"])
        self.assertEqual(row["attempts"], [])
        self.assertEqual(probe["rate_limit"]["actual_requests"], 0)
        self.assertEqual(len(audit_store(self.store)["captures"]), 2)
        self.assertEqual(self.saved(first, first["records"][0]["attempts"][0]["raw_path"]).read_bytes(),
                         b"limited")

    def test_legacy_429_is_inherited_even_if_a_later_legacy_attempt_succeeded(self):
        old_fetch = FakeFetcher(self.clock, response(429, b"old restriction"), response())
        with patch.object(legacy, "symbols", return_value=list(SYMBOLS[:1])):
            with patch.object(legacy, "_clock", side_effect=self.clock.now):
                original = legacy.collect(self.store, session_dates=[SESSION], now=self.clock.now(),
                                          fetcher=old_fetch, capture_id="legacy-capture")
        self.assertEqual(original["records"][0]["status"], "captured")
        self.assertEqual(len(old_fetch.calls), 2)
        fetch = FakeFetcher(self.clock)
        diagnostic = self.collect(fetch, probe=True, identity="inherited-probe")
        self.assertEqual(fetch.calls, [])
        self.assertEqual(self.only_record(diagnostic)["status"], "probe_only")
        self.assertEqual(diagnostic["rate_limit"]["actual_requests"], 0)
        self.assertEqual(len(audit_store(self.store)["captures"]), 2)

    def test_probe_rejects_multiple_manual_sessions_before_any_http(self):
        fetch = FakeFetcher(self.clock)
        with self.assertRaises(ValueError):
            self.collect(fetch, dates=["2026-11-09", "2026-11-10"], probe=True)
        self.assertEqual(fetch.calls, [])

    def test_automatic_probe_on_holiday_uses_latest_closed_protocol_session_only(self):
        self.clock = FakeClock(datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc))
        fetch = FakeFetcher(self.clock, response(day="2026-11-20"))
        with patch.object(collector, "symbols", return_value=list(SYMBOLS)):
            result = self.collect(fetch, dates=None, probe=True)
        row = self.only_record(result)
        self.assertEqual(result["requested_sessions"], ["2026-11-20"])
        self.assertEqual(row["session_date"], "2026-11-20")
        self.assertEqual(row["symbol"], SYMBOLS[0])
        self.assertEqual(row["status"], "probe_only")
        self.assertEqual(row["probe_result"], "available")
        self.assertEqual(len(fetch.calls), 1)

    def test_automatic_normal_collection_skips_ineligible_dates_before_http_or_journal(self):
        for i, now in enumerate((
            datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 9, 7, 29, 59, tzinfo=timezone.utc),
            datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc),
            datetime(2026, 12, 8, 8, 0, tzinfo=timezone.utc),
        )):
            with self.subTest(now=now):
                self.clock = FakeClock(now)
                self.store = Path(self.temp.name) / f"ineligible-{i}"
                fetch = FakeFetcher(self.clock)
                result = self.collect(fetch, dates=None, identity=f"skip-{i}")
                self.assertEqual(fetch.calls, [])
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["records"], [])
                self.assertEqual(audit_store(self.store)["captures"], [])

    def test_programming_error_is_rethrown_after_prior_evidence_is_sealed(self):
        raw = chart_body()
        fetch = FakeFetcher(self.clock, response(body=raw), RuntimeError("synthetic defect"))
        with patch.object(collector, "symbols", return_value=list(SYMBOLS[:2])):
            with self.assertRaisesRegex(RuntimeError, "synthetic defect"):
                self.collect(fetch, identity="aborted")
        captures = audit_store(self.store)["captures"]
        self.assertEqual(len(captures), 1)
        row = captures[0]["records"][0]
        self.assertEqual(row["status"], "captured")
        self.assertEqual(self.saved(captures[0], row["attempts"][0]["raw_path"]).read_bytes(), raw)

    def test_global_time_budget_does_not_start_another_request_without_timeout_room(self):
        def slow_success(_url, _params):
            self.clock.elapsed += 2695
            return response()

        fetch = FakeFetcher(self.clock, slow_success)
        with patch.object(collector, "symbols", return_value=list(SYMBOLS[:2])):
            result = self.collect(fetch)
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(result["records"][0]["status"], "captured")
        self.assertEqual(result["records"][1]["attempts"], [])
        self.assertIsNone(result["records"][1]["received_at"])

    def test_late_november_ninth_capture_stays_late_and_legacy_sources_are_unchanged(self):
        folder = Path(legacy.__file__).parent
        before = {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        result = self.collect(FakeFetcher(self.clock, response()))
        row = self.only_record(result)
        self.assertEqual(row["session_date"], "2026-11-09")
        self.assertFalse(row["on_time"])
        add_receipt(self.store, [result["capture_id"]], "b" * 40, result["completed_at"])
        selected = select_primary(self.store, as_of=self.clock.now())["selected"]
        self.assertEqual(len(selected), 1)
        self.assertFalse(selected[0]["on_time"])
        after = {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
