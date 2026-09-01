"""
The chart primitives, and the observability column built on them.

Pure functions over series, so these need no cluster, no VictoriaMetrics and no
Flask. What they are guarding is that a quiet cluster — every series empty, or
flat, or missing — draws something honest rather than something broken, because
that is the state the panel is in most of the time and the state in which a
wrong chart is least likely to be noticed.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import charts                                                    # noqa: E402
import shape                                                     # noqa: E402


def series(*values, start=1_000_000, step=60):
    return [(start + i * step, v) for i, v in enumerate(values)]


def coords(svg):
    """Every (x, y) the paths in this SVG actually draw."""
    out = []
    for path in re.findall(r'\sd="([^"]+)"', svg):
        for x, y in re.findall(r"[ML]([-\d.]+) ([-\d.]+)", path):
            out.append((float(x), float(y)))
    return out


class EmptyTest(unittest.TestCase):
    """
    "Nothing matched" and "the value is zero" are different facts. Drawing the
    first as a flat line at zero is how somebody spends an afternoon debugging a
    healthy system, so every chart says so in words instead.
    """

    def test_every_chart_names_the_gap_rather_than_drawing_one(self):
        for label, body in (
                ("line", charts.line({})),
                ("line with only empty series", charts.line({"a": [], "b": []})),
                ("stack", charts.stack({})),
                ("bars", charts.bars([])),
                ("columns", charts.columns([])),
                ("bullet", charts.bullet(None))):
            with self.subTest(chart=label):
                self.assertIn("chart-empty", body)
                self.assertNotIn("<svg", body)

    def test_a_row_with_no_reading_is_dropped_not_drawn_as_zero(self):
        body = charts.bars([{"name": "up", "value": 4.0, "max": 10},
                            {"name": "silent", "value": None}])
        self.assertIn("up", body)
        self.assertNotIn("silent", body)


class ScaleTest(unittest.TestCase):
    def test_a_flat_series_does_not_divide_by_zero(self):
        svg = charts.line({"flat": series(3.0, 3.0, 3.0)})
        self.assertIn("<svg", svg)
        for _, y in coords(svg):
            self.assertFalse(y != y, "a NaN coordinate draws nothing at all")

    def test_an_all_zero_series_still_draws(self):
        svg = charts.line({"zero": series(0.0, 0.0)})
        self.assertIn("<svg", svg)
        self.assertEqual(len(coords(svg)), 2)

    def test_values_above_the_axis_clamp_instead_of_escaping_the_box(self):
        """
        A reading above the reference line is real and must stay visible. It is
        clamped rather than dropped, because a chart that omits the spike draws
        a gap exactly where the interesting thing happened.
        """
        svg = charts.line({"spike": series(1.0, 1.0, 500.0)}, reference=2.0)
        for x, y in coords(svg):
            self.assertGreaterEqual(y, 0.0)
            self.assertLessEqual(y, charts.H)
            self.assertGreaterEqual(x, 0.0)
            self.assertLessEqual(x, charts.W)

    def test_a_stack_carries_the_last_known_value_across_a_gap(self):
        """
        Layers must share a time axis to be stackable. A layer missing a point
        keeps its last value rather than dropping to zero, which would carve a
        notch out of every layer above it.
        """
        svg = charts.stack({"a": [(0, 2.0), (60, 2.0), (120, 2.0)],
                            "b": [(0, 1.0), (120, 1.0)]})
        self.assertIn("<svg", svg)
        self.assertIn("2 layers", svg)


class SafetyTest(unittest.TestCase):
    def test_a_series_name_cannot_close_the_tag_it_sits_in(self):
        """Names come from metric labels, which are data from the cluster."""
        svg = charts.line({'</title><script>x': series(1.0, 2.0)})
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_a_bar_name_is_escaped_in_both_places_it_appears(self):
        body = charts.bars([{"name": '"><b>', "value": 1.0, "max": 2}])
        self.assertNotIn("<b>", body)


class ToneTest(unittest.TestCase):
    def test_a_bullet_takes_its_tone_from_the_threshold_that_acts(self):
        # The container carries the verdict; `bullet-mark is-warn` is the
        # threshold TICK and is drawn whether or not it has been crossed.
        self.assertNotIn("bullet is-warn", charts.bullet(10, warn=70, danger=80))
        self.assertIn("bullet is-warn", charts.bullet(71, warn=70, danger=80))
        self.assertIn("bullet is-bad", charts.bullet(81, warn=70, danger=80))

    def test_a_bullet_marks_both_thresholds_even_when_neither_is_crossed(self):
        body = charts.bullet(5, warn=70, danger=80)
        self.assertIn("bullet-mark is-warn", body)
        self.assertIn("bullet-mark is-bad", body)

    def test_a_zero_count_still_draws_a_baseline(self):
        """
        "We counted and it was none" must look different from "we did not
        look" — the empty state above covers the second.
        """
        svg = charts.columns([{"name": "OOM kills", "value": 0.0}])
        self.assertIn("<svg", svg)
        self.assertIn("OOM kills", svg)


class ColumnTest(unittest.TestCase):
    """The assembled RED / USE / Golden column."""

    def build(self, ranges=None, instants=None):
        ranges = ranges or {}
        instants = instants or {}
        return shape.observability(
            lambda expr, minutes, step, label=None: ranges.get(expr, {}),
            lambda expr: instants.get(expr),
            charts)

    def test_the_three_frameworks_are_separated_by_scope(self):
        sections = self.build()
        self.assertEqual([s["key"] for s in sections], ["red", "use", "golden"])
        for section in sections:
            with self.subTest(section=section["key"]):
                # They overlap by construction — RED is a subset of the golden
                # signals. Saying which scope each answers at is the only thing
                # that stops the column reading as one chart printed three times.
                self.assertTrue(section["scope"])

    def test_a_cluster_with_no_metrics_at_all_renders_every_card(self):
        for section in self.build():
            for card in section["cards"]:
                with self.subTest(card=card["title"]):
                    self.assertTrue(card["body"])
                    self.assertTrue(card["note"])

    def test_the_missing_http_timer_is_named_rather_than_left_blank(self):
        """
        No application in this cluster publishes an HTTP timer, so per-service
        rate and errors have no source. The card says which metric would give
        it one instead of drawing an unexplained empty box.
        """
        red = self.build()[0]
        warned = [c for c in red["cards"] if c["warning"]]
        self.assertEqual([c["title"] for c in warned], ["Rate"])
        self.assertIn("http_server_requests_seconds", warned[0]["warning"])

    def test_saturation_is_drawn_against_the_thresholds_that_spend_money(self):
        golden = self.build(instants={"overseer_cluster_cpu_percent": 85.0,
                                      "overseer_cluster_mem_percent": 20.0})[2]
        card = next(c for c in golden["cards"] if c["title"] == "Saturation")
        self.assertIn("bullet is-bad", card["body"])       # cpu over danger
        self.assertIn(f"{shape.SATURATION_WARN:.0f}%", card["note"])
        self.assertIn(f"{shape.SATURATION_DANGER:.0f}%", card["note"])

    def test_the_error_ratio_is_redrawn_as_a_percentage(self):
        """
        The query answers 0..1 and the reference line is 5. Charting the raw
        ratio against it would put every real error rate flat on the floor.
        """
        sections = self.build(ranges={shape.Q_ERROR_RATIO: {"5xx": series(0.5)}})
        red = sections[0]
        card = next(c for c in red["cards"] if c["title"] == "Errors")
        self.assertIn("50.0%", card["body"])

    def test_utilisation_rows_are_toned_by_the_same_thresholds(self):
        ranges = {expr: {"wkr-1": series(95.0)} for _, expr in shape.Q_UTILISATION}
        use = self.build(ranges=ranges)[1]
        card = next(c for c in use["cards"] if c["title"] == "Utilisation")
        self.assertIn("bar-row is-bad", card["body"])
        self.assertIn("wkr-1 · cpu", card["body"])


class LogShapeTest(unittest.TestCase):
    def test_a_level_is_found_however_the_line_spells_it(self):
        self.assertEqual(shape.log_level("2026-01-01 ERROR boom"), "err")
        self.assertEqual(shape.log_level("level=error boom"), "err")
        self.assertEqual(shape.log_level("WARN slow"), "warn")
        self.assertEqual(shape.log_level("level=warning slow"), "warn")
        self.assertEqual(shape.log_level("all fine"), "")

    def test_error_wins_over_warning_on_one_line(self):
        self.assertEqual(shape.log_level("WARN retrying after ERROR"), "err")

    def test_a_line_with_no_timestamp_is_not_given_a_made_up_one(self):
        rows = shape.log_rows([(None, "from the docker CLI")])
        self.assertEqual(rows[0]["at"], "")


if __name__ == "__main__":
    unittest.main()
