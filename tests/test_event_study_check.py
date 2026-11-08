"""Independent persisted-artifact checks using tiny, offline CLI inputs."""

from contextlib import redirect_stdout
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research.event_study.__main__ import main
from research.event_study.check import check


class EventStudyArtifactCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prices = self.root / "prices"
        self.prices.mkdir()
        self.output = self.root / "output"
        self.watchlist = self.root / "watchlist.csv"
        self.watchlist.write_text("code,name\nA.T,Synthetic research input\n", encoding="utf-8")
        timezone = ZoneInfo("Asia/Tokyo")
        with (self.prices / "A_T_5m.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["Datetime", "Open", "High", "Low", "Close", "Volume"])
            for hour, minute, slots in ((9, 0, 30), (12, 30, 35)):
                start = datetime(2026, 10, 1, hour, minute, tzinfo=timezone)
                for i in range(slots):
                    writer.writerow([str(start + timedelta(minutes=5 * i)), 1000, 1000, 1000, 1000, 100])
        # The fixture exercises the public CLI path; it neither reads a live
        # provider nor uses a calculator to generate its expected return values.
        with patch("research.event_study.__main__._git_info", return_value={
                "commit": "synthetic-test", "branch": "synthetic-test", "dirty": False}), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--prices", str(self.prices), "--watchlist", str(self.watchlist),
                                   "--output", str(self.output)]), 0)

    def test_generated_saved_fixture_passes_independent_artifact_checker(self):
        with redirect_stdout(io.StringIO()) as captured:
            manifest = check(self.output)
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["input_counts"]["requested_symbols"], 1)
        self.assertEqual(manifest["input_counts"]["usable_symbols"], 1)
        # All 65 flat-price bars are time-of-day trials at three horizons.
        # Session boundaries leave 61 + 57 + 51 exactly measurable outcomes.
        self.assertEqual(manifest["record_counts"]["observations"], 195)
        self.assertEqual(manifest["input_counts"]["measurable_observations"], 169)
        self.assertIn("Artifacts verified", captured.getvalue())
        self.assertTrue((self.output / "report.html").is_file())

    def test_byte_level_artifact_change_is_rejected_by_manifest_hash(self):
        path = self.output / "observations.csv"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "Artifact differs from manifest: observations.csv"):
            check(self.output)

    def test_false_net_pnl_is_rejected_even_with_recomputed_manifest_hash(self):
        path = self.output / "observations.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = reader.fieldnames
            rows = list(reader)
        victim = next(row for row in rows if row["status"] == "valid")
        self.assertEqual(float(victim["gross_pnl_yen"]), 0.0)
        self.assertEqual(float(victim["net1_pnl_yen"]), -400.0)
        victim["net1_pnl_yen"] = "-277.0"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw = path.read_bytes()
        manifest["artifacts"][path.name] = {
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "event arithmetic differs: net1_pnl_yen"):
            check(self.output)


if __name__ == "__main__":
    unittest.main()
