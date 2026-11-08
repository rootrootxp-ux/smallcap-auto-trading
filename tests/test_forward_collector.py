"""Offline tests for the fixed forward-data collection protocol.

The HTTP dependency is always injected.  Fixtures deliberately keep incomplete
bars so retries cannot select a more attractive version of the same session.
"""
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research.forward_study import collector
from research.forward_study.store import audit_store


JST = ZoneInfo("Asia/Tokyo")
SYMBOL = "3994.T"
SESSION = "2026-11-09"
NOW = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)


def stamp(date=SESSION, clock="09:00"):
    return int(datetime.fromisoformat(f"{date}T{clock}:00").replace(tzinfo=JST).timestamp())


def chart_body(*, symbol=SYMBOL, timestamps=None, quote=None, error=None):
    if timestamps is None:
        timestamps = [stamp(), stamp(clock="09:05")]
    if quote is None:
        quote = {"open": [100, 101], "high": [102, 103], "low": [99, 100],
                 "close": [101, 102], "volume": [1000, 2000]}
    data = {"chart": {"result": [{
        "meta": {"symbol": symbol, "exchangeTimezoneName": "Asia/Tokyo",
                 "dataGranularity": "5m", "instrumentType": "EQUITY"},
        "timestamp": timestamps, "indicators": {"quote": [quote]},
    }], "error": error}}
    # Intentional whitespace tests byte-for-byte preservation, not re-encoding.
    return ("\n  " + json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")


def response(body=None, status=200):
    return collector.FetchResponse(
        status=status,
        headers={"Content-Type": "application/json", "Date": "Mon, 09 Nov 2026 08:00:00 GMT",
                 "Set-Cookie": "synthetic-cookie=fixture", "Authorization": "synthetic fixture"},
        body=chart_body() if body is None else body,
        url=f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}",
    )


