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
