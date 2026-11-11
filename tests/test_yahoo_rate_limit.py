from datetime import datetime, timedelta, timezone
import unittest

from research.forward_capture_v2.policy import load_policy
from research.forward_capture_v2.rate_limit import RateController, restriction, retry_after, state_from_archive


NOW = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = NOW
        self.tick = 0.
        self.waits = []

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.tick += seconds
        self.now += timedelta(seconds=seconds)


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy()

    def gate(self, audit=None):
        clock = Clock()
        gate = RateController(audit or {'captures': []}, self.policy,
                              clock=lambda: clock.now, monotonic=lambda: clock.tick, sleep=clock.sleep)
        return gate, clock

    def test_retry_after_seconds_case_and_http_date_are_not_capped(self):
        parsed = retry_after({'Retry-After': '7200'}, NOW)
        self.assertEqual(parsed['deadline'], NOW + timedelta(hours=2))
        date = retry_after({'rEtRy-AfTeR': 'Mon, 09 Nov 2026 12:30:00 GMT'}, NOW)
        self.assertEqual(date['deadline'], NOW + timedelta(hours=2, minutes=30))
        deadline, _ = restriction({'Retry-After': '7200'}, NOW, self.policy)
        self.assertEqual(deadline, NOW + timedelta(hours=2))

    def test_missing_invalid_and_past_headers_use_conservative_fallback(self):
        for raw in (None, '-1', 'invalid', '0.5', 'Mon, 09 Nov 2026 09:00:00 GMT'):
            with self.subTest(raw=raw):
                deadline, _ = restriction({} if raw is None else {'Retry-After': raw}, NOW, self.policy)
                self.assertEqual(deadline, NOW + timedelta(hours=1))

    def test_huge_valid_delay_never_becomes_a_shorter_retry(self):
        result = retry_after({'Retry-After': '9' * 1000}, NOW)
        self.assertEqual(result['deadline'], datetime.max.replace(tzinfo=timezone.utc))

    def test_http_date_accounts_conservatively_for_server_clock_skew(self):
        result = retry_after({'Retry-After': 'Mon, 09 Nov 2026 11:00:00 GMT',
                              'Date': 'Mon, 09 Nov 2026 08:00:00 GMT'}, NOW)
        self.assertEqual(result['deadline'], NOW + timedelta(hours=3))

    def test_existing_longer_deadline_cannot_be_reset_by_another_response(self):
        deadline, _ = restriction({'Retry-After': '1'}, NOW, self.policy, NOW + timedelta(days=2))
        self.assertEqual(deadline, NOW + timedelta(days=2))

    def test_legacy_429_and_zero_request_snapshot_keep_the_maximum_deadline(self):
        audit = {'captures': [
            {'capture_id': 'old', 'completed_at': NOW.isoformat(),
             'records': [{'received_at': NOW.isoformat(), 'attempts': [{'http_status': 429}]}]},
            {'capture_id': 'noop', 'completed_at': NOW.isoformat(), 'records': [], 'rate_limit': {'blocked_until': None}},
        ]}
        state = state_from_archive(audit, self.policy)
        self.assertEqual(state['blocked_until'], (NOW + timedelta(hours=1)).isoformat())
        gate, _ = self.gate(audit)
        self.assertFalse(gate.before_request(2700))
        self.assertEqual(gate.actual_requests, 0)

    def test_429_stops_this_run_even_if_the_clock_advances_past_cooldown(self):
        gate, clock = self.gate()
        self.assertTrue(gate.before_request(2700))
        gate.observe(429, {'Retry-After': '7200'}, clock.now)
        clock.now += timedelta(days=1)
        self.assertFalse(gate.before_request(2700))
        self.assertFalse(gate.retry_wait(1, 2700))
        self.assertEqual(gate.actual_requests, 1)
        self.assertEqual(clock.waits, [])

    def test_every_attempt_is_paced_and_retry_backoff_is_bounded(self):
        gate, clock = self.gate()
        self.assertTrue(gate.before_request(2700))
        gate.observe(200, {}, clock.now)
        self.assertTrue(gate.before_request(2700))
        self.assertEqual(clock.tick, 5)
        gate.observe(None, {}, clock.now)
        self.assertTrue(gate.retry_wait(1, 2700))
        self.assertTrue(gate.before_request(2700))
        self.assertEqual(clock.tick, 10)
        self.assertEqual(gate.actual_requests, 3)

    def test_budget_cannot_be_spent_waiting_then_start_another_request(self):
        gate, clock = self.gate()
        self.assertTrue(gate.before_request(2700))
        self.assertFalse(gate.before_request(4))
        self.assertEqual(gate.actual_requests, 1)
        self.assertEqual(clock.waits, [])

    def test_long_service_retry_after_stops_without_sleeping(self):
        gate, clock = self.gate()
        gate.before_request(2700)
        gate.observe(503, {'Retry-After': '3601'}, NOW)
        self.assertEqual(gate.stop_reason, 'provider_deferred')
        self.assertEqual(gate.blocked_until, NOW + timedelta(seconds=3601))
        self.assertFalse(gate.retry_wait(1, 2700))
        self.assertEqual(clock.waits, [])
