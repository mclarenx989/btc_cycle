#!/usr/bin/env python3
"""Tests for the pure modules. No network, no database.

    python3 -m unittest test_btc_cycle -v
"""

import unittest
from datetime import date, timedelta

import pivots as pv
import schedule as sch
import score as sc
import sources


def series(spec):
    """spec: list of (date, high, low) -> candle series."""
    return [{"date": d, "open": h, "high": h, "low": l, "close": l, "source": "test"}
            for d, h, l in spec]


def ramp(start, days, high, low):
    return [(start + timedelta(days=i), high, low) for i in range(days)]


class TestUTC(unittest.TestCase):
    """A local-time conversion shifts every candle back a day in US Eastern,
    which silently moves every pivot. This pins it."""

    def test_utc_midnight_stays_on_its_own_day(self):
        # 2025-10-06T00:00:00Z
        self.assertEqual(sources._utc_date(1759708800), date(2025, 10, 6))

    def test_known_pivot_epochs(self):
        self.assertEqual(sources._utc_date(1421193600), date(2015, 1, 14))
        self.assertEqual(sources._utc_date(1513468800), date(2017, 12, 17))


class TestPivots(unittest.TestCase):
    def setUp(self):
        # peak 100 -> trough 20 -> peak 200, with long flats so both peaks
        # clear the confirmation window.
        s = []
        s += ramp(date(2020, 1, 1), 10, 50, 45)
        s += [(date(2020, 1, 11), 100, 95)]                  # ATH #1
        s += ramp(date(2020, 1, 12), 400, 60, 55)
        s += [(date(2021, 2, 15), 30, 20)]                   # the low
        s += ramp(date(2021, 2, 16), 400, 60, 55)
        s += [(date(2022, 3, 23), 200, 195)]                 # ATH #2
        s += ramp(date(2022, 3, 24), 400, 150, 140)
        self.series = series(s)

    def test_finds_both_peaks_and_the_trough_between(self):
        got = pv.detect(self.series)
        self.assertEqual([(p["date"], p["kind"]) for p in got], [
            (date(2020, 1, 11), "ath"),
            (date(2021, 2, 15), "atl"),
            (date(2022, 3, 23), "ath"),
        ])

    def test_run_up_days_are_not_each_an_ath(self):
        """The naive 'high above every prior high' rule marks every day of a
        rally as a top. Only the confirmed peak counts."""
        aths = [p for p in pv.detect(self.series) if p["kind"] == "ath"]
        self.assertEqual(len(aths), 2)

    def test_running_low_is_unconfirmed(self):
        low = pv.running_low_since(self.series, date(2022, 3, 23))
        self.assertEqual(low["low"], 140)
        self.assertNotIn("atl", [p["kind"] for p in pv.detect(self.series)
                                 if p["date"] > date(2022, 3, 23)])


class TestConfirmDaysCalibration(unittest.TestCase):
    """CONFIRM_DAYS is two-sided. Below 238 the 2024-03-13 consolidation peak
    (237d un-exceeded) is wrongly promoted to a cycle top; above 302 the
    2025-10-06 ATH cannot be confirmed yet. Both edges are load-bearing."""

    def test_window_bounds_are_sane(self):
        self.assertLess(pv.CONFIRM_MIN, pv.CONFIRM_MAX)
        self.assertTrue(pv.CONFIRM_MIN <= pv.CONFIRM_DAYS <= pv.CONFIRM_MAX)

    def test_lower_edge_excludes_the_known_false_positive(self):
        self.assertGreater(pv.CONFIRM_DAYS, 237)

    def test_upper_edge_still_confirms_the_2025_ath(self):
        self.assertLessEqual(pv.CONFIRM_DAYS, 302)


class TestSchedule(unittest.TestCase):
    def test_oracle_chain_reproduces_the_famous_date(self):
        """From the 2021-11-08 daily-close anchor: +364 -> a low that never
        happened, +1064 -> 2025-10-06."""
        rows = sch.oracle(steps=2)
        self.assertEqual(rows[0]["date"], date(2022, 11, 7))
        self.assertEqual(rows[1]["date"], date(2025, 10, 6))

    def test_legs_are_whole_weeks(self):
        self.assertEqual(sch.UP_LEG % 7, 0)
        self.assertEqual(sch.DOWN_LEG % 7, 0)
        self.assertEqual(sch.UP_LEG // 7, 152)
        self.assertEqual(sch.DOWN_LEG // 7, 52)

    def test_applying_the_rule_to_the_real_2022_low_misses_by_two_weeks(self):
        """The post's stated rule on real data gives 2025-10-20, not 10-06."""
        real = sch.chain(date(2022, 11, 21), "atl", 1)
        self.assertEqual(real[0]["date"], date(2025, 10, 20))

    def test_current_leg_counts_forward_from_the_last_pivot(self):
        ps = [{"date": date(2025, 10, 6), "kind": "ath", "price": 126272.0}]
        leg = sch.current_leg(ps, date(2026, 8, 4))
        self.assertEqual(leg["elapsed"], 302)
        self.assertEqual(leg["target_date"], date(2026, 10, 5))
        self.assertEqual(leg["remaining"], 62)
        self.assertEqual(leg["to_kind"], "atl")


class TestScore(unittest.TestCase):
    def setUp(self):
        self.pivots = [
            {"date": date(2022, 11, 21), "kind": "atl", "price": 15479.0},
            {"date": date(2025, 10, 6), "kind": "ath", "price": 126272.0},
        ]

    def test_exact_hit(self):
        g = sc.grade({"date": date(2025, 10, 6), "kind": "ath"}, self.pivots)
        self.assertEqual(g["days_off"], 0)
        self.assertTrue(g["within_tolerance"])

    def test_miss_outside_band(self):
        g = sc.grade({"date": date(2022, 11, 7), "kind": "atl"}, self.pivots)
        self.assertEqual(g["days_off"], 14)
        self.assertFalse(g["within_tolerance"])

    def test_window_must_close_before_grading(self):
        proj = {"date": date(2026, 10, 5), "kind": "atl"}
        self.assertFalse(sc.resolvable(proj, self.pivots, date(2026, 10, 6)))
        self.assertTrue(sc.resolvable(proj, self.pivots, date(2026, 10, 20)))

    def test_claimed_legs_align_by_date_not_position(self):
        """Extending history backwards adds legs at the front. A positional
        zip compares the wrong pairs; this catches that regression."""
        realized = [
            {"from": {"date": date(2013, 11, 30), "kind": "ath"},
             "to": {"date": date(2015, 1, 14), "kind": "atl"}, "days": 410},
            {"from": {"date": date(2015, 1, 14), "kind": "atl"},
             "to": {"date": date(2017, 12, 17), "kind": "ath"}, "days": 1068},
            {"from": {"date": date(2017, 12, 17), "kind": "ath"},
             "to": {"date": date(2018, 12, 15), "kind": "atl"}, "days": 363},
            {"from": {"date": date(2018, 12, 15), "kind": "atl"},
             "to": {"date": date(2021, 11, 10), "kind": "ath"}, "days": 1061},
            {"from": {"date": date(2021, 11, 10), "kind": "ath"},
             "to": {"date": date(2022, 11, 21), "kind": "atl"}, "days": 376},
        ]
        rows = sc.score_claimed_legs(realized, sch.CLAIMED_LEGS)
        self.assertEqual([r["delta"] for r in rows], [4, -1, -3, 12])
        self.assertEqual(sum(1 for r in rows if r["within_tolerance"]), 2)


if __name__ == "__main__":
    unittest.main()
