"""Collect immutable, observed Yahoo chart responses without evaluating them.

This module has no broker, order, or event-calculator interface.  The first
schema-valid nonempty response for a requested day is kept even when its bars
are incomplete; later responses cannot replace that evidence by quality score.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import csv
import io
import json
import math
from pathlib import Path
import re
from time import monotonic
from urllib.parse import quote
import uuid
from zoneinfo import ZoneInfo

from .protocol import load_protocol, symbols
from .store import audit_store, seal_capture


@dataclass(frozen=True)
class FetchResponse:
    status: int
    headers: dict
    body: bytes
    url: str


class CaptureTransportError(RuntimeError):
    """An expected HTTP transport failure, eligible for the fixed retry limit."""


class _MalformedResponse(ValueError):
    pass


_RESPONSE_HEADERS = frozenset({
    'content-type', 'content-length', 'date', 'server', 'cache-control',
    'expires', 'etag', 'last-modified', 'retry-after', 'x-yahoo-request-id',
})
_FIELDS = ('Open', 'High', 'Low', 'Close', 'Volume')


def _fetch(url, params):
    # curl_cffi is already a dependency of the pinned yfinance installation.
    # Use its default TLS certificate verification; no credentials or cookies.
    from curl_cffi import requests
    try:
        response = requests.get(url, params=params,
                                timeout=load_protocol()['request_timeout_seconds'])
    except requests.RequestsError as exc:
        raise CaptureTransportError(str(exc)) from exc
    return FetchResponse(int(response.status_code), dict(response.headers),
                         response.content, str(response.url))


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _clock(fixed):
    value = fixed if fixed is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('now must be an explicit timezone-aware datetime')
    return value.astimezone(timezone.utc)


def _record_list(manifest):
    return manifest.get('records', manifest.get('payload', {}).get('records', []))


def _requested(protocol, audit, requested, local_now):
    sessions = list(protocol['sessions'])
    close = time.fromisoformat(protocol['capture_not_before_local'])
    today = local_now.date().isoformat()
    if requested is not None:
        if (not isinstance(requested, list) or not requested
                or any(not isinstance(day, str) for day in requested)
                or len(requested) != len(set(requested))):
            raise ValueError('Manual session_dates must be a nonempty list without duplicates')
        for day in requested:
            if day not in sessions:
                raise ValueError(f'Date is not a frozen protocol session: {day}')
            if day > today or (day == today and local_now.time().replace(tzinfo=None) < close):
                raise ValueError(f'Session has not reached its capture time: {day}')
        return list(requested), None
    if today < protocol['start_date'] or today > protocol['end_date']:
        return [], 'outside_protocol_period'
    if today not in sessions:
        return [], 'not_a_protocol_session'
    if local_now.time().replace(tzinfo=None) < close:
        return [], 'before_capture_time'
    chosen = [today]
    prior = [day for day in sessions if day < today]
    review_count = int(protocol.get('auto_review_previous_sessions', 1))
    chosen.extend(reversed(prior[-review_count:]) if review_count else [])
    captured = {(row['symbol'], row['session_date'])
                for manifest in audit.get('captures', []) for row in _record_list(manifest)
                if row.get('status') == 'captured'}
    codes = symbols()
    oldest = next((day for day in sessions if day <= today
                   and day not in chosen and any((code, day) not in captured for code in codes)), None)
    if oldest is not None:
        chosen.append(oldest)
    return chosen[:int(protocol.get('max_sessions_per_run', 3))], None


def _normalize(body, symbol, session_date, zone):
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _MalformedResponse('invalid_json') from exc
    chart = document.get('chart') if isinstance(document, dict) else None
    if not isinstance(chart, dict) or chart.get('error') is not None:
        raise _MalformedResponse('chart_error_or_missing_chart')
    results = chart.get('result')
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise _MalformedResponse('expected_one_chart_result')
    result = results[0]
    meta = result.get('meta')
    if not isinstance(meta, dict) or meta.get('symbol') != symbol:
        raise _MalformedResponse('response_symbol_mismatch')
    timestamps = result.get('timestamp')
    indicators = result.get('indicators')
    quotes = indicators.get('quote') if isinstance(indicators, dict) else None
    if not isinstance(timestamps, list) or not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict):
        raise _MalformedResponse('missing_timestamp_or_quote_arrays')
    arrays = [quotes[0].get(field.lower()) for field in _FIELDS]
    if any(not isinstance(values, list) or len(values) != len(timestamps) for values in arrays):
        raise _MalformedResponse('unaligned_quote_arrays')
    if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)))
           for values in arrays for value in values):
        # Yahoo quote schema is number/null.  In particular, boolean/string
        # "True" must never be inferred by a downstream CSV reader as price 1.
        raise _MalformedResponse('nonnumeric_quote_value')
    rows, outside = [], 0
    for index, stamp in enumerate(timestamps):
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or (isinstance(stamp, float) and (not math.isfinite(stamp) or not stamp.is_integer()))):
            raise _MalformedResponse('invalid_timestamp')
        try:
            instant = datetime.fromtimestamp(stamp, timezone.utc).astimezone(zone)
        except (OverflowError, OSError, ValueError) as exc:
            raise _MalformedResponse('invalid_timestamp') from exc
        if instant.date().isoformat() != session_date:
            outside += 1
            continue
        # Preserve ordering, duplicates, off-session timestamps and null OHLCV.
        # A later quality audit decides availability without repairing prices.
        rows.append([str(instant), *(values[index] for values in arrays)])
    if not rows:
        return None, 0, outside
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(['Datetime', *_FIELDS])
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8'), len(rows), outside


def collect(store: Path, session_dates: list[str] | None = None,
            now: datetime | None = None, fetcher=None,
            capture_id: str | None = None) -> dict:
    """Capture fixed-session observations; retain every attempted response.

    An injected fetcher accepts ``(url, params)`` and returns FetchResponse, or
    raises CaptureTransportError.  Other exceptions are programming failures
    and propagate.  No request is made by automatic runs outside the protocol
    calendar or before its predeclared local capture time.
    """
    protocol = load_protocol()
    started = _clock(now)
    started_monotonic = monotonic()
    zone = ZoneInfo(protocol['timezone'])
    local_now = started.astimezone(zone)
    # Guard before touching a store or a transport.  Manual dates still undergo
    # the same frozen-calendar and already-closed-session validation.
    requested, reason = _requested(protocol, {}, session_dates, local_now)
    if reason:
        return {'status': 'skipped', 'reason': reason, 'started_at': _iso(started),
                'completed_at': _iso(_clock(now)), 'requested_sessions': [], 'records': []}
    audit = audit_store(Path(store))
    requested, _ = _requested(protocol, audit, session_dates, local_now)
    cutoff = datetime.fromisoformat(protocol['final_cutoff'])
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError('Protocol final cutoff must have an explicit timezone')
    limit = int(protocol['request_max_attempts'])
    if limit != 2:
        raise ValueError('Collector only supports the frozen two-attempt protocol')
    budget = float(protocol['max_capture_seconds'])
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError('Invalid registered capture time budget')
    codes = list(symbols())
    if any(not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', code) for code in codes):
        raise ValueError('Unsafe protocol symbol')
    identity = capture_id or started.strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8]
    if (not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,180}', identity)
            or identity in {'.', '..'}):
        raise ValueError('Unsafe capture identifier')
    if any(item['capture_id'] == identity for item in audit.get('captures', [])):
        raise ValueError('Capture ID already exists; use a new identifier')
    transport = fetcher or _fetch
    files, records = {}, []
    budget_exhausted = False
    active = {}
    try:
        for day in requested:
            day_start = datetime.fromisoformat(day).replace(tzinfo=zone)
            params = {'interval': '5m', 'period1': int(day_start.timestamp()),
                      'period2': int((day_start + timedelta(days=1)).timestamp()),
                      'includePrePost': 'false', 'includeAdjustedClose': 'false'}
            for symbol in codes:
                url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + quote(symbol, safe='')
                attempts = []
                record = {'symbol': symbol, 'session_date': day, 'status': 'failed',
                          'csv_path': None, 'rows': 0, 'attempts': attempts,
                          'outside_target_day_rows': 0}
                for attempt in range(1, limit + 1):
                    if budget_exhausted or monotonic() - started_monotonic >= budget:
                        budget_exhausted = True
                        record.update(status='failed', reason='capture_budget_exhausted')
                        record.setdefault('received_at', None)
                        record.setdefault('on_time', None)
                        record.setdefault('after_final_cutoff', None)
                        break
                    prefix = f'raw/{day}/{symbol}/attempt-{attempt}'
                    metadata_path, raw_path = prefix + '.json', prefix + '.body'
                    requested_at = _clock(now)
                    metadata = {'request': {'url': url, 'params': dict(params),
                                            'requested_at': _iso(requested_at)}, 'response': None}
                    info = {'attempt': attempt, 'requested_at': _iso(requested_at),
                            'metadata_path': metadata_path, 'raw_path': None,
                            'http_status': None, 'response_url': None}
                    active.update(metadata_path=metadata_path, metadata=metadata, info=info)
                    try:
                        response = transport(url, dict(params))
                    except CaptureTransportError as exc:
                        received = _clock(now)
                        info.update(status='failed', reason='transport_error',
                                    transport_error=str(exc)[:1000], received_at=_iso(received))
                    else:
                        received = _clock(now)
                        if not isinstance(response, FetchResponse) or not isinstance(response.body, bytes):
                            raise TypeError('fetcher must return FetchResponse with body bytes')
                        files[raw_path] = response.body
                        info.update(raw_path=raw_path, http_status=response.status,
                                    response_url=response.url, received_at=_iso(received))
                        metadata['response'] = {'status': response.status, 'url': response.url,
                            'headers': {str(key).lower(): str(value) for key, value in response.headers.items()
                                        if str(key).lower() in _RESPONSE_HEADERS},
                            'received_at': _iso(received), 'body_path': raw_path}
                        if not 200 <= response.status < 300:
                            info.update(status='failed', reason='http_error')
                        else:
                            try:
                                normalized, row_count, outside = _normalize(response.body, symbol, day, zone)
                            except _MalformedResponse as exc:
                                info.update(status='failed', reason=str(exc))
                            else:
                                info.update(status='captured' if normalized is not None else 'empty',
                                            reason='target_day_observed' if normalized is not None else 'no_target_day_rows',
                                            rows=row_count, outside_target_day_rows=outside)
                                record['outside_target_day_rows'] = outside
                                if normalized is not None:
                                    csv_path = f'derived/{day}/{symbol.replace(".", "_")}_5m.csv'
                                    files[csv_path] = normalized
                                    record.update(csv_path=csv_path, rows=row_count)
                    attempts.append(info)
                    metadata.update(outcome=dict(info))
                    files[metadata_path] = (json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                                                     allow_nan=False, indent=2) + '\n').encode('utf-8')
                    record.update(status=info['status'], reason=info['reason'], received_at=info['received_at'],
                                  on_time=received.astimezone(zone).date().isoformat() == day,
                                  after_final_cutoff=received > cutoff)
                    if info['status'] == 'captured':
                        break
                records.append(record)
    except Exception as original:
        # Preserve response bytes already obtained, while keeping a programming
        # defect fatal.  Do not turn it into a retryable provider failure.
        diagnostic = {'type': type(original).__name__, 'classification': 'programming_error',
                      'message': 'Collection aborted; the original exception is re-raised.'}
        try:
            active_path = active.get('metadata_path')
            if active_path and active_path not in files:
                interrupted = dict(active['metadata'])
                interrupted.update(programming_error=diagnostic,
                                   outcome=dict(active['info']))
                files[active_path] = (json.dumps(interrupted, ensure_ascii=False, sort_keys=True,
                                                allow_nan=False, indent=2) + '\n').encode('utf-8')
            files['programming_error.json'] = (json.dumps(diagnostic, sort_keys=True) + '\n').encode('utf-8')
            aborted = {'status': 'failed', 'started_at': _iso(started),
                       'completed_at': _iso(_clock(now)), 'requested_sessions': requested,
                       'records': records, 'programming_error': diagnostic,
                       'trigger': 'manual' if session_dates is not None else 'automatic',
                       'provider': 'Yahoo Finance public chart', 'interval': '5m',
                       'final_cutoff': protocol['final_cutoff'],
                       'capture_budget_exhausted': budget_exhausted,
                       'max_capture_seconds': budget}
            seal_capture(Path(store), identity, aborted, files)
        except Exception as seal_error:
            raise ExceptionGroup('Collector failed and its abort evidence could not be sealed',
                                 [original, seal_error]) from original
        raise
    completed = _clock(now)
    captured_count = sum(row['status'] == 'captured' for row in records)
    status = ('success' if records and captured_count == len(records) else
              'partial_success' if captured_count else 'failed')
    payload = {'status': status,
               'started_at': _iso(started), 'completed_at': _iso(completed),
               'requested_sessions': requested, 'records': records,
               'trigger': 'manual' if session_dates is not None else 'automatic',
               'provider': 'Yahoo Finance public chart', 'interval': '5m',
               'price_fields': 'unadjusted quote OHLCV; adjclose not used',
               'final_cutoff': protocol['final_cutoff'], 'after_final_cutoff': completed > cutoff,
               'capture_budget_exhausted': budget_exhausted, 'max_capture_seconds': budget}
    return seal_capture(Path(store), identity, payload, files)
