"""Synthetic offline-input checks, independent of provider availability and PnL."""

import csv
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import pandas as pd

from research.event_study.inputs import load_inputs


HEADER = "Datetime,Open,High,Low,Close,Volume\n"
GOOD = (HEADER + "2026-10-01 09:00:00+09:00,100,102,99,101,500\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class EventStudyInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prices = self.root / "prices"
        self.prices.mkdir()
        self.watchlist = self.root / "watchlist.csv"

    def watch(self, codes):
        with self.watchlist.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["コード", "候補名"])
            writer.writerows((code, "研究候補 " + code) for code in codes)

    def price(self, code, raw=GOOD):
        path = self.prices / (code.replace(".", "_") + "_5m.csv")
        path.write_bytes(raw)
        return path

    def manifest(self, value):
        path = self.root / "source.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_directory_and_zip_replay_identical_prices_and_exact_byte_hashes(self):
        self.watch(["219A.T", "9999.T"])
        alpha = ("\ufeffDatetime,Open,High,Low,Close,Adj Close,Volume\r\n"
                 "2026-10-01T00:05:00Z,100,102,99,101,101,500\r\n"
                 "2026-10-01T00:10:00Z,101,103,100,102,102,600\r\n").encode()
        self.price("219A.T", alpha)
        self.price("9999.T")
        metadata = {"source": {"retrieved_at": "2026-10-02T08:00:00+09:00"},
                    "input_hashes": {"219A.T": digest(alpha), "9999.T": digest(GOOD)},
                    "old_eligibility": {"219A.T": "データ不足"}}
        manifest = self.manifest(metadata)
        archive = self.root / "snapshot.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
            stream.writestr("inside/219A_T_5m.csv", alpha)
            stream.writestr("inside/9999_T_5m.csv", GOOD)
        directory_frames, directory_rows, directory_info = load_inputs(self.prices, self.watchlist, manifest)
        zip_frames, zip_rows, zip_info = load_inputs(archive, self.watchlist, manifest)
        self.assertEqual(set(directory_frames), {"219A.T", "9999.T"})
        self.assertEqual(set(zip_frames), set(directory_frames))
        for code in directory_frames:
            pd.testing.assert_frame_equal(directory_frames[code], zip_frames[code], check_exact=True)
        self.assertEqual(directory_frames["219A.T"].index[0], pd.Timestamp("2026-10-01 09:05+09:00"))
        self.assertEqual(str(zip_frames["219A.T"].index.tz), "Asia/Tokyo")
        self.assertEqual(zip_frames["219A.T"].iloc[-1].Close, 102)
        for rows in (directory_rows, zip_rows):
            self.assertTrue(all(row["status"] == "loaded" and row["hash_status"] == "matched" for row in rows))
            self.assertEqual(rows[0]["sha256"], digest(alpha))
            self.assertEqual(rows[0]["bytes"], len(alpha))
        self.assertEqual(directory_info["watchlist_sha256"], digest(self.watchlist.read_bytes()))
        self.assertEqual(zip_info["prices_sha256"], digest(archive.read_bytes()))
        self.assertEqual(zip_info["source_manifest_sha256"], digest(manifest.read_bytes()))
        self.assertEqual(zip_info["source_manifest"], metadata)
        self.assertEqual(zip_info["loaded_symbols"], 2)
        self.assertEqual(zip_info["failed_symbols"], 0)
        self.assertFalse((self.root / "inside").exists())

    def test_bad_symbol_inputs_do_not_discard_a_valid_symbol(self):
        cases = {
            "BROKEN.T": b'Datetime,Open,High,Low,Close,Volume\n"unterminated',
            "COLUMNS.T": b"Datetime,Open\n2026-10-01 09:00+09:00,100\n",
            "NAIVE.T": (HEADER + "2026-10-01 09:00,100,102,99,101,500\n").encode(),
            "DATE.T": (HEADER + "not-a-date,100,102,99,101,500\n").encode(),
            "HASH.T": GOOD + b"\n",
            "DUPCOL.T": b"Datetime,Open,High,Low,Close,Volume,Open\n2026-10-01 09:00+09:00,100,102,99,101,500,999\n",
            "EMPTY.T": HEADER.encode(),
            "ENCODING.T": b"\xff\xfeinvalid-utf8",
        }
        self.watch(["GOOD.T", "MISSING.T", *cases])
        self.price("GOOD.T")
        for code, raw in cases.items():
            self.price(code, raw)
        manifest = self.manifest({"input_hashes": {"GOOD.T": digest(GOOD), "HASH.T": digest(GOOD)}})
        frames, records, info = load_inputs(self.prices, self.watchlist, manifest)
        self.assertEqual(set(frames), {"GOOD.T"})
        self.assertEqual(frames["GOOD.T"].iloc[0].Close, 101)
        rows = {row["code"]: row for row in records}
        self.assertEqual(rows["GOOD.T"]["status"], "loaded")
        self.assertEqual(rows["MISSING.T"]["status"], "missing")
        for code in cases:
            with self.subTest(code=code):
                self.assertEqual(rows[code]["status"], "failed")
                self.assertTrue(rows[code]["reason"])
        self.assertEqual(rows["HASH.T"]["hash_status"], "mismatch")
        self.assertEqual(rows["HASH.T"]["sha256"], digest(cases["HASH.T"]))
        self.assertEqual(info["loaded_symbols"], 1)
        self.assertEqual(info["missing_symbols"], 1)
        self.assertEqual(info["failed_symbols"], len(cases))

    def test_duplicate_off_grid_invalid_and_missing_price_rows_are_not_repaired(self):
        self.watch(["A.T"])
        raw = (HEADER +
               "2026-10-01 09:10:00+09:00,100,99,98,101,10\n"  # High below Close.
               "2026-10-01 09:00:00+09:00,unknown,102,99,101,0\n"
               "2026-10-01 09:00:00+09:00,104,105,103,104,20\n"
               "2026-10-01 12:01:00+09:00,100,101,99,100,30\n").encode()
        path = self.price("A.T", raw)
        frames, records, _ = load_inputs(self.prices, self.watchlist)
        frame = frames["A.T"]
        expected_times = ["09:10", "09:00", "09:00", "12:01"]
        self.assertEqual([stamp.strftime("%H:%M") for stamp in frame.index], expected_times)
        self.assertEqual(len(frame), 4)
        self.assertNotIn(pd.Timestamp("2026-10-01 09:05+09:00"), frame.index)
        self.assertEqual(frame.iloc[0].High, 99)
        self.assertTrue(math.isnan(frame.iloc[1].Open))
        self.assertEqual(frame.iloc[1].Volume, 0)
        self.assertEqual(frame.iloc[2].Open, 104)
        self.assertEqual(records[0]["duplicate_timestamps"], 1)
        self.assertFalse(records[0]["timestamps_monotonic"])
        self.assertEqual(records[0]["numeric_missing_values"], 1)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(records[0]["sha256"], digest(raw))

    def test_ambiguous_filenames_fail_in_both_directory_and_zip(self):
        self.watch(["A.T", "B.T"])
        self.price("B.T")
        for folder in ("one", "two"):
            (self.prices / folder).mkdir()
            (self.prices / folder / "A_T_5m.csv").write_bytes(GOOD)
        archive = self.root / "ambiguous.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("one/A_T_5m.csv", GOOD)
            stream.writestr("two/A_T_5m.csv", GOOD)
            stream.writestr("B_T_5m.csv", GOOD)
        for source in (self.prices, archive):
            with self.subTest(source=source.name):
                frames, records, _ = load_inputs(source, self.watchlist)
                self.assertEqual(set(frames), {"B.T"})
                self.assertEqual(records[0]["status"], "failed")
                self.assertIn("ambiguous", records[0]["reason"])
                self.assertEqual(records[1]["status"], "loaded")

    def test_original_json_encoded_hash_mapping_and_filename_keys_are_supported(self):
        self.watch(["A.T"])
        self.price("A.T")
        for expected in ({"A.T": digest(GOOD)}, json.dumps({"A_T_5m.csv": digest(GOOD)})):
            with self.subTest(mapping=expected):
                manifest = self.manifest({"input_hashes": expected})
                frames, records, _ = load_inputs(self.prices, self.watchlist, manifest)
                self.assertEqual(set(frames), {"A.T"})
                self.assertEqual(records[0]["hash_status"], "matched")
        manifest = self.manifest({"input_hashes": {"OTHER.T": digest(GOOD)}})
        frames, records, _ = load_inputs(self.prices, self.watchlist, manifest)
        self.assertEqual(set(frames), {"A.T"})
        self.assertEqual(records[0]["hash_status"], "not_in_manifest")

    def test_corrupted_zip_member_does_not_hide_other_valid_members(self):
        self.watch(["A.T", "B.T"])
        archive = self.root / "corrupt.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as stream:
            stream.writestr("A_T_5m.csv", GOOD)
            stream.writestr("B_T_5m.csv", GOOD)
        with zipfile.ZipFile(archive) as stream:
            info = stream.getinfo("A_T_5m.csv")
        raw = bytearray(archive.read_bytes())
        payload_offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        raw[payload_offset + len(GOOD) - 3] ^= 1  # Valid ZIP directory, invalid A CRC.
        archive.write_bytes(raw)
        frames, records, _ = load_inputs(archive, self.watchlist)
        self.assertEqual(set(frames), {"B.T"})
        self.assertEqual(records[0]["status"], "failed")
        self.assertIn("BadZipFile", records[0]["reason"])
        self.assertEqual(records[1]["status"], "loaded")

    def test_invalid_global_watchlist_and_manifest_raise(self):
        for raw in ("code,name\nA.T,A\nA.T,again\n", "code,name\n", "wrong,name\nA.T,A\n", "code,name\n,A\n"):
            with self.subTest(watchlist=raw):
                self.watchlist.write_text(raw)
                with self.assertRaises(ValueError):
                    load_inputs(self.prices, self.watchlist)
        self.watch(["A.T"])
        for metadata in ([], {"input_hashes": []}, {"input_hashes": {"A.T": "invalid-sha"}}):
            with self.subTest(manifest=metadata):
                with self.assertRaises(ValueError):
                    load_inputs(self.prices, self.watchlist, self.manifest(metadata))

    def test_unexpected_programming_errors_propagate_instead_of_becoming_data_failures(self):
        self.watch(["A.T"])
        self.price("A.T")
        for error in (RuntimeError("internal failure"), AssertionError("invariant failed"), ValueError("unexpected internal value")):
            with self.subTest(error=type(error).__name__):
                with patch("research.event_study.inputs._frame", side_effect=error):
                    with self.assertRaises(type(error)):
                        load_inputs(self.prices, self.watchlist)


if __name__ == "__main__":
    unittest.main()
