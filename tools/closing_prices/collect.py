"""Read-only Yahoo Finance close-price probe for a 15-stock Japan portfolio.

The output is diagnostic data, NOT a verified brokerage valuation.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

JST = ZoneInfo("Asia/Tokyo")
STOCK_CODES = (
    "7270", "6247", "4689", "7942", "8016", "9432", "3861", "9434",
    "4042", "4528", "7272", "7956", "4502", "5137", "8593",
)
STOCKS = tuple(f"{code}.T" for code in STOCK_CODES)
INDEXES = ("^N225", "^TOPX")
SYMBOLS = STOCKS + INDEXES
FIELDS = ("symbol", "kind", "previous_date", "previous_close", "date", "close", "change_yen_or_points", "change_pct")


def _close_column(frame: pd.DataFrame, symbol: str) -> pd.Series:
    columns = frame.columns
    if not isinstance(columns, pd.MultiIndex) or columns.nlevels != 2:
        raise KeyError(f"unexpected column shape for {symbol}")
    for key in ((symbol, "Close"), ("Close", symbol)):
        if key in columns:
            return frame[key]
    raise KeyError(f"Close column not found: {symbol}")


def _date(value: object):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(JST)
    return stamp.date()


def inspect_prices(frame: pd.DataFrame, now: datetime, max_age_days: int = 7):
    """Return (rows, errors); fail closed for missing, stale or mismatched dates."""
    today = now.astimezone(JST).date()
    rows = []
    errors = []
    latest_dates = {}
    for symbol in SYMBOLS:
        try:
            series = _close_column(frame, symbol)
            valid = []
            for timestamp, value in series.items():
                number = pd.to_numeric(value, errors="coerce")
                if pd.notna(number) and math.isfinite(float(number)) and float(number) > 0:
                    valid.append((_date(timestamp), float(number)))
            valid.sort(key=lambda x: x[0])
            if len(valid) < 2 or valid[-1][0] == valid[-2][0]:
                raise ValueError("fewer than two distinct valid daily closes")
            (prev_date, prev), (date, close) = valid[-2:]
            if date > today or (today - date).days > max_age_days:
                raise ValueError(f"future/stale latest date: {date}")
            change = close - prev
            rows.append({
                "symbol": symbol,
                "kind": "stock" if symbol in STOCKS else "index",
                "previous_date": prev_date.isoformat(),
                "previous_close": round(prev, 4),
                "date": date.isoformat(),
                "close": round(close, 4),
                "change_yen_or_points": round(change, 4),
                "change_pct": round(change / prev * 100, 4),
            })
            latest_dates[symbol] = date
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            errors.append(f"{symbol}: {exc}")
    if len(set(latest_dates.values())) > 1:
        errors.append("Latest trading dates do not agree across symbols: " + ", ".join(f"{k}={v}" for k, v in latest_dates.items()))
    if len(rows) != len(SYMBOLS):
        errors.append(f"Incomplete coverage: {len(rows)}/{len(SYMBOLS)} symbols")
    return rows, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="results/closing_price_probe")
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(JST)
    try:
        import yfinance as yf
        frame = yf.download(
            list(SYMBOLS), period="10d", interval="1d", group_by="ticker",
            auto_adjust=False, progress=False, threads=False,
        )
        rows, errors = inspect_prices(frame, timestamp)
    except Exception as exc:
        rows, errors = [], [f"Download failed: {type(exc).__name__}: {exc}"]

    with (output / "prices.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    status = {
        "retrieved_at_jst": timestamp.isoformat(),
        "provider": "Yahoo Finance through yfinance (unofficial)",
        "source_is_official": False,
        "symbols_requested": list(SYMBOLS),
        "symbols_returned": len(rows),
        "success": not errors,
        "errors": errors,
        "note": "Unadjusted close field, auto_adjust=False. Corporate actions and published official closes require separate verification.",
    }
    (output / "status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        "# yfinance close-price diagnostic",
        f"Retrieved (JST): {timestamp.isoformat()}",
        f"Result: {'PASS' if not errors else 'FAIL'} ({len(rows)}/{len(SYMBOLS)})",
        "Source: unofficial Yahoo Finance; **not independently verified**.",
        "\n| Symbol | Date | Close | Previous close | Change (%) |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for r in rows:
        lines.append(f"| {r['symbol']} | {r['date']} | {r['close']} | {r['previous_close']} | {r['change_pct']:+.4f}% |")
    if errors:
        lines.extend(["\n## Errors", *[f"- {e}" for e in errors]])
    report = "\n".join(lines) + "\n"
    (output / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
