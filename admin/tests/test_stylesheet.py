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
        # Not anchored to the line start: tokens are declared several to a line.
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", self.code))
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
        for path in self.chip_templates():
            body = path.read_text()
            with self.subTest(template=path.name):
                self.assertLess(body.index("dot dot-"), body.index("slot-chart"))
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

    def test_disk_has_no_reservation_tick(self):
        # Swarm has no disk reservation, so a tick there would be a mark at a
        # number nobody set.
        css = CSS.read_text()
        self.assertIn(".band-disk::after { content: none; }", css)


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
