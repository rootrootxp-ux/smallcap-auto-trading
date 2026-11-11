"""Paced public-chart acquisition with durable, archive-wide provider stops.

The registered research package remains unchanged.  This separate collector
reuses its calendar and schema parser; it never evaluates prices or orders.
"""
from datetime import datetime, time, timedelta
import json
import math
from pathlib import Path
import re
import time as timers
from urllib.parse import quote
import uuid
from zoneinfo import ZoneInfo

from research.forward_study import collector as legacy
from research.forward_study.protocol import load_protocol, symbols
from research.forward_study.store import audit_store, seal_capture

from .policy import load_policy, provenance
from .rate_limit import RateController


FetchResponse = legacy.FetchResponse
CaptureTransportError = legacy.CaptureTransportError


def _fetch(url, params):
    """One fixed public endpoint, verified TLS, no redirects or HTTP retries."""
    from curl_cffi import requests
    try:
        response = requests.get(url, params=params,
                                timeout=load_policy()['request_timeout_seconds'],
                                allow_redirects=False)
    except requests.RequestsError as exc:
        raise CaptureTransportError(str(exc)) from exc
    return FetchResponse(int(response.status_code), dict(response.headers),
                         response.content, str(response.url))


def _headers(values):
    result = {}
    for name, value in values.items():
        key = str(name).lower()
        quota = key == 'ratelimit' or key.startswith(('ratelimit-', 'x-ratelimit-'))
        sensitive = any(part in key for part in ('authorization', 'cookie', 'secret', 'token', 'api-key'))
        if not sensitive and (key in legacy._RESPONSE_HEADERS or quota):
            result[key] = str(value)
    return result


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       allow_nan=False, indent=2) + '\n').encode('utf-8')


def _probe_dates(protocol, requested, local_now):
    if requested is not None:
        if not isinstance(requested, list) or len(requested) != 1:
            raise ValueError('A probe accepts exactly one manual session date')
        return legacy._requested(protocol, {}, requested, local_now)
    today = local_now.date().isoformat()
    closed = time.fromisoformat(protocol['capture_not_before_local'])
    eligible = [day for day in protocol['sessions']
                if day < today or (day == today and local_now.time().replace(tzinfo=None) >= closed)]
    return ([eligible[-1]], None) if eligible else ([], 'no_closed_protocol_session')


def _stop_kind(controller, known=None):
    if known:
        return known
    reason = str(controller.stop_reason or '').lower()
    if any(word in reason for word in ('429', 'cooldown', 'rate_limit', 'rate limit')):
        return 'rate_limited'
    if any(word in reason for word in ('401', '403', 'authentication', 'authorization', 'auth_')):
        return 'authentication_blocked'
    if 'budget' in reason:
        return 'budget_exhausted'
    return 'provider_deferred'


def _not_attempted(record, kind, probe):
    record.update(status='probe_only' if probe else 'not_attempted_' + kind,
                  reason=kind, received_at=None, on_time=None, after_final_cutoff=None)
    if probe:
        record['probe_result'] = kind if kind in {'rate_limited', 'authentication_blocked'} else 'not_attempted'


