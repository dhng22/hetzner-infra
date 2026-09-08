"""
Structural checks on the stylesheet and the templates.

    python3 -m unittest discover -s admin/tests -v

This exists because of a real defect that survived for months: a comment block
was closed early, leaving eight lines of prose sitting at the top level of the
file. CSS parsed them as the start of a selector, which swallowed the entire
`:root` block that followed — so the dark series palette was silently dropped
and only looked right because another rule happened to supply the same values.

Nothing about that is visible in a browser, in a diff, or in a screenshot. It is
visible to a parser, which is what this is.
"""

import os
import pathlib
import re
import unittest

ADMIN = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSS = ADMIN / "static" / "style.css"
TEMPLATES = ADMIN / "templates"


def strip_comments(text):
    """Remove /* ... */, and report any that never close."""
    out, i, unclosed = [], 0, 0
    while True:
        start = text.find("/*", i)
        if start == -1:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = text.find("*/", start + 2)
        if end == -1:
            unclosed += 1
            break
        i = end + 2
    return "".join(out), unclosed


class StylesheetTest(unittest.TestCase):
    def setUp(self):
        self.raw = CSS.read_text()
        self.code, self.unclosed = strip_comments(self.raw)

    def test_every_comment_closes(self):
        self.assertEqual(self.unclosed, 0, "a /* is never closed")

    def test_no_stray_comment_terminator(self):
        """
        A `*/` with no opener is the exact shape of the bug this file exists
        for: the text before it is read as a selector, and the next rule's body
        is consumed as part of it.
        """
        self.assertNotIn("*/", self.code,
                         "a */ appears outside a comment — everything before it is "
                         "being parsed as a selector")

    def test_braces_balance(self):
        depth = 0
        for line_no, line in enumerate(self.code.splitlines(), 1):
            depth += line.count("{") - line.count("}")
            self.assertGreaterEqual(depth, 0, f"unbalanced }} at line ~{line_no}")
        self.assertEqual(depth, 0, "unbalanced { at end of file")

    def test_no_prose_at_the_top_level(self):
        """
        Every top-level construct must be a rule, an at-rule, or blank. Prose
        that escaped a comment reads as a selector and takes the next block
        with it.
        """
        for chunk in self.code.split("}"):
            head = chunk.split("{")[0].strip()
            if not head or head.startswith("@"):
                continue
            # A selector is punctuation and identifiers. Two consecutive words
            # separated by a space is a descendant selector; a sentence is not.
            self.assertLess(
                len(head.split()), 12,
                f"this does not look like a selector: {head[:70]!r}")
            self.assertNotIn(".  ", head)

    def test_every_custom_property_used_is_defined(self):
        """
        A `var(--typo)` is silent: the declaration is dropped and the element
        renders at whatever it inherited.

        Set ANYWHERE the panel sets one, not only in the stylesheet. The chart's
        five properties are supplied per element — inline by the templates, and
        by `app.js` on every repaint — because their whole job is to differ per
        node and per task. Reading the templates and the script too keeps the
        typo check and adds one: a property the stylesheet draws with and
        nothing anywhere fills in still fails.
        """
        # Not anchored to the line start: tokens are declared several to a line.
        sources = [self.code, (ADMIN / "static" / "app.js").read_text()]
        sources += [p.read_text() for p in TEMPLATES.glob("*.html")]
        defined = set()
        for text in sources:
            defined |= set(re.findall(r"(--[a-z0-9-]+)\s*:", text))
            defined |= set(re.findall(r'setProperty\(\s*"(--[a-z0-9-]+)"', text))
        used = set(re.findall(r"var\((--[a-z0-9-]+)", self.code))
        self.assertEqual(used - defined, set(), "undefined custom properties")

    def test_both_themes_define_the_same_tokens(self):
        """
        A token defined in one theme and not the other is invisible until
        someone switches, which is the worst time to find out.
        """
        blocks = {}
        for name, pattern in [
            ("light-media", r"@media \(prefers-color-scheme: light\) \{\s*:root \{(.*?)\}"),
            ("light-attr", r":root\[data-theme=\"light\"\] \{(.*?)\}"),
            ("dark-attr", r":root\[data-theme=\"dark\"\] \{(.*?)\}"),
            ("light-body", r"body\.theme-light \{(.*?)\}"),
            ("dark-body", r"body\.theme-dark \{(.*?)\}"),
        ]:
            match = re.search(pattern, self.code, re.S)
            self.assertIsNotNone(match, f"{name} theme block is missing")
            blocks[name] = set(re.findall(r"(--[a-z0-9-]+)\s*:", match.group(1)))

        reference = blocks["light-attr"]
        for name, tokens in blocks.items():
            self.assertEqual(tokens, reference,
                             f"{name} defines a different token set than light-attr")

    def test_hidden_beats_display(self):
        """
        `.kv-grid` is display:grid, which outranks the UA's [hidden] rule, so
        the environment editor's row view would stay visible in text mode
        without an explicit author rule.
        """
        self.assertIn("[hidden] { display: none !important; }", self.code)


