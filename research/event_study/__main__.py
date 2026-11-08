"""Run the frozen descriptive event study against saved observations only."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import core
from .inputs import load_inputs
from .report import DATASETS, LIMITATIONS, json_safe, write_report


PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _git_info() -> dict:
    def read(*args):
        result = subprocess.run(
            ['git', '-C', str(ROOT), *args], capture_output=True, text=True,
            check=False, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    try:
        status = read('status', '--porcelain')
        return {'commit': read('rev-parse', 'HEAD'),
                'branch': read('branch', '--show-current'),
                'dirty': bool(status) if status is not None else None}
    except (OSError, subprocess.TimeoutExpired):
        # Running an exported source tree without Git is supported, but not certified.
        return {'commit': None, 'branch': None, 'dirty': None}


def _runtime() -> dict:
    versions = {}
    for name in ('pandas', 'numpy'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {'python': sys.version, 'platform': platform.platform(), 'packages': versions}


def _period(frames) -> dict:
    starts = [frame.index.min() for frame in frames.values() if not frame.empty]
    ends = [frame.index.max() for frame in frames.values() if not frame.empty]
    return {'first_bar': min(starts).isoformat() if starts else None,
            'last_bar': max(ends).isoformat() if ends else None,
            'rows': sum(len(frame) for frame in frames.values())}


def _source_fingerprint(prices: Path, source_info: dict):
    if prices.is_file():
        return source_info.get('prices_sha256') or _sha256(prices)
    # Include ambiguous and unrequested matching members as well as parsed files.
    hashes = {str(path.relative_to(prices)): _sha256(path)
              for path in sorted(prices.rglob('*_5m.csv')) if path.is_file()}
    payload = json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _validate_study(study):
    if not isinstance(study, dict):
        raise TypeError('Event core must return a mapping')
    for key in DATASETS:
        rows = study[key]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise TypeError(f'Event core {key} must be a list of mappings')


def _research_status(study, counts):
    observed = sum(row.get('status') == 'valid' for row in study['observations'])
    if not observed:
        return 'failed', 'no_measurable_temporal_outcomes'
    incomplete = any(
        row.get('missing_or_ambiguous_regular_bars', 0)
        or row.get('invalid_regular_bars', 0)
        or row.get('zero_volume_regular_bars', 0)
        for row in study['quality'])
    unavailable = counts['missing_symbols'] + counts['failed_symbols']
    reasons = []
    if unavailable:
        reasons.append('some_inputs_unavailable')
    if incomplete:
        reasons.append('incomplete_research_coverage')
    return ('partial_success', ';'.join(reasons)) if reasons else ('success', 'loaded_inputs_processed')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='保存済み5分足の記述的イベント研究（注文・ダウンロード機能なし）')
    parser.add_argument('--prices', type=Path, default=PACKAGE / 'saved_prices.zip',
                        help='保存済み *_5m.csv のディレクトリまたはZIP')
    parser.add_argument('--watchlist', type=Path, default=ROOT / 'watchlist.csv',
                        help='対象コードのCSV（既定: research50 watchlist.csv）')
    parser.add_argument('--output', type=Path, default=ROOT / 'results' / 'event_study',
                        help='HTML・CSV・manifest.jsonの出力先')
    parser.add_argument('--source-manifest', type=Path, default=None,
                        help='保存データの来歴と任意のinput_hashesを持つJSON')
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    started = datetime.now(timezone.utc)
    timer = time.perf_counter()
    # A failed rerun must not leave an earlier successful report masquerading as new.
    args.output.mkdir(parents=True, exist_ok=True)
    for name in [*DATASETS.values(), 'input_audit.csv', 'report.html', 'manifest.json']:
        (args.output / name).unlink(missing_ok=True)
    config_path = PACKAGE / 'config.json'
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes.decode('utf-8'))
    if not isinstance(config, dict):
        raise ValueError('Frozen config must be a JSON object')
    code_hashes = {
        str(path.relative_to(ROOT)): _sha256(path)
        for path in sorted(PACKAGE.glob('*.py'))
    }
    git_info = _git_info()

    source_manifest = args.source_manifest
    # Never attach the bundled provenance to unrelated custom prices by default.
    if source_manifest is None and args.prices.resolve() == (PACKAGE / 'saved_prices.zip').resolve():
        source_manifest = PACKAGE / 'saved_source.json'
    frames, input_records, source_info = load_inputs(
        args.prices, args.watchlist, source_manifest)
    if frames:
        # Programming/configuration errors propagate and fail CI; no success fallback.
        study = core.run_study(frames, config)
    else:
        study = {key: [] for key in DATASETS}
    _validate_study(study)
    calculation_seconds = time.perf_counter() - timer

    counts = {
        'requested_symbols': len(input_records), 'usable_symbols': len(frames),
        'missing_symbols': sum(row.get('status') == 'missing' for row in input_records),
        'failed_symbols': sum(row.get('status') == 'failed' for row in input_records),
    }
    status, reason = _research_status(study, counts) if frames else ('failed', 'no_loaded_inputs')
    valid_by_symbol = {}
    for row in study['observations']:
        if row.get('status') == 'valid':
            symbol = row['symbol']
            valid_by_symbol[symbol] = valid_by_symbol.get(symbol, 0) + 1
    counts['research_usable_symbols'] = len(valid_by_symbol)
    counts['measurable_observations'] = sum(valid_by_symbol.values())
    input_records = [{**row,
                      'research_status': ('usable' if row['code'] in valid_by_symbol
                                          else 'insufficient_outcomes' if row['status'] == 'loaded'
                                          else 'input_unavailable'),
                      'measurable_observations': valid_by_symbol.get(row['code'], 0)}
                     for row in input_records]
    ci = {key: os.environ[key] for key in
          ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA')
          if key in os.environ}
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    prices_sha = _source_fingerprint(args.prices, source_info)
    manifest = {
        'schema_version': 1,
        'study_id': started.strftime('%Y%m%dT%H%M%S.%fZ'),
        'status': status,
        'status_reason': reason,
        'study_kind': 'retrospective_descriptive_event_study_not_trade_execution',
        'started_at': started.isoformat(),
        'calculation_seconds': calculation_seconds, 'runtime_seconds': None,
        'input_counts': counts, 'input_period': _period(frames),
        'record_counts': {key: len(study.get(key, [])) for key in DATASETS},
        'prices_sha256': prices_sha,
        'prices_hash_method': 'exact_zip_bytes' if args.prices.is_file() else
            'sha256_of_sorted_relative_csv_path_to_sha256_json',
        'watchlist_sha256': source_info.get('watchlist_sha256'),
        'config_sha256': config_sha, 'config': config,
        'code_sha256': code_hashes, 'git': git_info, 'runtime': _runtime(), 'ci': ci,
        'source_info': source_info, 'limitations': LIMITATIONS,
        'artifacts': {},
    }
    write_report(args.output, study, input_records, manifest)
    manifest['artifacts'] = {
        name: {'sha256': _sha256(args.output / name), 'bytes': (args.output / name).stat().st_size}
        for name in [*DATASETS.values(), 'input_audit.csv', 'report.html']
    }
    manifest['runtime_seconds'] = time.perf_counter() - timer
    # The manifest is written last and excludes its own hash to avoid a cycle.
    (args.output / 'manifest.json').write_text(
        json.dumps(json_safe(manifest), ensure_ascii=False, indent=2, sort_keys=True,
                   allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({
        'status': status, 'input_counts': counts,
        'observations': len(study.get('observations', [])),
        'runtime_seconds': round(manifest['runtime_seconds'], 3),
        'report': str(args.output / 'report.html'),
    }, ensure_ascii=False))
    return 1 if status == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