def collect(store: Path, session_dates: list[str] | None = None,
            now: datetime | None = None, fetcher=None,
            capture_id: str | None = None, probe=False, sleeper=None,
            monotonic_clock=None, guard_evidence=None) -> dict:
    """Seal observed response bytes; a provider stop prevents all later HTTP.

    All clocks, sleeps and HTTP can be injected for offline tests.  A probe uses
    one fixed symbol, one closed session and at most one communication attempt;
    it never emits a captured record or a primary derived-CSV path.
    """
    protocol, policy = load_protocol(), load_policy()
    clock = monotonic_clock or timers.monotonic
    sleep = sleeper or timers.sleep
    started, started_tick = legacy._clock(now), clock()
    zone = ZoneInfo(protocol['timezone'])
    local_now = started.astimezone(zone)
    choose = _probe_dates if probe else legacy._requested
    if probe:
        requested, reason = choose(protocol, session_dates, local_now)
    else:
        requested, reason = choose(protocol, {}, session_dates, local_now)
    if reason:
        return {'status': 'skipped', 'reason': reason, 'probe': bool(probe),
                'started_at': legacy._iso(started), 'completed_at': legacy._iso(legacy._clock(now)),
                'requested_sessions': [], 'records': []}
    audit = audit_store(Path(store))
    if not probe:
        requested, _ = legacy._requested(protocol, audit, session_dates, local_now)
    codes = list(symbols())
    if not codes or any(not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', code) for code in codes):
        raise ValueError('Invalid fixed research symbols')
    if probe:
        codes = codes[:1]
    identity = capture_id or started.strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8]
    if (not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,180}', identity)
            or identity in {'.', '..'}):
        raise ValueError('Unsafe capture identifier')
    if any(row['capture_id'] == identity for row in audit.get('captures', [])):
        raise ValueError('Capture ID already exists; use a new identifier')
    budget = float(policy['max_capture_seconds'])
    request_timeout = float(policy['request_timeout_seconds'])
    limit = int(policy['request_max_attempts'])
    if (not math.isfinite(budget) or budget <= 0 or limit != 2
            or limit != protocol['request_max_attempts']
            or budget != float(protocol['max_capture_seconds'])
            or policy['request_timeout_seconds'] != protocol['request_timeout_seconds']
            or policy['probe_max_requests'] != 1):
        raise ValueError('Unsupported original acquisition budget or retry limit')
    cutoff = datetime.fromisoformat(protocol['final_cutoff'])
    controller = RateController(audit, policy, clock=lambda: legacy._clock(now),
                                monotonic=clock, sleep=sleep)
    transport = fetcher or _fetch
    files, records, active = {}, [], {}
    if guard_evidence is not None:
        files['publication_guard.json'] = _json(guard_evidence)
    stop_kind = None

    def remaining():
        return max(0., budget - (clock() - started_tick))

    def payload(failed=False):
        captured = sum(row['status'] == 'captured' for row in records)
        status = ('failed' if failed else 'probe_only' if probe else
                  'success' if records and captured == len(records) else
                  'partial_success' if captured else 'failed')
        completed = legacy._clock(now)
        value = {'status': status, 'started_at': legacy._iso(started),
                 'completed_at': legacy._iso(completed), 'requested_sessions': requested,
                 'records': records, 'probe': bool(probe),
                 'trigger': 'probe' if probe else 'manual' if session_dates is not None else 'automatic',
                 'provider': 'Yahoo Finance public chart', 'data_source_id': 'yahoo-chart-v8',
                 'acquisition_version': 2, 'acquisition_implementation': provenance(),
                 'interval': '5m', 'price_fields': 'unadjusted quote OHLCV; adjclose not used',
                 'final_cutoff': protocol['final_cutoff'], 'after_final_cutoff': completed > cutoff,
                 'capture_budget_exhausted': stop_kind == 'budget_exhausted',
                 'max_capture_seconds': budget, 'stop_reason': stop_kind or controller.stop_reason,
                 'rate_limit': controller.snapshot()}
        if probe:
            value['probe_result'] = records[0].get('probe_result', 'unavailable') if records else 'not_attempted'
        return value

    try:
        for day in requested:
            day_start = datetime.fromisoformat(day).replace(tzinfo=zone)
            params = {'interval': '5m', 'period1': int(day_start.timestamp()),
                      'period2': int((day_start + timedelta(days=1)).timestamp()),
                      'includePrePost': 'false', 'includeAdjustedClose': 'false'}
            for symbol in codes:
                attempts = []
                record = {'symbol': symbol, 'session_date': day, 'status': 'probe_only' if probe else 'failed',
                          'csv_path': None, 'rows': 0, 'attempts': attempts,
                          'outside_target_day_rows': 0}
                url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + quote(symbol, safe='')
                for attempt in range(1, (1 if probe else limit) + 1):
                    left = remaining()
                    if not stop_kind and left <= request_timeout:
                        stop_kind = 'budget_exhausted'
                    # Reserve one full HTTP timeout before the pacing gate can
                    # spend time waiting and count a real communication attempt.
                    if stop_kind or not controller.before_request(left - request_timeout):
                        stop_kind = _stop_kind(controller, stop_kind)
                        if not attempts:
                            _not_attempted(record, stop_kind, probe)
                        else:
                            record['retry_stop_reason'] = stop_kind
                        break
                    prefix = f'raw/{day}/{symbol}/attempt-{attempt}'
                    metadata_path, raw_path = prefix + '.json', prefix + '.body'
                    requested_at = legacy._clock(now)
                    metadata = {'request': {'url': url, 'params': dict(params),
                                            'requested_at': legacy._iso(requested_at)}, 'response': None}
                    info = {'attempt': attempt, 'requested_at': legacy._iso(requested_at),
                            'metadata_path': metadata_path, 'raw_path': None,
                            'http_status': None, 'response_url': None}
                    active.update(metadata_path=metadata_path, metadata=metadata, info=info)
                    retry = False
                    try:
                        response = transport(url, dict(params))
                    except CaptureTransportError as exc:
                        received = legacy._clock(now)
                        controller.observe(None, {}, received)
                        info.update(status='failed', reason='transport_error', transport_error=str(exc)[:1000],
                                    received_at=legacy._iso(received))
                        retry = True
                        probe_result = 'unavailable'
                    else:
                        received = legacy._clock(now)
                        if not isinstance(response, FetchResponse) or not isinstance(response.body, bytes):
                            raise TypeError('fetcher must return FetchResponse with body bytes')
                        files[raw_path] = response.body
                        response_headers = _headers(response.headers)
                        info.update(raw_path=raw_path, http_status=response.status,
                                    response_url=response.url, received_at=legacy._iso(received))
                        metadata['response'] = {'status': response.status, 'url': response.url,
                            'headers': response_headers, 'received_at': legacy._iso(received), 'body_path': raw_path}
                        controller.observe(response.status, response_headers, received)
                        probe_result = 'unavailable'
                        if response.status == 429:
                            info.update(status='failed', reason='http_429_rate_limited')
                            stop_kind, probe_result = 'rate_limited', 'rate_limited'
                        elif response.status in (401, 403):
                            info.update(status='failed', reason='http_authentication_blocked')
                            stop_kind, probe_result = 'authentication_blocked', 'authentication_blocked'
                        elif not 200 <= response.status < 300:
                            info.update(status='failed', reason='http_error')
                            retry = response.status in policy['retryable_http_statuses']
                        else:
                            try:
                                normalized, count, outside = legacy._normalize(response.body, symbol, day, zone)
                            except legacy._MalformedResponse as exc:
                                info.update(status='failed', reason=str(exc))
                                retry = True
                            else:
                                info.update(status='captured' if normalized is not None else 'empty',
                                            reason='target_day_observed' if normalized is not None else 'no_target_day_rows',
                                            rows=count, outside_target_day_rows=outside)
                                record.update(rows=count, outside_target_day_rows=outside)
                                probe_result = 'available' if normalized is not None else 'empty'
                                retry = normalized is None
                                if normalized is not None and not probe:
                                    csv_path = f'derived/{day}/{symbol.replace(".", "_")}_5m.csv'
                                    files[csv_path] = normalized
                                    record['csv_path'] = csv_path
                    info['rate_limit'] = controller.snapshot()
                    attempts.append(info)
                    metadata['outcome'] = dict(info)
                    files[metadata_path] = _json(metadata)
                    record.update(status='probe_only' if probe else info['status'], reason=info['reason'],
                                  received_at=info['received_at'],
                                  on_time=received.astimezone(zone).date().isoformat() == day,
                                  after_final_cutoff=received > cutoff)
                    if probe:
                        record['probe_result'] = probe_result
                    if stop_kind or controller.stop_reason:
                        stop_kind = _stop_kind(controller, stop_kind)
                        break
                    if probe or info['status'] == 'captured' or not retry or attempt == limit:
                        break
                    if not controller.retry_wait(attempt, max(0., remaining() - request_timeout)):
                        stop_kind = _stop_kind(controller)
                        record['retry_stop_reason'] = stop_kind
                        break
                records.append(record)
    except Exception as original:
        diagnostic = {'type': type(original).__name__, 'classification': 'programming_error',
                      'message': 'Collection aborted; the original exception is re-raised.'}
        try:
            path = active.get('metadata_path')
            if path and path not in files:
                value = dict(active['metadata'])
                value.update(programming_error=diagnostic, outcome=dict(active['info']))
                files[path] = _json(value)
            files['programming_error.json'] = _json(diagnostic)
            value = payload(failed=True)
            value['programming_error'] = diagnostic
            seal_capture(Path(store), identity, value, files)
        except Exception as seal_error:
            raise ExceptionGroup('Collector failed and abort evidence could not be sealed',
                                 [original, seal_error]) from original
        raise
    return seal_capture(Path(store), identity, payload(), files)
