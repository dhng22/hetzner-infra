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


class AxisTest(unittest.TestCase):
    """
    A line with no scale on either edge is a shape, not a measurement. These
    pin the furniture that makes it one — and pin it OUTSIDE the viewBox,
    because the plot is stretched to the column width and text inside it would
    be stretched with it.
    """

    def test_every_chart_says_where_it_is_now_rather_than_naming_its_axes(self):
        """
        The line under a chart used to read "y: p95 latency (ms) · x: last 60
        min", which restated the tick labels sitting on both edges. It says
        where the chart IS instead — the number a shape cannot show and the one
        somebody scanning a column of them is after.
        """
        for label, body in (
                ("line", charts.line({"api": series(1.0, 2.0)}, "ms")),
                ("stack", charts.stack({"2xx": series(1.0, 2.0)}, "/s")),
                ("columns", charts.columns([{"name": "OOM", "value": 2.0}])),
                ("bars", charts.bars([{"name": "wkr-1", "value": 2.0,
                                       "max": 4.0}], "%"))):
            with self.subTest(chart=label):
                self.assertIn("chart-note", body)
                self.assertNotIn("y:", body)
                self.assertNotIn("x:", body)

    def test_a_reading_names_the_series_only_when_there_is_a_choice(self):
        one = charts.line({"api": series(300.0, 412.0)}, "ms")
        many = charts.line({"api": series(300.0, 412.0),
                            "web": series(90.0, 91.0)}, "ms")
        self.assertIn("now 412ms", one)
        self.assertNotIn("highest", one)
        self.assertIn("api highest", many)

    def test_a_reading_says_which_side_of_the_rule_it_is_on(self):
        """
        The dashed line is already drawn; whether today's number is above it is
        the fact worth reading, and a glance at a 96px chart does not settle it.
        """
        under = charts.line({"api": series(300.0, 412.0)}, "ms", reference=500.0)
        over = charts.line({"api": series(300.0, 620.0)}, "ms", reference=500.0)
        self.assertIn("under the 500ms line", under)
        self.assertIn("OVER the 500ms line", over)

    def test_a_stack_names_the_layer_the_total_is_mostly_made_of(self):
        note = charts.stack({"2xx": series(10.0, 12.0),
                             "5xx": series(0.2, 0.3)}, "/s")
        self.assertIn("mostly 2xx", note)

    def test_a_tally_of_nothing_says_so_in_words(self):
        """
        A row of baselines looks identical to a chart that failed to load, and
        "we counted and it was none" is the answer here most of the time.
        """
        self.assertIn("nothing counted in this window",
                      charts.columns([{"name": "OOM kills", "value": 0.0},
                                      {"name": "tx errors", "value": 0.0}]))
        self.assertIn("counted: tx errors 11.0, OOM kills 3.0",
                      charts.columns([{"name": "OOM kills", "value": 3.0},
                                      {"name": "tx errors", "value": 11.0}]))

    def test_the_axis_ends_are_printed_not_only_implied(self):
        svg = charts.line({"api": series(10.0, 5407.0)}, "ms")
        self.assertIn("chart-y", svg)
        self.assertIn("chart-x", svg)
        # Both ends of time, named relative to the newest sample: this module
        # is pure and the reader's timezone is not one of its arguments.
        self.assertIn("now", svg)
        self.assertIn("1 min ago", svg)

    def test_the_axis_top_is_a_number_somebody_would_write_down(self):
        """A peak of 5407 gives an axis of 6k, not one labelled 5.4k."""
        self.assertEqual(charts._nice(5407.0), 6000.0)
        self.assertEqual(charts._nice(100.0), 100.0)
        self.assertEqual(charts._nice(0.0), 1.0)
        self.assertIn("6.0k", charts.line({"a": series(5407.0, 10.0)}))

    def test_the_axis_top_is_never_below_the_peak_it_has_to_hold(self):
        for peak in (0.004, 0.4, 7.0, 99.0, 101.0, 1234.0, 999999.0):
            with self.subTest(peak=peak):
                self.assertGreaterEqual(charts._nice(peak), peak)

    def test_a_series_needs_no_legend_entry_it_has_no_colour_for(self):
        """
        Past the fourth series there is no distinct hue left, so a fifth named
        legend row would claim a distinction the picture does not make.
        """
        many = {f"n{i}": series(1.0, 2.0) for i in range(7)}
        legend = charts.line(many)
        self.assertIn("+3 more", legend)
        # Named in the legend is the claim; the line itself and its readout
        # still carry every series, because those are not claiming a hue.
        self.assertNotIn("</i>n6</span>", legend)
        self.assertIn("n6 1.0", legend)