class FakeFetcher:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if not self.replies:
            raise AssertionError("unexpected HTTP attempt")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class ForwardCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        self.symbol_patch = patch.object(collector, "symbols", return_value=[SYMBOL])
        self.symbol_patch.start()
        self.addCleanup(self.symbol_patch.stop)

    def collect(self, fetcher, *, now=NOW, session_dates=None, capture_id="test-capture"):
        return collector.collect(
            self.store, session_dates=[SESSION] if session_dates is None else session_dates,
            now=now, fetcher=fetcher, capture_id=capture_id,
        )

    def stored_file(self, relative):
        matches = [p for p in self.store.rglob("*")
                   if p.is_file() and str(p).endswith("/" + relative)]
        self.assertEqual(len(matches), 1, relative)
        return matches[0]

    def only_record(self, result):
        self.assertEqual(len(result["records"]), 1)
        row = result["records"][0]
        self.assertEqual(row["symbol"], SYMBOL)
        self.assertEqual(row["session_date"], SESSION)
        return row

    def assert_utc(self, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_manual_capture_uses_exact_day_unadjusted_five_minute_request(self):
        raw = chart_body()
        fetch = FakeFetcher(response(raw))
        result = self.collect(fetch)
        row = self.only_record(result)
        self.assertEqual(len(fetch.calls), 1)
        url, params = fetch.calls[0]
        self.assertEqual(url, f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}")
        self.assertEqual(params["interval"], "5m")
        self.assertIn(params["includePrePost"], (False, "false"))
        self.assertIn(params["includeAdjustedClose"], (False, "false"))
        self.assertEqual(int(params["period1"]), stamp(clock="00:00"))
        self.assertEqual(int(params["period2"]), stamp("2026-11-10", "00:00"))
        self.assertEqual(row["rows"], 2)
        self.assertTrue(row["on_time"])
        self.assertEqual(len(row["attempts"]), 1)
        attempt = row["attempts"][0]
        self.assertEqual(attempt["status"], "captured")
        self.assertEqual(self.stored_file(attempt["raw_path"]).read_bytes(), raw)
        self.assert_utc(attempt["requested_at"])
        self.assert_utc(attempt["received_at"])
        self.assert_utc(row["received_at"])
        self.assertEqual(len(audit_store(self.store)["captures"]), 1)

    def test_first_schema_valid_partial_response_is_kept_without_quality_retry(self):
        times = [stamp(clock="08:55"), stamp(), stamp(), stamp(clock="09:05") + 1]
        quote = {"open": [99, 100, None, 101], "high": [100, 101, None, 102],
                 "low": [98, 99, None, 100], "close": [99, 100, None, 101],
                 "volume": [100, 1000, None, 1000]}
        raw = chart_body(timestamps=times, quote=quote)
        fetch = FakeFetcher(response(raw), response())
        row = self.only_record(self.collect(fetch))
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(len(row["attempts"]), 1)
        self.assertEqual(row["attempts"][0]["status"], "captured")
        self.assertEqual(self.stored_file(row["attempts"][0]["raw_path"]).read_bytes(), raw)
        saved = list(csv.DictReader(io.StringIO(self.stored_file(row["csv_path"]).read_text())))
        self.assertEqual(len(saved), 4)
        self.assertEqual(list(saved[0]), ["Datetime", "Open", "High", "Low", "Close", "Volume"])
        self.assertIn("08:55", saved[0]["Datetime"])
        self.assertEqual(saved[1]["Datetime"], saved[2]["Datetime"])
        self.assertEqual(saved[2]["Open"], "")
        self.assertEqual(saved[2]["Volume"], "")
        self.assertIn("09:05:01", saved[3]["Datetime"])

    def test_http_failure_retries_once_and_retains_both_response_bodies(self):
        failed = b"upstream unavailable\n"
        good = chart_body()
        fetch = FakeFetcher(response(failed, 503), response(good))
        row = self.only_record(self.collect(fetch))
        self.assertEqual(len(fetch.calls), 2)
        self.assertEqual([a["status"] for a in row["attempts"]], ["failed", "captured"])
        self.assertEqual([a["http_status"] for a in row["attempts"]], [503, 200])
        self.assertEqual(self.stored_file(row["attempts"][0]["raw_path"]).read_bytes(), failed)
        self.assertEqual(self.stored_file(row["attempts"][1]["raw_path"]).read_bytes(), good)
        for attempt in row["attempts"]:
            metadata = json.loads(self.stored_file(attempt["metadata_path"]).read_text())
            self.assertEqual(metadata["request"]["params"], fetch.calls[0][1])
            self.assertEqual(metadata["response"]["status"], attempt["http_status"])
            self.assertEqual(metadata["response"]["body_path"], attempt["raw_path"])
            self.assertEqual(metadata["response"]["headers"]["content-type"], "application/json")
            self.assertNotIn("set-cookie", metadata["response"]["headers"])
            self.assertNotIn("authorization", metadata["response"]["headers"])

    def test_transport_error_is_a_recorded_attempt_then_retries(self):
        fetch = FakeFetcher(collector.CaptureTransportError("synthetic timeout"), response())
        row = self.only_record(self.collect(fetch))
        self.assertEqual(len(fetch.calls), 2)
        self.assertEqual([a["status"] for a in row["attempts"]], ["failed", "captured"])
        self.assertTrue(self.stored_file(row["attempts"][0]["metadata_path"]).exists())
        self.assertFalse(row["attempts"][0].get("raw_path"))

    def test_wrong_day_is_empty_then_retry_uses_target_day(self):
        wrong = chart_body(timestamps=[stamp("2026-11-06"), stamp("2026-11-06", "09:05")])
        fetch = FakeFetcher(response(wrong), response())
        row = self.only_record(self.collect(fetch))
        self.assertEqual(len(fetch.calls), 2)
        self.assertEqual([a["status"] for a in row["attempts"]], ["empty", "captured"])
        self.assertEqual(row["rows"], 2)
        self.assertEqual(self.stored_file(row["attempts"][0]["raw_path"]).read_bytes(), wrong)

    def test_empty_and_malformed_responses_stop_after_two_attempts(self):
        empty = chart_body(timestamps=[], quote={key: [] for key in
                           ("open", "high", "low", "close", "volume")})
        malformed = b"{not-json\xff\n"
        fetch = FakeFetcher(response(empty), response(malformed), response())
        row = self.only_record(self.collect(fetch))
        self.assertEqual(len(fetch.calls), 2)
        self.assertEqual([a["status"] for a in row["attempts"]], ["empty", "failed"])
        self.assertEqual(row["rows"], 0)
        self.assertFalse(row["csv_path"])
        self.assertNotEqual(row["status"], "captured")
        self.assertEqual(self.stored_file(row["attempts"][1]["raw_path"]).read_bytes(), malformed)
        self.assertEqual(len(audit_store(self.store)["captures"]), 1)

    def test_schema_failures_retry_without_accepting_wrong_prices(self):
        invalid = {
            "wrong_symbol": chart_body(symbol="1234.T"),
            "chart_error": chart_body(error={"code": "Not Found", "description": "fixture"}),
            "invalid_timestamp": chart_body(timestamps=["not-a-timestamp", stamp(clock="09:05")]),
            "overflow_timestamp": chart_body(timestamps=[10 ** 400, stamp(clock="09:05")]),
            "unequal_arrays": chart_body(quote={"open": [100], "high": [101, 102],
                                               "low": [99, 100], "close": [100, 101],
                                               "volume": [1000, 2000]}),
        }
        for name, raw in invalid.items():
            with self.subTest(name=name):
                fetch = FakeFetcher(response(raw), response())
                row = self.only_record(self.collect(fetch, capture_id=name))
                self.assertEqual(len(fetch.calls), 2)
                self.assertEqual([a["status"] for a in row["attempts"]], ["failed", "captured"])
                # Captures are immutable; use that capture's raw file only.
                files = [p for p in self.store.rglob("attempt-1.body") if p.read_bytes() == raw]
                self.assertEqual(len(files), 1)

    def test_programmer_error_is_not_disguised_as_data_failure(self):
        fetch = FakeFetcher(RuntimeError("synthetic programming defect"))
        with self.assertRaisesRegex(RuntimeError, "programming defect"):
            self.collect(fetch)
        self.assertEqual(len(fetch.calls), 1)

    def test_http_wrapper_uses_timeout_and_preserves_response_bytes(self):
        raw = b"synthetic raw response\x00\xff\n"
        original = SimpleNamespace(status_code=201, headers={"Content-Type": "application/json"},
                                   content=raw, url="https://query1.finance.yahoo.com/fixture")
        with patch("curl_cffi.requests.get", return_value=original) as get:
            saved = collector._fetch("https://query1.finance.yahoo.com/request", {"interval": "5m"})
        self.assertEqual(saved, collector.FetchResponse(201, dict(original.headers), raw, original.url))
        args, kwargs = get.call_args
        self.assertEqual(args, ("https://query1.finance.yahoo.com/request",))
        self.assertEqual(kwargs["params"], {"interval": "5m"})
        self.assertEqual(kwargs["timeout"], 10)
        self.assertIsNot(kwargs.get("verify", True), False)

    def test_http_wrapper_classifies_transport_errors_without_hiding_programmer_errors(self):
        from curl_cffi import requests as curl_requests

        with patch("curl_cffi.requests.get", side_effect=curl_requests.RequestsError("synthetic timeout")):
            with self.assertRaises(collector.CaptureTransportError):
                collector._fetch("https://query1.finance.yahoo.com/request", {})
        with patch("curl_cffi.requests.get", side_effect=RuntimeError("synthetic defect")):
            with self.assertRaisesRegex(RuntimeError, "synthetic defect"):
                collector._fetch("https://query1.finance.yahoo.com/request", {})

    def test_total_budget_preserves_received_data_and_records_unattempted_symbols(self):
        fetch = FakeFetcher(response())
        with patch.object(collector, 'symbols', return_value=[SYMBOL, '4475.T']), \
             patch.object(collector, 'monotonic', side_effect=[0, 0, 3000]):
            result = self.collect(fetch)
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(result['status'], 'partial_success')
        self.assertTrue(result['capture_budget_exhausted'])
        self.assertEqual(result['records'][1]['reason'], 'capture_budget_exhausted')
        self.assertEqual(result['records'][1]['attempts'], [])
        self.assertIsNone(result['records'][1]['received_at'])
        self.assertEqual(len(audit_store(self.store)['captures']), 1)

    def test_programming_abort_preserves_earlier_raw_data_and_remains_fatal(self):
        original = response()
        fetch = FakeFetcher(original, RuntimeError('synthetic implementation defect'))
        with patch.object(collector, 'symbols', return_value=[SYMBOL, '4475.T']):
            with self.assertRaisesRegex(RuntimeError, 'implementation defect'):
                self.collect(fetch)
        capture = audit_store(self.store)['captures'][0]
        self.assertEqual(capture['status'], 'failed')
        self.assertEqual(capture['programming_error']['classification'], 'programming_error')
        self.assertEqual(capture['records'][0]['status'], 'captured')
        self.assertTrue(any(p.read_bytes() == original.body for p in self.store.rglob('*.body')))

    def test_symbol_failure_keeps_another_symbols_captured_evidence(self):
        fetch = FakeFetcher(response(), response(b"malformed"), response(b"still malformed"))
        with patch.object(collector, "symbols", return_value=[SYMBOL, "4475.T"]):
            result = self.collect(fetch)
        self.assertEqual(len(fetch.calls), 3)
        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(len(result["records"]), 2)
        good, failed = result["records"]
        self.assertEqual(good["status"], "captured")
        self.assertEqual(good["rows"], 2)
        self.assertTrue(self.stored_file(good["csv_path"]).exists())
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["csv_path"])
        self.assertEqual(len(failed["attempts"]), 2)
        self.assertEqual(len(audit_store(self.store)["captures"]), 1)

    def test_manual_date_guards_raise_before_any_http(self):
        cases = [
            (["2026-11-06"], NOW),
            (["2026-11-14"], NOW),
            (["2026-12-07"], NOW),
            ([SESSION], datetime(2026, 11, 9, 7, 29, 59, tzinfo=timezone.utc)),
        ]
        for dates, now in cases:
            with self.subTest(dates=dates, now=now):
                fetch = FakeFetcher()
                with self.assertRaises(ValueError):
                    collector.collect(self.store, session_dates=dates, now=now,
                                      fetcher=fetch, capture_id="invalid-date")
                self.assertEqual(fetch.calls, [])

    def test_naive_now_is_rejected_before_any_http(self):
        fetch = FakeFetcher()
        with self.assertRaises(ValueError):
            self.collect(fetch, now=NOW.replace(tzinfo=None))
        self.assertEqual(fetch.calls, [])

    def test_auto_before_start_holiday_and_before_close_do_not_fetch(self):
        times = [
            datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 9, 7, 29, 59, tzinfo=timezone.utc),
        ]
        for i, now in enumerate(times):
            with self.subTest(now=now):
                fetch = FakeFetcher()
                result = collector.collect(self.store, now=now, fetcher=fetch,
                                           capture_id=f"skip-{i}")
                self.assertEqual(fetch.calls, [])
                self.assertEqual(result.get("records", []), [])

    def test_close_cutoff_is_inclusive_and_late_manual_capture_is_labeled(self):
        exact_close = datetime(2026, 11, 9, 7, 30, tzinfo=timezone.utc)
        first = self.only_record(self.collect(FakeFetcher(response()), now=exact_close,
                                             capture_id="at-cutoff"))
        self.assertTrue(first["on_time"])
        late = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
        second = self.only_record(self.collect(FakeFetcher(response()), now=late,
                                              capture_id="late-capture"))
        self.assertFalse(second["on_time"])

    def test_fixed_final_cutoff_is_inclusive_and_independent_of_session_timeliness(self):
        cutoff = datetime(2026, 12, 7, 9, 30, tzinfo=timezone.utc)
        exact = self.only_record(self.collect(FakeFetcher(response()), now=cutoff,
                                             capture_id="final-cutoff"))
        self.assertFalse(exact["on_time"])
        self.assertFalse(exact["after_final_cutoff"])
        after = cutoff.replace(second=1)
        late = self.only_record(self.collect(FakeFetcher(response()), now=after,
                                            capture_id="after-final-cutoff"))
        self.assertFalse(late["on_time"])
        self.assertTrue(late["after_final_cutoff"])

    def test_automatic_run_limits_work_to_current_previous_and_oldest_missing(self):
        calls = []

        def fetch(url, params):
            calls.append((url, dict(params)))
            day = datetime.fromtimestamp(int(params["period1"]), JST).date().isoformat()
            return response(chart_body(timestamps=[stamp(day), stamp(day, "09:05")]))

        now = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
        result = collector.collect(self.store, now=now, fetcher=fetch, capture_id="auto-first")
        self.assertEqual(len(calls), 3)
        self.assertEqual({r["session_date"] for r in result["records"]},
                         {"2026-11-13", "2026-11-12", "2026-11-09"})
        # The oldest successfully captured session is not a missing-session
        # target on the next run, while the declared previous-day review stays.
        calls.clear()
        repeated = collector.collect(self.store, now=now, fetcher=fetch,
                                     capture_id="auto-second")
        self.assertEqual(len(calls), 3)
        self.assertEqual({r["session_date"] for r in repeated["records"]},
                         {"2026-11-13", "2026-11-12", "2026-11-10"})


if __name__ == "__main__":
    unittest.main()
