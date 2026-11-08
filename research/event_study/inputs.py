"""Offline, loss-preserving loading of saved five-minute research observations.

``load_inputs`` never imports a trading engine, downloads prices, fills missing
bars, filters by an earlier backtest's eligibility, or extracts ZIP members.
Watchlists use ``コード`` (or ``code`` / ``symbol``) and optionally ``候補名``
(or ``name``). Codes remain strings, including their exchange suffix.

A directory is searched recursively, or a ZIP is inspected, for the exact
basename ``<code with dots replaced by underscores>_5m.csv``. Multiple matching
members are ambiguous, even if their contents happen to match. Each CSV needs
``Datetime,Open,High,Low,Close,Volume``; extra columns are retained. Every
Datetime must explicitly include a timezone. The returned index is converted
to Asia/Tokyo, without sorting, deduplication, resampling, or row removal.
OHLCV (and optional Adj Close) are numeric; unparseable numeric cells become
NaN while their rows remain available for the event engine's quality audit.

Return ``(frames, input_records, source_info)``. ``frames`` contains only
successfully parsed, nonempty symbols. Every requested symbol has one record,
whose ``status`` is ``loaded``, ``missing``, or ``failed``. Missing files,
ambiguous names, malformed CSVs, absent columns, naive/invalid timestamps, and
expected-hash mismatches affect that symbol, not other symbols. Global source,
watchlist, and manifest format errors raise an exception to the caller.

Hashes cover the exact original CSV/watchlist/ZIP/manifest bytes. A supplied
JSON manifest is returned as provenance; its optional ``input_hashes`` mapping
(also accepted as a JSON-encoded mapping, as in the saved portfolio artifact)
is checked by code or CSV basename. A code absent from that mapping is marked
``not_in_manifest`` rather than falsely certified. Duplicate/off-grid/invalid
price rows in a successfully parsed CSV are deliberately left for the core.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile
import zlib

import pandas as pd


REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


class InvalidCSVError(ValueError):
    """Expected saved-data validation failure, distinct from programming errors."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _watchlist(raw: bytes) -> list[tuple[str, str]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    fields = reader.fieldnames or []
    code_column = next((key for key in ("コード", "code", "symbol") if key in fields), None)
    if code_column is None:
        raise ValueError("Watchlist requires a コード, code, or symbol column")
    name_column = next((key for key in ("候補名", "name") if key in fields), None)
    result = []
    seen = set()
    for row_number, row in enumerate(reader, start=2):
        code = (row.get(code_column) or "").strip()
        if not code:
            raise ValueError(f"Empty watchlist code at row {row_number}")
        if code in seen:
            raise ValueError(f"Duplicate watchlist code: {code}")
        seen.add(code)
        name = (row.get(name_column) or "").strip() if name_column else ""
        result.append((code, name or code))
    if not result:
        raise ValueError("Watchlist is empty")
    return result


def _manifest(path: Path | None) -> tuple[dict, dict, str]:
    if path is None:
        return {}, {}, ""
    raw = path.read_bytes()
    metadata = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(metadata, dict):
        raise ValueError("Source manifest must be a JSON object")
    expected = metadata.get("input_hashes", {})
    if isinstance(expected, str):
        expected = json.loads(expected)
    if not isinstance(expected, dict):
        raise ValueError("Manifest input_hashes must be a mapping")
    for code, value in expected.items():
        if not isinstance(code, str) or not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("Manifest contains an invalid input SHA256")
    return metadata, expected, _sha256(raw)


