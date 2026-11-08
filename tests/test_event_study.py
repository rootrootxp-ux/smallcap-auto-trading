"""Synthetic checks for the independent short-event study.

These are research-return checks, not brokerage execution simulations.  Each
fixture contains only explicitly supplied bars; missing prices are never filled.
"""
import json
import unittest

import pandas as pd

from research.event_study.core import feature_rows, load_config, run_study


TZ = "Asia/Tokyo"
EVENTS = (
    "ma3_cross_down", "vwap_cross_down", "relative_volume_gt_1_5",
    "prior6_low_break", "time_of_day",
)


def day_grid(date="2026-10-01"):
    return pd.date_range(f"{date} 09:00", f"{date} 11:25", freq="5min", tz=TZ).append(
        pd.date_range(f"{date} 12:30", f"{date} 15:20", freq="5min", tz=TZ))


def bars(date="2026-10-01"):
    index = day_grid(date)
    return pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                         "Close": 100.0, "Volume": 100.0}, index=index)


def set_close(frame, index, close, volume=None):
    """Keep a physically possible OHLC bar when changing its signal close."""
    t = frame.index[index]
    frame.loc[t, "Close"] = close
    frame.loc[t, "High"] = max(float(frame.loc[t, "Open"]), close) + 1
    frame.loc[t, "Low"] = min(float(frame.loc[t, "Open"]), close) - 1
    if volume is not None:
        frame.loc[t, "Volume"] = volume


def event_bars(date="2026-10-01"):
    frame = bars(date)
    for i, close in enumerate([100, 100, 100, 102, 103, 104, 90, 98, 97]):
        set_close(frame, i, close)
    frame.loc[frame.index[6], "Volume"] = 200.0
    frame.loc[frame.index[7], "Open"] = 100.0
    frame.loc[frame.index[8], "Open"] = 95.0
    frame.loc[frame.index[8], "Low"] = 94.0
    return frame


def at(records, t):
    wanted = pd.Timestamp(t)
    return next(row for row in records if pd.Timestamp(row["signal_time"]) == wanted)


def observation(result, signal_time, event="ma3_cross_down", horizon=5, symbol="A"):
    return next(row for row in result["observations"]
                if row["symbol"] == symbol and row["event_type"] == event
                and row["horizon_minutes"] == horizon
                and pd.Timestamp(row["signal_time"]) == pd.Timestamp(signal_time))


class EventStudyTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def study(self, frame):
        return run_study({"A": frame}, self.config)

    def test_signal_uses_next_open_and_reference_cost_arithmetic(self):
        frame = event_bars()
        result = self.study(frame)
        row = observation(result, frame.index[6])
        self.assertEqual(row["status"], "valid")
        self.assertEqual(pd.Timestamp(row["signal_confirmed_at"]), frame.index[7])
        self.assertEqual(pd.Timestamp(row["entry_time"]), frame.index[7])
        self.assertEqual(pd.Timestamp(row["exit_time"]), frame.index[8])
        self.assertEqual(row["entry_open"], 100.0)
        self.assertEqual(row["exit_open"], 95.0)
        self.assertAlmostEqual(row["price_return_pct"], -5.0)
        self.assertAlmostEqual(row["short_gross_return_pct"], 5.0)
        self.assertAlmostEqual(row["gross_pnl_yen"], 500.0)
        self.assertAlmostEqual(row["net1_pnl_yen"], 100.0)
        self.assertAlmostEqual(row["net2_pnl_yen"], -100.0)
        self.assertAlmostEqual(row["net1_return_pct"], 1.0)
        self.assertAlmostEqual(row["net2_return_pct"], -1.0)

    def test_future_prices_and_volume_cannot_rewrite_confirmed_features(self):
        frame = event_bars()
        before, _ = feature_rows(frame, self.config)
        changed = frame.copy()
        future = changed.index > frame.index[6]
        changed.loc[future, ["Open", "High", "Low", "Close"]] *= 7
        changed.loc[future, "Volume"] *= 19
        after, _ = feature_rows(changed, self.config)
        cutoff = frame.index[6]
        self.assertEqual([r for r in before if pd.Timestamp(r["signal_time"]) <= cutoff],
                         [r for r in after if pd.Timestamp(r["signal_time"]) <= cutoff])
        self.assertNotEqual(observation(self.study(frame), cutoff)["entry_open"],
                            observation(self.study(changed), cutoff)["entry_open"])

    def test_prior_windows_exclude_current_bar_and_thresholds_are_strict(self):
        frame = bars()
        set_close(frame, 6, 99.0, 150.0)
        rows, _ = feature_rows(frame, self.config)
        row = at(rows, frame.index[6])
        self.assertTrue(row["relative_volume_gt_1_5_eligible"])
        self.assertTrue(row["prior6_low_break_eligible"])
        self.assertFalse(row["relative_volume_gt_1_5"])
        self.assertFalse(row["prior6_low_break"])
        set_close(frame, 6, 98.99, 150.01)
        rows, _ = feature_rows(frame, self.config)
        row = at(rows, frame.index[6])
        self.assertTrue(row["relative_volume_gt_1_5"])
        self.assertTrue(row["prior6_low_break"])

    def test_equal_ma_and_vwap_are_not_downward_crosses(self):
        frame = bars()
        rows, _ = feature_rows(frame, self.config)
        flat = at(rows, frame.index[6])
        self.assertTrue(flat["ma3_cross_down_eligible"])
        self.assertTrue(flat["vwap_cross_down_eligible"])
        self.assertFalse(flat["ma3_cross_down"])
        self.assertFalse(flat["vwap_cross_down"])
        set_close(frame, 6, 98.0)
        rows, _ = feature_rows(frame, self.config)
        down = at(rows, frame.index[6])
        self.assertTrue(down["ma3_cross_down"])
        self.assertTrue(down["vwap_cross_down"])

    def test_indicator_windows_reset_each_day(self):
        first = bars("2026-10-01") * 10
        second = bars("2026-10-02")
        rows, _ = feature_rows(pd.concat([first, second]), self.config)
        for i in range(3):
            self.assertFalse(at(rows, second.index[i])["ma3_cross_down_eligible"])
        self.assertTrue(at(rows, second.index[3])["ma3_cross_down_eligible"])
        for i in range(5):
            self.assertFalse(at(rows, second.index[i])["relative_volume_gt_1_5_eligible"])
        for i in range(6):
            self.assertFalse(at(rows, second.index[i])["prior6_low_break_eligible"])
        self.assertFalse(at(rows, second.index[0])["vwap_cross_down_eligible"])
        self.assertFalse(any(at(rows, t)["vwap_cross_down"] for t in second.index))

    def test_missing_past_slot_disables_windows_until_the_gap_rolls_out(self):
        frame = bars()
        missing = frame.index[3]
        rows, _ = feature_rows(frame.drop(missing), self.config)
        self.assertEqual(len(rows), 65)
        self.assertFalse(at(rows, frame.index[6])["prior6_low_break_eligible"])
        self.assertFalse(at(rows, frame.index[8])["relative_volume_gt_1_5_eligible"])
        self.assertTrue(at(rows, frame.index[9])["relative_volume_gt_1_5_eligible"])
        self.assertTrue(at(rows, frame.index[10])["prior6_low_break_eligible"])
        self.assertFalse(at(rows, frame.index[-1])["vwap_cross_down_eligible"])

    def test_missing_opening_bar_leaves_vwap_unknown_while_local_ma_recovers(self):
        frame = bars()
        rows, quality = feature_rows(frame.iloc[1:], self.config)
        self.assertFalse(quality[0]["opening_0900_observed"])
        self.assertFalse(at(rows, frame.index[3])["ma3_cross_down_eligible"])
        self.assertTrue(at(rows, frame.index[4])["ma3_cross_down_eligible"])
        self.assertTrue(all(not row["vwap_prefix_complete"] for row in rows))
        self.assertTrue(all(not row["vwap_cross_down_eligible"] for row in rows))

    def test_scheduled_indicator_windows_continue_across_lunch_without_lunch_outcomes(self):
        frame = bars()
        set_close(frame, 28, 102.0)
        set_close(frame, 29, 104.0)
        set_close(frame, 30, 90.0, 160.0)
        rows, _ = feature_rows(frame, self.config)
        first_afternoon = at(rows, frame.index[30])
        self.assertTrue(first_afternoon["ma3_cross_down"])
        self.assertTrue(first_afternoon["relative_volume_gt_1_5"])
        self.assertAlmostEqual(first_afternoon["ma3"], (102 + 104 + 90) / 3)
        row = observation(self.study(frame), frame.index[30])
        self.assertEqual(row["status"], "valid")
        self.assertEqual(pd.Timestamp(row["entry_time"]), frame.index[31])

    def test_zero_volume_cannot_signal_but_does_not_fabricate_a_vwap_price(self):
        frame = bars()
        frame.loc[frame.index[3], "Volume"] = 0.0
        rows, _ = feature_rows(frame, self.config)
        row = at(rows, frame.index[3])
        for event in EVENTS:
            self.assertFalse(row[event + "_eligible"])
            self.assertFalse(row[event])
        self.assertFalse(at(rows, frame.index[4])["vwap_cross_down_eligible"])
        self.assertTrue(at(rows, frame.index[5])["vwap_cross_down_eligible"])
        self.assertFalse(at(rows, frame.index[6])["relative_volume_gt_1_5_eligible"])

    def test_missing_entry_is_retained_and_not_moved_to_a_later_open(self):
        frame = event_bars()
        result = self.study(frame.drop(frame.index[7]))
        for horizon in (5, 15, 30):
            row = observation(result, frame.index[6], horizon=horizon)
            self.assertEqual(row["status"], "entry_missing")
            self.assertEqual(pd.Timestamp(row["entry_time"]), frame.index[7])
            self.assertIsNone(row["gross_pnl_yen"])

    def test_each_horizon_keeps_its_own_missing_path_status(self):
        frame = event_bars()
        result = self.study(frame.drop(frame.index[10]))
        self.assertEqual(observation(result, frame.index[6], horizon=5)["status"], "valid")
        self.assertEqual(observation(result, frame.index[6], horizon=15)["status"], "exit_missing")
        self.assertEqual(observation(result, frame.index[6], horizon=30)["status"], "path_missing")

    def test_future_close_is_not_required_for_known_open_outcomes(self):
        frame = event_bars()
        frame.loc[frame.index[[7, 8]], ["High", "Low", "Close"]] = float("nan")
        row = observation(self.study(frame), frame.index[6])
        self.assertEqual(row["status"], "valid")
        self.assertAlmostEqual(row["gross_pnl_yen"], 500.0)

    def test_invalid_open_and_no_volume_are_distinct_outcome_reasons(self):
        original = event_bars()
        for index, field, value, expected in (
            (7, "Open", float("nan"), "entry_invalid_open"),
            (7, "Volume", 0.0, "entry_no_volume"),
            (8, "Open", 0.0, "exit_invalid_open"),
            (8, "Volume", 0.0, "exit_no_volume"),
            (9, "Open", float("nan"), "path_invalid_open"),
            (9, "Volume", 0.0, "path_no_volume"),
        ):
            with self.subTest(expected=expected):
                frame = original.copy()
                frame.loc[frame.index[index], field] = value
                horizon = 15 if index == 9 else 5
                row = observation(self.study(frame), frame.index[6], horizon=horizon)
                self.assertEqual(row["status"], expected)
                self.assertIsNone(row["gross_pnl_yen"])

    def test_lunch_and_end_of_day_boundaries_are_not_crossed(self):
        for index, status in ((28, "horizon_session_boundary"),
                              (29, "entry_session_boundary"),
                              (63, "horizon_session_boundary"),
                              (64, "entry_session_boundary")):
            with self.subTest(index=index):
                frame = bars()
                set_close(frame, index, 90.0)
                row = observation(self.study(frame), frame.index[index])
                self.assertEqual(row["status"], status)
                self.assertIsNone(row["gross_pnl_yen"])

    def test_timezone_naive_input_is_rejected(self):
        frame = event_bars()
        frame.index = frame.index.tz_localize(None)
        with self.assertRaises(ValueError):
            self.study(frame)

    def test_duplicate_copies_and_off_grid_rows_do_not_supply_valid_features(self):
        frame = event_bars()
        duplicate = frame.loc[[frame.index[6]]].copy()
        duplicate["Close"] = 95.0
        extra_index = pd.DatetimeIndex([
            pd.Timestamp("2026-10-01 11:30", tz=TZ),
            pd.Timestamp("2026-10-01 15:25", tz=TZ),
            pd.Timestamp("2026-10-01 15:30", tz=TZ)])
        extra = pd.DataFrame({"Open": 100., "High": 101., "Low": 99.,
                              "Close": 100., "Volume": 100.}, index=extra_index)
        rows, quality = feature_rows(pd.concat([frame, duplicate, extra]), self.config)
        self.assertEqual(len(rows), 65)
        duplicate_row = at(rows, frame.index[6])
        self.assertTrue(all(not duplicate_row[event + "_eligible"] for event in EVENTS))
        self.assertFalse(any(pd.Timestamp(r["signal_time"]) in extra_index for r in rows))
        audit = json.dumps(quality, ensure_ascii=False).lower()
        self.assertIn("duplicate", audit)
        self.assertTrue("off_grid" in audit or "offgrid" in audit)

    def test_random_controls_are_reproducible_and_input_order_is_irrelevant(self):
        first, second = event_bars(), event_bars("2026-10-02")
        before = run_study({"B": first, "A": second}, self.config)
        after = run_study({"A": second.iloc[::-1], "B": first.iloc[::-1]}, self.config)
        self.assertEqual(before, after)
        self.assertTrue(any(row["status"] == "valid" for row in before["controls"]))
        self.assertEqual(len(before["controls"]), len(before["observations"]))
        coverage = {(row["symbol"], row["event_type"]): row
                    for row in before["feature_coverage"]}
        for symbol in ("A", "B"):
            self.assertEqual(coverage[symbol, "time_of_day"]["scheduled_slots"], 130)
            self.assertEqual(coverage[symbol, "time_of_day"]["signal_side_eligible"], 65)
        absent = [row for row in before["quality"] if row["whole_day_absent"]]
        self.assertEqual(len(absent), 2)
        self.assertTrue(all(row["missing_or_ambiguous_regular_bars"] == 65 for row in absent))

    def test_controls_match_strata_without_replacement_and_include_event_times(self):
        frame = event_bars()
        result = self.study(frame)
        events = {row["observation_id"]: row for row in result["observations"]}
        features, _ = feature_rows(frame, self.config)
        features_by_time = {row["signal_time"]: row for row in features}
        used = {}
        actual_event_was_sampled = False
        non_event_was_sampled = False
        for control in result["controls"]:
            if control["status"] == "no_control":
                continue
            event = events[control["matched_observation_id"]]
            for key in ("symbol", "date", "time_bucket", "horizon_minutes", "event_type"):
                self.assertEqual(control[key], event[key])
            feature = features_by_time[control["signal_time"]]
            self.assertTrue(feature[control["event_type"] + "_eligible"])
            stratum = tuple(control[key] for key in
                            ("symbol", "date", "time_bucket", "horizon_minutes", "event_type"))
            used.setdefault(stratum, []).append(control["signal_time"])
            if feature[control["event_type"]]:
                actual_event_was_sampled = True
            else:
                non_event_was_sampled = True
        self.assertTrue(actual_event_was_sampled)
        self.assertTrue(non_event_was_sampled)
        for times in used.values():
            self.assertEqual(len(times), len(set(times)))

    def test_session_boundary_trials_do_not_remove_valid_controls_from_same_bucket(self):
        result = self.study(bars())
        events = {row["observation_id"]: row for row in result["observations"]}
        mixed = [row for row in result["controls"]
                 if row["event_type"] == "time_of_day" and row["time_bucket"] == "11:00"
                 and row["horizon_minutes"] == 15]
        valid = [row for row in mixed if events[row["matched_observation_id"]]["status"] == "valid"]
        boundary = [row for row in mixed if events[row["matched_observation_id"]]["status"] != "valid"]
        self.assertTrue(valid)
        self.assertTrue(boundary)
        self.assertTrue(all(row["status"] == "valid" for row in valid))
        self.assertTrue(all(row["status"] == "no_control" for row in boundary))

    def test_future_outcome_failure_does_not_redraw_unchanged_control_stratum(self):
        frame = event_bars()
        # Every 30-minute outcome starting in 09:30's bucket includes 10:00.
        # Changing that future Open cannot alter signal history in that bucket.
        changed = frame.copy()
        changed.loc[pd.Timestamp("2026-10-01 10:00", tz=TZ), "Open"] = float("nan")
        before = self.study(frame)
        after = self.study(changed)
        before_events = {row["observation_id"]: row for row in before["observations"]
                         if row["time_bucket"] == "09:30" and row["horizon_minutes"] == 30}
        old = {row["matched_observation_id"]: row for row in before["controls"]
               if row["matched_observation_id"] in before_events}
        new = {row["matched_observation_id"]: row for row in after["controls"]
               if row["matched_observation_id"] in before_events}
        self.assertTrue(old)
        self.assertEqual(old.keys(), new.keys())
        for identity in old:
            for key in ("signal_time", "entry_time", "exit_time", "selection_seed", "candidate_count"):
                self.assertEqual(old[identity][key], new[identity][key])
            self.assertEqual(old[identity]["status"], "valid")
            self.assertIn(new[identity]["status"], ("exit_invalid_open", "path_invalid_open"))
            self.assertIsNone(new[identity]["gross_pnl_yen"])

    def test_summary_arithmetic_retains_unobservable_trials_and_valid_pairs_only(self):
        frame = event_bars()
        # A future missing slot affects different horizons independently, while
        # boundary observations still remain in the denominators.
        second = event_bars("2026-10-02")
        second.loc[:, ["Open", "High", "Low", "Close"]] *= 2
        result = self.study(pd.concat([frame.drop(frame.index[10]), second.iloc[:15]]))
        matched = {row["matched_observation_id"]: row for row in result["controls"]}
        scenarios = {"zero": ("short_gross_return_pct", "gross_pnl_yen"),
                     "assumed_slip1": ("net1_return_pct", "net1_pnl_yen"),
                     "assumed_slip2": ("net2_return_pct", "net2_pnl_yen")}
        saw_incomplete = False
        saw_empty = False

        def assert_optional_mean(actual, values):
            if values:
                self.assertAlmostEqual(actual, sum(values) / len(values))
            else:
                self.assertIsNone(actual)

        for summary in result["summaries"]:
            trials = [row for row in result["observations"]
                      if row["event_type"] == summary["event_type"]
                      and row["horizon_minutes"] == summary["horizon_minutes"]
                      and (summary["time_bucket"] == "ALL"
                           or row["time_bucket"] == summary["time_bucket"])]
            valid = [row for row in trials if row["status"] == "valid"]
            random = [matched[row["observation_id"]] for row in trials]
            random_valid = [row for row in random if row["status"] == "valid"]
            pairs = [(row, matched[row["observation_id"]]) for row in valid
                     if matched[row["observation_id"]]["status"] == "valid"]
            return_key, pnl_key = scenarios[summary["cost_scenario"]]
            self.assertEqual(summary["event_trials"], len(trials))
            self.assertEqual(summary["event_observed"], len(valid))
            self.assertEqual(summary["random_trials"], len(trials))
            self.assertEqual(summary["random_observed"], len(random_valid))
            self.assertEqual(summary["paired_observed"], len(pairs))
            if trials:
                self.assertAlmostEqual(summary["event_coverage_pct"], 100 * len(valid) / len(trials))
                self.assertAlmostEqual(summary["random_coverage_pct"], 100 * len(random_valid) / len(trials))
            else:
                self.assertIsNone(summary["event_coverage_pct"])
                self.assertIsNone(summary["random_coverage_pct"])
            assert_optional_mean(summary["event_mean_reference_pnl_yen"], [r[pnl_key] for r in valid])
            assert_optional_mean(summary["random_mean_reference_pnl_yen"], [r[pnl_key] for r in random_valid])
            assert_optional_mean(summary["event_mean_short_reference_return_pct"], [r[return_key] for r in valid])
            daily = {}
            for event, control in pairs:
                daily.setdefault(event["date"], []).append(event[return_key] - control[return_key])
            self.assertEqual(summary["paired_dates"], len(daily))
            self.assertEqual(summary["paired_symbol_days"], len({(a["symbol"], a["date"]) for a, _ in pairs}))
            if summary["event_type"] == "time_of_day":
                self.assertEqual(summary["paired_comparison_status"], "not_applicable_same_universe")
                self.assertIsNone(summary["paired_mean_excess_short_return_pct"])
                self.assertIsNone(summary["paired_day_mean_excess_short_return_pct"])
            else:
                self.assertEqual(summary["paired_comparison_status"], "descriptive_matched_difference")
                assert_optional_mean(summary["paired_mean_excess_short_return_pct"],
                                     [a[return_key] - b[return_key] for a, b in pairs])
                assert_optional_mean(summary["paired_day_mean_excess_short_return_pct"],
                                     [sum(values) / len(values) for values in daily.values()])
            if valid:
                self.assertAlmostEqual(summary["event_median_reference_pnl_yen"],
                                       pd.Series([r[pnl_key] for r in valid]).median())
                self.assertAlmostEqual(summary["event_down_fraction_pct"],
                                       100 * sum(r["price_return_pct"] < 0 for r in valid) / len(valid))
            else:
                self.assertIsNone(summary["event_median_reference_pnl_yen"])
                self.assertIsNone(summary["event_down_fraction_pct"])
                saw_empty = True
            saw_incomplete |= len(valid) < len(trials)
        self.assertTrue(saw_incomplete)
        self.assertTrue(saw_empty)

    def test_time_of_day_permutation_with_missingness_has_no_reported_paired_effect(self):
        frame = bars()
        for i, t in enumerate(frame.index):
            price = 100 + i * 0.1 + (i % 5) * 0.7
            frame.loc[t, ["Open", "High", "Low", "Close"]] = [price, price + 1, price - 1, price]
        result = self.study(frame.drop(frame.index[6]))
        matched = {row["matched_observation_id"]: row for row in result["controls"]}
        trials = [row for row in result["observations"]
                  if row["event_type"] == "time_of_day" and row["horizon_minutes"] == 15]
        pairs = [(row, matched[row["observation_id"]]) for row in trials
                 if row["status"] == "valid" and matched[row["observation_id"]]["status"] == "valid"]
        self.assertTrue(pairs)
        raw_pair_difference = sum(a["short_gross_return_pct"] - b["short_gross_return_pct"]
                                  for a, b in pairs) / len(pairs)
        # This nonzero difference is created solely by the random pairing of
        # overlapping valid subsets; event/control whole sets are identical.
        self.assertGreater(abs(raw_pair_difference), 0.01)
        summaries = [row for row in result["summaries"] if row["event_type"] == "time_of_day"]
        for row in summaries:
            self.assertEqual(row["event_observed"], row["random_observed"])
            for event_key, random_key in (
                    ("event_mean_price_return_pct", "random_mean_price_return_pct"),
                    ("event_mean_short_reference_return_pct", "random_mean_short_reference_return_pct"),
                    ("event_mean_reference_pnl_yen", "random_mean_reference_pnl_yen")):
                if row[event_key] is None:
                    self.assertIsNone(row[random_key])
                else:
                    self.assertAlmostEqual(row[event_key], row[random_key])
            self.assertEqual(row["paired_comparison_status"], "not_applicable_same_universe")
            self.assertIsNone(row["paired_mean_excess_short_return_pct"])
            self.assertIsNone(row["paired_day_mean_excess_short_return_pct"])
        aggregate = next(row for row in summaries if row["time_bucket"] == "ALL"
                         and row["horizon_minutes"] == 15 and row["cost_scenario"] == "zero")
        self.assertEqual(aggregate["paired_observed"], len(pairs))
        self.assertLess(aggregate["event_coverage_pct"], 100)

    def test_no_events_has_explicit_zero_trial_summaries_without_invented_zero_returns(self):
        result = run_study({}, self.config)
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["controls"], [])
        self.assertEqual(len(result["summaries"]), 5 * 3 * 3)
        for summary in result["summaries"]:
            self.assertEqual(summary["time_bucket"], "ALL")
            self.assertEqual(summary["event_trials"], 0)
            self.assertEqual(summary["event_observed"], 0)
            self.assertIsNone(summary["event_coverage_pct"])
            self.assertIsNone(summary["event_mean_reference_pnl_yen"])
            self.assertIsNone(summary["paired_mean_excess_short_return_pct"])


if __name__ == "__main__":
    unittest.main()
