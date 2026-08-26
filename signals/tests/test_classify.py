"""
The decision rule, tested where it lives.

    python3 -m unittest discover -s signals/tests

This is the one copy of "is this service slow, and is that its own fault". The
dispatcher applies it; the autoscaler acts on the result. Before the split the
autoscaler did both, which is why it had to know what a MongoDB driver timer
looked like.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from signals import classify  # noqa: E402

UP, DOWN, HOLD = classify.DIRECTION_UP, classify.DIRECTION_DOWN, classify.DIRECTION_HOLD

#: api_app's real policy, read off the live service on 2026-08-26.
API = classify.DEFAULTS._replace(slo_ms=500.0, up_ratio=0.8, down_ratio=0.36,
                                 up_cpu=70.0, down_cpu=30.0)


def decide(lat=None, cpu=None, mem=None, lat_pk=None, cpu_pk=None, mem_pk=None, t=API):
    return classify.decide(t, (lat, cpu, mem), (lat_pk, cpu_pk, mem_pk))


class UpTest(unittest.TestCase):
    def test_the_real_event_does_not_scale(self):
        """
        08-22 14:05 on short-drama-master. One person testing an Android client
        made four calls; one took 904ms. With no other traffic in the 2-minute
        rate window that single request WAS the service's mean latency, held
        past the 90-second sustain check. CPU was 11%. The cluster went
        2 -> 3 -> 4 replicas for one user and drained back half an hour later.
        """
        self.assertEqual(decide(lat=904.0, cpu=11.0, mem=7.0)[0], HOLD)

    def test_the_same_latency_with_busy_replicas_does_scale(self):
        self.assertEqual(decide(lat=904.0, cpu=40.0, mem=7.0)[0], UP)

    def test_memory_alone_counts_as_busy_for_latency(self):
        # A replica pinned against its heap ceiling is working even when the CPU
        # says otherwise: GC pressure shows up as latency, not as cycles.
        self.assertEqual(decide(lat=904.0, cpu=2.0, mem=75.0)[0], UP)

    def test_unknown_utilisation_is_not_busy(self):
        # cadvisor missing must never read as permission to scale on latency.
        self.assertEqual(decide(lat=904.0)[0], HOLD)

    def test_latency_exactly_on_the_line_does_not_trigger(self):
        self.assertEqual(decide(lat=400.0, cpu=90.0)[0], UP)     # cpu triggers
        self.assertEqual(decide(lat=400.0, cpu=40.0)[0], HOLD)   # 400 is not > 400

    def test_cpu_alone_triggers_without_any_latency_signal(self):
        self.assertEqual(decide(cpu=71.0)[0], UP)
        self.assertEqual(decide(cpu=69.0)[0], HOLD)

    def test_memory_alone_triggers(self):
        self.assertEqual(decide(mem=90.0)[0], UP)
        self.assertEqual(decide(mem=80.0)[0], HOLD)

    def test_the_reason_names_every_signal_that_fired(self):
        _, reason = decide(lat=900.0, cpu=90.0, mem=95.0)
        for fragment in ("latency held", "cpu/replica held", "memory/replica held"):
            self.assertIn(fragment, reason)


class DownTest(unittest.TestCase):
    QUIET = dict(lat_pk=1.0, cpu_pk=5.0, mem_pk=5.0)

    def test_everything_quiet_scales_down(self):
        self.assertEqual(decide(**self.QUIET)[0], DOWN)

    def test_no_traffic_at_all_still_scales_down(self):
        # An absent latency series is an idle service, not an unknown one — but
        # CPU must still be present, or a dead cadvisor would drain the fleet.
        self.assertEqual(decide(lat_pk=None, cpu_pk=5.0, mem_pk=5.0)[0], DOWN)

    def test_missing_cpu_holds_rather_than_shrinks(self):
        self.assertEqual(decide(lat_pk=1.0, cpu_pk=None, mem_pk=5.0)[0], HOLD)

    def test_busy_cpu_holds(self):
        self.assertEqual(decide(lat_pk=1.0, cpu_pk=50.0, mem_pk=5.0)[0], HOLD)

    def test_high_memory_holds_a_service_cpu_would_shrink(self):
        self.assertEqual(decide(lat_pk=1.0, cpu_pk=5.0, mem_pk=90.0)[0], HOLD)

    def test_unknown_memory_does_not_block_shrinking(self):
        # A service with no memory limit would otherwise never come down.
        self.assertEqual(decide(lat_pk=1.0, cpu_pk=5.0, mem_pk=None)[0], DOWN)

    def test_latency_above_the_down_line_holds(self):
        self.assertEqual(decide(lat_pk=200.0, cpu_pk=5.0, mem_pk=5.0)[0], HOLD)


class HoldReasonTest(unittest.TestCase):
    def test_a_throttled_service_says_why_it_is_held(self):
        direction, reason = decide(lat=904.0, cpu=5.0, mem=5.0)
        self.assertEqual(direction, HOLD)
        self.assertIn("replicas are idle", reason)

    def test_an_ordinary_hold_is_silent(self):
        # Between the two lines and nothing wrong: no reason, so nothing is
        # published, logged or dispatched about it.
        self.assertEqual(decide(lat=300.0, cpu=50.0, mem=50.0,
                                lat_pk=300.0, cpu_pk=50.0, mem_pk=50.0),
                         (HOLD, ""))


class ThresholdTest(unittest.TestCase):
    def test_defaults_when_a_service_declares_nothing(self):
        self.assertEqual(classify.thresholds_from_labels({}), classify.DEFAULTS)

    def test_labels_win(self):
        t = classify.thresholds_from_labels({"autoscale.slo_p95_ms": "800",
                                             "autoscale.up_cpu_pct": "50"})
        self.assertEqual((t.slo_ms, t.up_cpu), (800.0, 50.0))

    def test_a_typo_falls_back_and_reports_rather_than_raising(self):
        seen = []
        t = classify.thresholds_from_labels({"autoscale.slo_p95_ms": "soon"},
                                            on_bad=lambda k, v: seen.append(k))
        self.assertEqual(t.slo_ms, classify.DEFAULTS.slo_ms)
        self.assertEqual(seen, ["autoscale.slo_p95_ms"])

    def test_a_crossed_pair_reverts_BOTH_sides(self):
        # Repairing one side produces a configuration nobody wrote.
        t = classify.thresholds_from_labels({"autoscale.up_cpu_pct": "20",
                                             "autoscale.down_cpu_pct": "60"})
        self.assertEqual((t.up_cpu, t.down_cpu),
                         (classify.DEFAULTS.up_cpu, classify.DEFAULTS.down_cpu))

    def test_the_busy_floor_can_never_exceed_the_scale_up_threshold(self):
        # A floor above the trigger reads as "stricter" and means "latency can
        # never scale this service at all".
        t = classify.thresholds_from_labels({"autoscale.up_cpu_pct": "50",
                                             "autoscale.busy_cpu_pct": "90"})
        self.assertEqual(t.busy_cpu, 50.0)


class SaturationTest(unittest.TestCase):
    def test_either_resource_is_enough(self):
        self.assertTrue(classify.saturated(25.0, 60.0, 30.0, 1.0))
        self.assertTrue(classify.saturated(25.0, 60.0, 1.0, 70.0))

    def test_neither_is_not(self):
        self.assertFalse(classify.saturated(25.0, 60.0, 5.0, 5.0))

    def test_unknown_is_not_busy(self):
        self.assertFalse(classify.saturated(25.0, 60.0, None, None))

    def test_exactly_on_the_floor_counts_as_busy(self):
        self.assertTrue(classify.saturated(25.0, 60.0, 25.0, None))


if __name__ == "__main__":
    unittest.main()