class TaskChipTest(unittest.TestCase):
    """
    The chip's chart is rendered TWICE — by Jinja on first paint and by
    `app.js` on every poll after it — and nothing in either half fails when
    they disagree. The chart simply stops moving, or stops existing, five
    seconds after the page loads, which reads as "the panel is broken" and is
    invisible to every other test here.

    So the two are pinned against each other: same five custom properties, same
    three bands, same signature fields in the same order.
    """

    JS = ADMIN / "static" / "app.js"
    PROPS = ("--cpu", "--mem", "--cpu-use", "--mem-use", "--disk-use")
    BANDS = ("band-cpu", "band-mem", "band-disk")
    #: What `sig()` hashes, in order. A field the server leaves out is a chip
    #: that never repaints when only that field changed.
    SIG = ("name", "key", "tone", "cpu_share", "mem_share",
           "cpu_used", "mem_used", "disk_used")

    def chip_templates(self):
        return [TEMPLATES / "_overview.html", TEMPLATES / "_map.html"]

    def test_both_templates_draw_the_whole_chart(self):
        for path in self.chip_templates():
            body = path.read_text()
            with self.subTest(template=path.name):
                for prop in self.PROPS:
                    self.assertIn(f"{prop}:", body)
                for band in self.BANDS:
                    self.assertIn(band, body)

    def test_the_poller_sets_the_same_properties_the_server_does(self):
        js = self.JS.read_text()
        for prop in self.PROPS:
            self.assertIn(f'"{prop}"', js,
                          f"app.js never sets {prop}, so it vanishes on the "
                          f"first repaint")
        for band in self.BANDS:
            self.assertIn(band.split("-")[1], js)

    def test_the_signature_lists_the_same_fields_on_both_sides(self):
        js = self.JS.read_text()
        sig = re.search(r"function sig\(tasks\) \{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(sig, "sig() moved; this test cannot check it")
        rendered = (TEMPLATES / "_overview.html").read_text()
        marker = re.search(r'data-sig="(.*?)"', rendered, re.S)
        self.assertIsNotNone(marker)
        for field in self.SIG:
            self.assertIn(f"t.{field}", sig.group(1),
                          f"sig() ignores {field}")
            self.assertIn(f"t.{field}", marker.group(1),
                          f"data-sig omits {field}, so the first tick rebuilds "
                          f"every chip on an unchanged cluster")

    def test_the_chart_comes_after_the_name_in_both_renderers(self):
        """
        `margin-left: auto` only pushes the chart to the end of the chip if it
        is the LAST child. Build it before the name and it lands on the left,
        on the poller's chips only — so the page looks right until the first
        tick and wrong forever after.
        """
        # PER LINE, because `slot-chart` is no longer unique in either file —
        # a node card carries the same three bars for the machine itself. The
        # chip is the line that has the dot on it.
        for path in self.chip_templates():
            with self.subTest(template=path.name):
                chip = [l for l in path.read_text().splitlines() if "dot dot-" in l]
                self.assertEqual(len(chip), 1, "the chip moved; this cannot check it")
                self.assertLess(chip[0].index("dot dot-"), chip[0].index("slot-chart"))
        js = self.JS.read_text()
        build = re.search(r"function buildSlot\(t\) \{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(build)
        appends = re.findall(r"el\.appendChild\((\w+)", build.group(1))
        self.assertEqual(appends[-1], "chart", "the chart is not appended last")

    def test_the_chart_is_beside_the_name_and_takes_no_pointer_events(self):
        # The chart is pushed to the chip's end and takes no pointer events —
        # the chip's own tooltip has to win over it.
        css = CSS.read_text()
        block = re.search(r"\.slot-chart \{(.*?)\}", css, re.S)
        self.assertIsNotNone(block)
        self.assertIn("margin-left: auto", block.group(1))
        self.assertIn("pointer-events: none", block.group(1))

    def test_the_disk_line_says_how_much_disk_not_that_there_is_no_number(self):
        """
        CPU and memory each print an absolute beside their percentage; disk
        printed "no reservation", which is a fact about Swarm and not about this
        machine. `disk_used_gb` is derived once in `topology()` so the two
        templates and the poller cannot disagree about it.
        """
        for name in ("_overview.html", "_cluster.html"):
            body = (TEMPLATES / name).read_text()
            with self.subTest(template=name):
                self.assertNotIn("no reservation", body)
                self.assertIn("n.disk_used_gb", body)
        js = (ADMIN / "static" / "app.js").read_text()
        self.assertNotIn("no reservation", js)
        self.assertIn("disk_used_gb", js)

    def test_an_unmeasured_disk_reads_as_unmeasured_and_not_as_empty(self):
        """
        Docker 29's `overlayfs` driver falls through cadvisor's `FsStats` switch
        to `default: return nil`, at every released version and on master — so
        `container_fs_usage_bytes` carries no container labels at all and the
        disk band would sit at the floor forever. A bar at zero is a reading;
        this one is not, and the difference has to survive both renderers.
        """
        for path in self.chip_templates():
            body = path.read_text()
            with self.subTest(template=path.name):
                self.assertIn("t.disk_used is none", body)
                self.assertIn("not measured", body)
                # It still has to be drawn somewhere, and the floor is the only
                # honest place — `--disk-use:None` would break the whole chart.
                self.assertIn("--disk-use:{{ t.disk_used or 0 }}", body)
        js = self.JS.read_text()
        self.assertIn("t.disk_used == null", js)
        self.assertIn("not measured", js)

    def test_disk_has_no_reservation_tick(self):
        # Swarm has no disk reservation, so a tick there would be a mark at a
        # number nobody set.
        css = CSS.read_text()
        self.assertIn(".band-disk::after { content: none; }", css)

    def test_the_bars_are_the_only_way_capacity_is_drawn(self):
        """
        ONE IDIOM FOR ONE QUESTION.

        Reservation used to be drawn twice: these bars on a task, and a pair of
        conic-gradient rings on a node card and a component card. A ring shows a
        number; it cannot be compared with anything, and "is this reservation
        the right size" is nothing but a comparison. Both idioms in one map also
        meant two legends for one fact.
        """
        # Comments stripped first: prose is allowed to say what a thing used to
        # be, and this test is about what the browser is handed.
        css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
        self.assertNotIn("conic-gradient", css)
        for gone in (".gauge", "has-gauges", "--info"):
            self.assertNotIn(gone, css, f"{gone} survived the rings")
        for path in sorted(TEMPLATES.glob("*.html")):
            body = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)
            with self.subTest(template=path.name):
                self.assertNotIn("gauge", body)

    def test_every_surface_that_draws_capacity_draws_the_whole_chart(self):
        """
        A task, the node it runs on and the component it belongs to each get the
        same three bars with the same five properties. None of them may show a
        subset: a reservation on its own cannot tell 640MB held for a cache
        using 39MB apart from 640MB held for one using 600MB, and those are
        opposite situations.
        """
        for name in ("_overview.html", "_cluster.html", "_components_live.html"):
            body = (TEMPLATES / name).read_text()
            with self.subTest(template=name):
                self.assertIn("slot-chart", body)
                for band in self.BANDS:
                    self.assertIn(band, body)
                for prop in self.PROPS:
                    self.assertIn(f"{prop}:", body)

    def test_a_chip_is_as_wide_as_its_own_name(self):
        """
        Uniform tracks made every chip as wide as the widest name on the node,
        and the chart is pushed to the chip's end — so `api` rendered as three
        characters, a gap the width of `victoriametrics`, and then the bars.
        """
        css = CSS.read_text()
        block = re.search(r"\.slots \{(.*?)\}", css, re.S)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("flex-wrap: wrap", body)
        self.assertNotIn("grid-template-columns", body)
        # A wrapping row needs something to wrap AT, and the branch holding it
        # must be allowed to be that wide.
        self.assertIn("max-width: 400px", body)
        self.assertIn(".tree-branch:has(.slots) { flex: 1 0 auto; }", css)

    def test_the_tasks_hang_off_the_card_they_run_on(self):
        """
        A FAN, AND NOTHING PSEUDO-ELEMENTS CAN DRAW.

        Every other rank of this tree is one parent over a row of children, so a
        bus with a tick per child draws it in CSS. The tasks wrap onto several
        rows, and a bus per row produced a ladder of stubs joining each row to
        the one above rather than any of them to the card. Straight lines from
        the middle of the card to the middle of each chip need measured
        positions, so the markup carries an empty `<svg>` and `app.js` fills it.
        """
        css = CSS.read_text()
        block = re.search(r"\.slots \{(.*?)\}", css, re.S).group(1)
        self.assertIn("justify-content: center", block)
        self.assertNotIn("padding-left", block)
        # The comb is gone; nothing draws a connector out of a chip any more.
        self.assertNotIn(".slots > .slot::before", css)
        self.assertNotIn(".slots::after", css)
        # Under the chips, and taking no clicks — a chip is a link now.
        links = re.search(r"\.slot-links \{(.*?)\}", css, re.S).group(1)
        self.assertIn("z-index: 0", links)
        self.assertIn("pointer-events: none", links)
        self.assertIn(".tree-branch > .tnode, .tree-branch > .slots "
                      "{ position: relative; z-index: 1; }", css)
        for path in self.chip_templates():
            with self.subTest(template=path.name):
                self.assertIn("data-links", path.read_text())
        js = self.JS.read_text()
        self.assertIn("function drawLinks", js)
        # Re-measured whenever the chips can have moved, or the lines point at
        # where they used to be.
        for trigger in ('window.addEventListener("resize", redrawLinks)',
                        "document.fonts.ready.then(redrawLinks)"):
            self.assertIn(trigger, js)
        paint = re.search(r"function paintTopology\(data\) \{(.*?)\n  \}",
                          js, re.S)
        self.assertIsNotNone(paint)
        self.assertIn("redrawLinks()", paint.group(1),
                      "the chips are rebuilt and the lines are not redrawn")

    def test_a_task_chip_is_a_link_and_builds_no_url_of_its_own(self):
        """
        A task was the one thing on the map you could point at and not open.
        A component's chip goes to its page; the infrastructure stacks have no
        page, so theirs go to the list that describes them.

        `app.js` composes neither URL. Both are rendered by the same helpers the
        server-side chips use and handed over on the map's own element, which is
        the rule the templates already follow.
        """
        body = (TEMPLATES / "_overview.html").read_text()
        self.assertIn("component_href(t.component) if t.component "
                      "else components_href()", body)
        self.assertIn('data-href-component="{{ component_href(\'__NAME__\') }}"', body)
        self.assertIn('data-href-components="{{ components_href() }}"', body)
        js = self.JS.read_text()
        self.assertIn('document.createElement("a")', js)
        self.assertNotIn("/components/", js, "app.js is building a URL by hand")

    def test_a_node_card_says_nothing_the_bars_already_say(self):
        """
        The three percentages above the chart were the chart in words, minus
        the reservation it is drawn against.
        """
        for name in ("_overview.html", "_cluster.html"):
            with self.subTest(template=name):
                self.assertNotIn("tnode-meters", (TEMPLATES / name).read_text())
        self.assertNotIn(".tnode-meters", CSS.read_text())

    def test_hosts_that_do_not_fit_start_at_the_left_edge(self):
        """
        `justify-content: center` overflows equally off both edges, and the half
        that goes off the left cannot be scrolled to — a scroll offset is never
        negative. Once a branch is as wide as its tasks want, four hosts stop
        fitting routinely, and the first one disappears.
        """
        self.assertIn("justify-content: safe center", CSS.read_text())


class DividedBarTest(unittest.TestCase):
    def test_the_overrun_hatch_is_an_overlay_and_not_on_the_segments(self):
        """
        Each segment carries its colour as a `background:` SHORTHAND in an
        inline style, which resets `background-image` at a specificity no
        stylesheet rule can reach. A hatch declared on `.split-bar.is-over > i`
        is therefore discarded in silence and the bar renders as an ordinary
        one — which is exactly the reading it exists to contradict.
        """
        css = CSS.read_text()
        self.assertIn(".split-bar.is-over::after", css)
        self.assertNotIn(".split-bar.is-over > i", css)
        block = re.search(r"\.split-bar\.is-over::after \{(.*?)\}", css, re.S)
        self.assertIsNotNone(block)
        # It covers the segments, so it must not swallow their tooltips.
        self.assertIn("pointer-events: none", block.group(1))
        self.assertIn("repeating-linear-gradient", block.group(1))


class ServiceRowTest(unittest.TestCase):
    """
    `.row` is shared by five templates that put different things in it, so it
    must not care how many things there are.
    """

    def test_the_row_does_not_fix_how_many_children_it_has(self):
        """
        THE PILL THAT FELL THROUGH THE FLOOR.

        `grid-template-columns: 3px 1fr auto auto auto` gave five tracks. An
        infrastructure service with somewhere to send you — an Open button, or a
        URL it prints because you cannot reach it from here — has six children,
        and the sixth wrapped onto an implicit second row, under the stripe. So
        Grafana and VictoriaMetrics showed their state pill below the name while
        Loki, which has neither, showed it in line. The tracks never aligned
        between rows in the first place: each `.row` is its own grid.
        """
        block = re.search(r"\n\.row \{(.*?)\}", CSS.read_text(), re.S)
        self.assertIsNotNone(block)
        self.assertNotIn("grid", block.group(1))
        self.assertIn("display: flex", block.group(1))

    def test_the_name_takes_the_slack_and_the_stripe_does_not(self):
        css = CSS.read_text()
        self.assertIn(".row > * { flex: none; }", css)
        self.assertIn(".row > .stripe + * { flex: 1 1 auto; min-width: 0; }", css)

    def test_a_row_without_a_stripe_still_pushes_its_tail_to_the_end(self):
        """
        Not every row has a stripe — dataguard's refusal counts are a label and
        a number — and those rely on `.spacer`, which was scoped to two named
        containers and therefore did nothing inside a row.
        """
        css = CSS.read_text()
        self.assertIn(".spacer { margin-left: auto; }", css)
        self.assertNotIn(".panel-head .spacer", css)
        self.assertNotIn(".page-head .spacer", css)


class TemplateTest(unittest.TestCase):
    def templates(self):
        return sorted(TEMPLATES.glob("*.html"))

    def test_no_duplicate_class_attributes(self):
        """
        `<div class="a" class="b">` silently drops the second one.

        Counted per TAG, not per line: two elements on one line each carrying a
        class is normal and correct, and a line-based check calls it a bug.
        """
        for path in self.templates():
            body = path.read_text()
            for tag in re.findall(r"<[a-zA-Z][^>]*>", body, re.S):
                self.assertLessEqual(
                    len(re.findall(r'\bclass\s*=', tag)), 1,
                    f"{path.name}: two class attributes on {tag[:60]!r}")

    def test_every_page_wrapper_extends_base(self):
        for path in TEMPLATES.glob("page_*.html"):
            self.assertIn('{% extends "base.html" %}', path.read_text(), path.name)

    def test_partials_referenced_by_include_exist(self):
        for path in self.templates():
            for name in re.findall(r'{%\s*include\s+"([^"]+)"', path.read_text()):
                self.assertTrue((TEMPLATES / name).exists(),
                                f"{path.name} includes a missing {name}")

    def test_no_inline_font_sizes(self):
        """
        The type scale is a token set; an inline font-size is a value nobody
        else can see, and eleven of them are why the panel looked accidental.
        """
        for path in self.templates():
            body = path.read_text()
            self.assertNotIn("style=\"font-size", body, path.name)


if __name__ == "__main__":
    unittest.main()