def _frame(raw: bytes) -> pd.DataFrame:
    # Strings preserve malformed cells until the explicit numeric conversion.
    header = next(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")), [])
    if len(header) != len(set(header)):
        raise InvalidCSVError("CSV contains duplicate column names")
    frame = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig", dtype=str, keep_default_na=False)
    missing = [column for column in ("Datetime", *REQUIRED_COLUMNS) if column not in frame.columns]
    if missing:
        raise InvalidCSVError("Missing columns: " + ", ".join(missing))
    if frame.empty:
        raise InvalidCSVError("CSV contains no observations")
    stamps = []
    for value in frame.pop("Datetime"):
        try:
            stamp = pd.Timestamp(value)
        except (ValueError, OverflowError) as exc:
            raise InvalidCSVError(f"Invalid Datetime: {value}") from exc
        if pd.isna(stamp) or stamp.tzinfo is None or stamp.utcoffset() is None:
            raise InvalidCSVError("Datetime must be valid and explicitly timezone-aware")
        stamps.append(stamp)
    try:
        frame.index = pd.DatetimeIndex(pd.to_datetime(stamps, utc=True)).tz_convert("Asia/Tokyo")
    except (ValueError, OverflowError) as exc:
        raise InvalidCSVError("Datetime cannot be represented as a timezone-aware index") from exc
    frame.index.name = "Datetime"
    for column in (*REQUIRED_COLUMNS, "Adj Close"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def load_inputs(
    prices: Path,
    watchlist: Path,
    source_manifest: Path | None = None,
) -> tuple[dict[str, pd.DataFrame], list[dict], dict]:
    """Read saved observations only; see the module docstring for the contract."""
    prices = Path(prices)
    watchlist = Path(watchlist)
    source_manifest = Path(source_manifest) if source_manifest is not None else None
    watchlist_raw = watchlist.read_bytes()
    requested = _watchlist(watchlist_raw)
    metadata, expected, manifest_sha = _manifest(source_manifest)
    source_info = {
        "mode": "offline",
        "prices_path": str(prices),
        "prices_kind": "directory" if prices.is_dir() else "zip",
        "prices_sha256": "",
        "watchlist_path": str(watchlist),
        "watchlist_sha256": _sha256(watchlist_raw),
        "source_manifest_path": str(source_manifest) if source_manifest is not None else "",
        "source_manifest_sha256": manifest_sha,
        "source_manifest": metadata,
        "requested_symbols": len(requested),
    }
    archive = None
    members: dict[str, list] = {}
    if prices.is_dir():
        for path in sorted(prices.rglob("*_5m.csv")):
            if path.is_file():
                members.setdefault(path.name, []).append(path)
    else:
        # Hash and open the same bytes; no member is written to the filesystem.
        zip_raw = prices.read_bytes()
        source_info["prices_sha256"] = _sha256(zip_raw)
        archive = zipfile.ZipFile(io.BytesIO(zip_raw))
        for info in archive.infolist():
            name = PurePosixPath(info.filename).name
            if not info.is_dir() and name.endswith("_5m.csv"):
                members.setdefault(name, []).append(info)

    frames = {}
    records = []
    try:
        for code, name in requested:
            filename = code.replace(".", "_") + "_5m.csv"
            matches = members.get(filename, [])
            expected_sha = expected.get(code, expected.get(filename, "")).lower()
            record = {
                "code": code, "name": name, "status": "missing", "reason": "missing_csv",
                "path": "", "sha256": "", "expected_sha256": expected_sha,
                "hash_status": "not_in_manifest" if expected else "not_supplied",
                "bytes": 0, "rows": 0, "actual_start": "", "actual_end": "",
                "timezone": "Asia/Tokyo", "duplicate_timestamps": 0,
                "timestamps_monotonic": "", "numeric_missing_values": 0,
            }
            records.append(record)
            if not matches:
                continue
            if len(matches) != 1:
                record.update(status="failed", reason=f"ambiguous_csv: {len(matches)} matching members")
                continue
            member = matches[0]
            record["path"] = f"{prices}!{member.filename}" if archive else str(member)
            if archive is not None and member.flag_bits & 1:
                record.update(status="failed", reason="encrypted_zip_member")
                continue
            try:
                raw = archive.read(member) if archive else member.read_bytes()
            except (OSError, zipfile.BadZipFile, zlib.error) as exc:
                record.update(status="failed", reason=f"unreadable_csv: {type(exc).__name__}: {exc}")
                continue
            record.update(sha256=_sha256(raw), bytes=len(raw))
            if expected_sha:
                record["hash_status"] = "matched" if record["sha256"] == expected_sha else "mismatch"
                if record["hash_status"] == "mismatch":
                    record.update(status="failed", reason="input_sha256_mismatch")
                    continue
            try:
                frame = _frame(raw)
            except (UnicodeError, InvalidCSVError, csv.Error, pd.errors.ParserError,
                    pd.errors.EmptyDataError) as exc:
                record.update(status="failed", reason=f"invalid_csv: {type(exc).__name__}: {exc}")
                continue
            frames[code] = frame
            record.update(
                status="loaded", reason="", rows=len(frame),
                actual_start=frame.index.min().isoformat(), actual_end=frame.index.max().isoformat(),
                duplicate_timestamps=int(frame.index.duplicated().sum()),
                timestamps_monotonic=bool(frame.index.is_monotonic_increasing),
                numeric_missing_values=int(frame[list(REQUIRED_COLUMNS)].isna().sum().sum()),
            )
    finally:
        if archive is not None:
            archive.close()
    source_info.update(
        loaded_symbols=len(frames),
        missing_symbols=sum(row["status"] == "missing" for row in records),
        failed_symbols=sum(row["status"] == "failed" for row in records),
    )
    return frames, records, source_info
