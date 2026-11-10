# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Kieran Cox
"""Tests for the light-colour rules. Run: python3 -m unittest -v test_netwatch (from this dir)."""
import unittest

import netwatch as nw

C = dict(nw.DEFAULTS)
T = 1_000_000.0


def ping(avg=40.0, loss=0.0, sent=10, ts=T):
    recv = sent - round(sent * loss / 100)
    p = dict(type="ping", ts=ts, host="1.1.1.1", sent=sent, recv=recv, loss=loss)
    if recv:
        p.update(avg=avg, max=avg * 2, min=avg / 2, jitter=1.0)
    return p


def bw(mbit, ts=T):
    return dict(type="bw", ts=ts, mbit=mbit)


def state(p, b, at=T + 10):
    latest = {}
    if p:
        latest["ping"] = p
    if b:
        latest["bw"] = b
    return nw.classify(C, latest, at)[0]


class SpeedBands(unittest.TestCase):
    def test_green_over_20(self):
        self.assertEqual(state(ping(), bw(35.0)), "green")
        self.assertEqual(state(ping(), bw(20.0)), "green")

    def test_yellow_3_to_20(self):
        for v in (3.0, 5.0, 7.0, 12.0, 19.9):
            self.assertEqual(state(ping(), bw(v)), "yellow", v)

    def test_red_under_3(self):
        for v in (0.0, 0.4, 2.99):
            self.assertEqual(state(ping(), bw(v)), "red", v)


class PingFactor(unittest.TestCase):
    def test_ping_can_only_worsen(self):
        self.assertEqual(state(ping(avg=200), bw(50)), "yellow")   # slow ping caps at yellow
        self.assertEqual(state(ping(avg=500), bw(50)), "red")      # very slow ping -> red
        self.assertEqual(state(ping(loss=10), bw(50)), "yellow")   # some loss -> yellow
        self.assertEqual(state(ping(loss=30), bw(50)), "red")      # lots of loss -> red
        self.assertEqual(state(ping(loss=100), bw(50)), "red")     # dead -> red
        self.assertEqual(state(ping(avg=20), bw(1.0)), "red")      # good ping can't rescue slow speed

    def test_worst_of_two_wins(self):
        self.assertEqual(state(ping(avg=200), bw(1.0)), "red")


class Staleness(unittest.TestCase):
    def test_no_ping_data_is_unknown(self):
        self.assertEqual(state(None, bw(50)), "unknown")

    def test_stale_ping_is_unknown(self):
        self.assertEqual(state(ping(ts=T), bw(50, ts=T), at=T + C["ping_stale_s"] + 1), "unknown")

    def test_old_speed_is_ignored_not_trusted(self):
        # no speed in the window -> ping alone decides
        self.assertEqual(state(ping(ts=T + 6000), bw(0.1, ts=T), at=T + 6010), "green")


class SpeedTestSchedule(unittest.TestCase):
    def due(self, minutes, last_ago=None, at=T):
        c = dict(C, bw_interval_min=minutes)
        latest = {} if last_ago is None else {"bw_full": bw(10, ts=at - last_ago)}
        return nw.bw_due(c, latest, at)

    def test_never_ran_is_due(self):
        self.assertTrue(self.due(30))

    def test_not_due_until_interval_elapsed(self):
        self.assertFalse(self.due(30, last_ago=10 * 60))
        self.assertTrue(self.due(30, last_ago=30 * 60))
        self.assertTrue(self.due(5, last_ago=6 * 60))

    def test_shorter_interval_takes_effect_immediately(self):
        self.assertFalse(self.due(30, last_ago=8 * 60))
        self.assertTrue(self.due(5, last_ago=8 * 60))   # user picked "every 5 min"

    def test_timer_jitter_slack(self):
        self.assertTrue(self.due(10, last_ago=10 * 60 - 3))

    def test_paused_never_due(self):
        self.assertFalse(self.due(0))
        self.assertFalse(self.due(0, last_ago=99999))

    def test_quick_checks_do_not_reset_the_schedule(self):
        # only 'bw_full' counts; a quick sample stored under 'bw' must not delay the next full test
        c = dict(C, bw_interval_min=30)
        latest = {"bw": dict(bw(50, ts=T - 10), type="quick"), "bw_full": bw(50, ts=T - 40 * 60)}
        self.assertTrue(nw.bw_due(c, latest, T))

    def test_long_interval_keeps_speed_reading_valid(self):
        c = dict(C, bw_interval_min=360)  # 6 h: a 3 h-old reading must still count
        latest = {"ping": ping(ts=T), "bw": bw(0.5, ts=T - 3 * 3600)}
        self.assertEqual(nw.classify(c, latest, T + 5)[0], "red")


if __name__ == "__main__":
    unittest.main()
