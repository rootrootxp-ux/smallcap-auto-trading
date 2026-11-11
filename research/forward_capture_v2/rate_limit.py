"""Conservative Retry-After handling and one provider-wide request gate."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import time

from research.forward_study.protocol import utc


def _later(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _add_seconds(now, seconds):
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError:
        # Never shorten a valid, very long server restriction into an hour.
        return datetime.max.replace(tzinfo=timezone.utc)


def retry_after(headers, received_at):
    """Parse RFC delay-seconds or HTTP-date; preserve invalid/unknown evidence."""
    now = utc(received_at)
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    raw = headers.get('retry-after')
    if raw is None:
        return {'raw': None, 'deadline': None, 'interpretation': 'absent'}
    value = raw.strip()
    if value.isascii() and value.isdigit():
        if len(value) > 18:
            deadline = datetime.max.replace(tzinfo=timezone.utc)
        else:
            deadline = _add_seconds(now, int(value))
        return {'raw': raw, 'deadline': deadline, 'interpretation': 'delay_seconds'}
    try:
        deadline = utc(parsedate_to_datetime(value))
    except (TypeError, ValueError, OverflowError):
        return {'raw': raw, 'deadline': None, 'interpretation': 'invalid'}
    # Be conservative when the client's clock and the server Date disagree.
    try:
        server_date = utc(parsedate_to_datetime(headers['date']))
        deadline = max(deadline, _add_seconds(now, max(0, (deadline - server_date).total_seconds())))
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return {'raw': raw, 'deadline': deadline, 'interpretation': 'http_date'}


def restriction(headers, received_at, policy, existing=None):
    parsed = retry_after(headers, received_at)
    now = utc(received_at)
    deadline = _later(existing, _add_seconds(now, policy['fallback_cooldown_seconds']))
    deadline = _later(deadline, parsed['deadline'])
    return deadline, {'retry_after': parsed['raw'], 'retry_after_interpretation': parsed['interpretation'],
                      'server_deadline': parsed['deadline'].isoformat() if parsed['deadline'] else None,
                      'received_at': now.isoformat(), 'blocked_until': deadline.isoformat()}


def state_from_archive(audit, policy):
    """Include unreceipted captures and legacy 429s; no-response runs can't reset it."""
    deadline, evidence = None, []
    for capture in audit.get('captures', []):
        saved = capture.get('rate_limit') or {}
        if saved.get('blocked_until'):
            deadline = _later(deadline, utc(saved['blocked_until']))
        for record in capture.get('records', []):
            for attempt in record.get('attempts', []):
                if attempt.get('http_status') not in (401, 403, 429):
                    continue
                received = attempt.get('received_at') or record.get('received_at') or capture['completed_at']
                headers = {}
                relative = attempt.get('metadata_path')
                if relative:
                    # audit_store already verified this exact file's checksum.
                    value = json.loads((Path(capture['capture_dir']) / relative).read_bytes())
                    headers = (value.get('response') or {}).get('headers') or {}
                deadline, detail = restriction(headers, received, policy, deadline)
                detail.update(capture_id=capture['capture_id'], http_status=attempt['http_status'])
                evidence.append(detail)
    return {'blocked_until': deadline.isoformat() if deadline else None, 'evidence': evidence}


class RateController:
    """Once limited, this run never resumes, even if its wall clock advances."""
    def __init__(self, audit, policy, clock=None, monotonic=None, sleep=None):
        self.policy = policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.initial_state = state_from_archive(audit, policy)
        self.blocked_until = utc(self.initial_state['blocked_until']) if self.initial_state['blocked_until'] else None
        self.stop_reason = None
        self.actual_requests = 0
        self.total_wait_seconds = 0.0
        self.events = []
        self._last_start = None
        self._not_before_mono = None
        if self.blocked_until is not None and utc(self.clock()) < self.blocked_until:
            self.stop_reason = 'rate_limit_cooldown'

    def _wait(self, seconds, remaining_seconds):
        seconds = max(0.0, float(seconds))
        if seconds >= remaining_seconds:
            self.stop_reason = 'capture_budget_exhausted'
            return False
        if seconds:
            self.sleep(seconds)
            self.total_wait_seconds += seconds
        return True

    def before_request(self, remaining_seconds):
        if self.stop_reason:
            return False
        now = self.monotonic()
        delay = max(0, self.policy['minimum_interval_seconds'] - (now - self._last_start)) if self._last_start is not None else 0
        if self._not_before_mono is not None:
            delay = max(delay, self._not_before_mono - now)
        if not self._wait(delay, remaining_seconds):
            return False
        self._last_start = self.monotonic()
        self.actual_requests += 1
        return True

    def observe(self, status, headers, received_at):
        headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        if status in (401, 403, 429):
            self.blocked_until, detail = restriction(headers, received_at, self.policy, self.blocked_until)
            self.stop_reason = 'rate_limited' if status == 429 else 'authentication_blocked'
            detail.update(http_status=status, stop_reason=self.stop_reason)
            self.events.append(detail)
            return
        if status is not None and status >= 500:
            parsed = retry_after(headers, received_at)
            if parsed['deadline'] is not None:
                wait = max(0, (parsed['deadline'] - utc(received_at)).total_seconds())
                self.blocked_until = _later(self.blocked_until, parsed['deadline'])
                if wait > self.policy['backoff_cap_seconds']:
                    self.stop_reason = 'provider_deferred'
                else:
                    self._not_before_mono = self.monotonic() + wait
                self.events.append({'http_status': status, 'retry_after': parsed['raw'],
                                    'blocked_until': self.blocked_until.isoformat(),
                                    'stop_reason': self.stop_reason})

    def retry_wait(self, attempt, remaining_seconds):
        if self.stop_reason:
            return False
        delay = min(self.policy['backoff_cap_seconds'], self.policy['backoff_base_seconds'] * 2 ** max(0, attempt - 1))
        if self._not_before_mono is not None:
            delay = max(delay, self._not_before_mono - self.monotonic())
        if delay > self.policy['backoff_cap_seconds']:
            self.stop_reason = 'provider_deferred'
            return False
        return self._wait(delay, remaining_seconds)

    def snapshot(self):
        return {'blocked_until': self.blocked_until.isoformat() if self.blocked_until else None,
                'stop_reason': self.stop_reason, 'actual_requests': self.actual_requests,
                'minimum_interval_seconds': self.policy['minimum_interval_seconds'],
                'total_wait_seconds': self.total_wait_seconds, 'events': self.events,
                'inherited_state': self.initial_state,
                'cross_run_policy': 'maximum observed server/policy deadline; never reset by a zero-request run'}