class HoverTest(unittest.TestCase):
    """
    The readout. `data-tip` is handled globally and by delegation in app.js, so
    these charts get a no-delay tooltip with no chart-specific JavaScript — and
    keep it after the live poll replaces the markup, which a listener bound to
    a chart element would not survive.
    """

    def test_every_sample_carries_its_own_reading(self):
        svg = charts.line({"api": series(1.0, 2.0, 3.0)}, "ms")
        self.assertEqual(svg.count('class="chart-slice"'), 3)
        self.assertIn("now · api 3.0ms", svg)
        self.assertIn("2 min ago · api 1.0ms", svg)

    def test_the_slices_cover_the_plot_with_no_dead_ground(self):
        """Every pixel belongs to one sample, so there is nowhere that answers
        nothing."""
        svg = charts.line({"a": series(1.0, 2.0, 3.0, 4.0)})
        edges = [(float(x), float(w)) for x, w in
                 re.findall(r'chart-slice" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"',
                            svg)]
        self.assertEqual(len(edges), 4)
        self.assertAlmostEqual(edges[0][0], charts.PAD_L, places=1)
        for (x, w), (nxt, _) in zip(edges, edges[1:]):
            self.assertAlmostEqual(x + w, nxt, places=1)
        self.assertAlmostEqual(edges[-1][0] + edges[-1][1],
                               charts.W - charts.PAD_R, places=1)

    def test_a_stack_reads_out_each_layer_not_the_running_total(self):
        """
        The stacked HEIGHT is what the picture already shows. What it cannot
        show is which part of it belongs to which colour.
        """
        svg = charts.stack({"2xx": [(0, 3.0)], "5xx": [(0, 1.0)]}, "/s")
        self.assertIn("2xx 3.0/s", svg)
        self.assertIn("5xx 1.0/s", svg)
        self.assertIn("total 4.0/s", svg)

    def test_a_gap_is_left_out_of_the_readout_rather_than_carried(self):
        """
        A line has to join two points across a gap; a tooltip does not, and one
        that did would be reporting a measurement nobody took.
        """
        svg = charts.line({"a": [(0, 1.0), (60, 2.0)], "b": [(0, 5.0)]})
        self.assertIn("1 min ago · a 1.0, b 5.0", svg)
        self.assertIn("now · a 2.0", svg)
        self.assertNotIn("now · a 2.0, b", svg)

    def test_a_series_name_cannot_break_out_of_the_readout(self):
        svg = charts.line({'" onmouseover="x': series(1.0)})
        self.assertNotIn('onmouseover="x', svg)

    def test_two_bullets_in_one_card_say_which_is_which(self):
        """Two unlabelled bullets are two identical pictures of two different
        resources, and the reader has only their order to go on."""
        body = (charts.bullet(10.0, 70, 80, label="CPU")
                + charts.bullet(20.0, 70, 80, label="memory"))
        self.assertIn("CPU", body)
        self.assertIn("memory", body)
        self.assertEqual(body.count("bullet-scale"), 2)
        self.assertIn("0 → 100%", body)


class WindowTest(unittest.TestCase):
    """
    The measure window, on every card. A 5-minute rate and an hour's total are
    drawn identically; only this line separates them.
    """

    def test_the_window_is_read_out_of_the_query_not_typed_beside_it(self):
        self.assertEqual(shape._window("", "increase(node_vmstat_oom_kill[3h])"),
                         "totalled over 3h")
        self.assertEqual(shape._window("now", "sum by (i) (rate(x[90s]))"),
                         "now · rate over 90s")
        # Two rates over the same window are one fact, not two.
        self.assertEqual(shape._window("", "rate(a[5m]) / rate(b[5m])"),
                         "rate over 5m")
        # A bare gauge has no lookback to report, and does not invent one.
        self.assertEqual(shape._window("now", "overseer_cluster_cpu_percent"),
                         "now")

    def test_every_card_states_the_span_it_measured_over(self):
        sections = shape.observability(
            lambda expr, minutes, step, label=None: {},
            lambda expr: None, charts)
        for section in sections:
            for card in section["cards"]:
                with self.subTest(card=f'{section["key"]}/{card["title"]}'):
                    self.assertTrue(card["window"])

    def test_a_current_reading_is_not_dressed_up_as_a_range(self):
        """
        Utilisation and cluster saturation are the newest sample, not the hour
        the charts beside them draw. Saying "last 60 min" over a single reading
        is the confusion this line exists to remove.
        """
        sections = shape.observability(
            lambda expr, minutes, step, label=None: {},
            lambda expr: None, charts)
        cards = {(s["key"], c["title"]): c["window"]
                 for s in sections for c in s["cards"]}
        self.assertTrue(cards[("use", "Utilisation")].startswith(shape.LATEST_SPAN))
        self.assertEqual(cards[("golden", "Saturation")], shape.LATEST_SPAN)
        self.assertTrue(cards[("red", "Duration")].startswith(shape.RANGE_SPAN))


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


class LogFilterTest(unittest.TestCase):
    """
    Narrowing the Logs tab.

    The property that matters most is where the narrowing HAPPENS: it becomes a
    LogQL line filter, so Loki searches everything it has retained. Filtering
    the answer instead would only narrow the two hundred lines already on
    screen, and the line anyone is hunting is usually not among them.
    """

    def test_an_empty_filter_changes_the_query_not_at_all(self):
        f = shape.LogFilter()
        self.assertFalse(f.active)
        self.assertEqual(f.logql(), "")
        self.assertTrue(f.matches("anything at all"))

    def test_text_becomes_a_line_filter_loki_can_run(self):
        f = shape.LogFilter(contains="timeout", excludes="/healthz")
        self.assertEqual(f.logql(), " |= `timeout` != `/healthz`")

    def test_regex_switches_the_operators_rather_than_quoting_the_pattern(self):
        f = shape.LogFilter(contains="tim(e|)out", excludes="health.*", regex=True)
        self.assertEqual(f.logql(), " |~ `tim(e|)out` !~ `health.*`")

    def test_a_level_filter_uses_the_same_patterns_that_colour_the_line(self):
        """
        If the filter and the colour disagreed, "errors only" could hide a line
        the page had just drawn in red — which is the sort of thing that costs
        an hour before anyone doubts the tool.
        """
        expression = shape.LogFilter(level="err").logql()
        for name, source in shape.LEVEL_PATTERNS:
            if name == "err":
                self.assertIn(source, expression)

    def test_warnings_and_errors_never_hides_the_more_severe_one(self):
        """A severity filter that excluded the worse thing would be a trap."""
        f = shape.LogFilter(level="warn")
        self.assertTrue(f.matches("WARN slow"))
        self.assertTrue(f.matches("ERROR boom"))
        self.assertFalse(f.matches("INFO fine"))
        self.assertIn(dict(shape.LEVEL_PATTERNS)["err"], f.logql())

    def test_errors_only_really_means_only(self):
        f = shape.LogFilter(level="err")
        self.assertTrue(f.matches("ERROR boom"))
        self.assertFalse(f.matches("WARN slow"))

    def test_a_broken_regex_is_reported_and_never_run(self):
        f = shape.LogFilter(contains="a(", regex=True)
        self.assertTrue(f.problem)
        self.assertFalse(f.active)
        self.assertEqual(f.logql(), "")

    def test_a_backtick_is_refused_because_logql_cannot_quote_one(self):
        """
        LogQL raw strings are backtick-delimited and have no escape for a
        backtick. Dropping the character would silently search for something
        else, so this refuses instead.
        """
        f = shape.LogFilter(contains="a`b")
        self.assertTrue(f.problem)
        self.assertFalse(f.active)

    def test_the_python_side_agrees_with_the_query_it_would_have_sent(self):
        """
        The CLI fallback cannot be asked to filter, so the same object does it
        in Python. Both halves have to make the same decision or the two sources
        would show different logs for the same filter.
        """
        f = shape.LogFilter(contains="timeout", excludes="healthz", level="warn")
        self.assertTrue(f.matches("WARN upstream timeout"))
        self.assertFalse(f.matches("WARN /healthz timeout"))   # excluded
        self.assertFalse(f.matches("INFO upstream timeout"))   # wrong level
        self.assertFalse(f.matches("WARN upstream refused"))   # no match

    def test_a_regex_that_is_valid_matches_the_same_way_in_both(self):
        f = shape.LogFilter(contains=r"tenant=\d+", regex=True)
        self.assertTrue(f.matches("throttled tenant=9931"))
        self.assertFalse(f.matches("throttled tenant=acme"))
