"""
The alert rules in `config/alerts.yml` against the gauges this process exports.

An alert is the only part of this system that has to be RIGHT while nothing is
happening, and the only part with no unit under it: vmalert reports a rule that
matches nothing as `inactive` and `health: ok`, which is also what a healthy
cluster looks like. A rule can therefore be incapable of firing for months and
every dashboard will say it is fine.

Two ways that has happened here, both caught by this file:

  * a JOIN LABEL that no longer exists on the gauge it joins against, so the
    match silently drops every series;
  * PROMQL PRECEDENCE, which is what actually happened. `*` binds tighter than
    `==`, so `a == 1 * on (...) group_left (...) b` parses as
    `a == (1 * on (...) group_left (...) b)` and returns nothing, ever.
    `ThrottledByNamedDependency` shipped like that and could not fire — while
    the vaguer rule beside it is suppressed EXACTLY when the named one should
    speak. The result was a service half an hour over its SLO, its cause fully
    diagnosed and published, and total silence.
"""
import os
import re
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(1, _ROOT)

os.environ.setdefault("HCLOUD_TOKEN", "test-token")
os.environ.setdefault("APP_NAME", "testcluster")

import docker  # noqa: E402

docker.DockerClient = lambda *a, **kw: types.SimpleNamespace(  # noqa: E731
    api=types.SimpleNamespace(), nodes=types.SimpleNamespace(),
    services=types.SimpleNamespace(), swarm=types.SimpleNamespace(),
    info=lambda: {},
)

import overseer as D  # noqa: E402

ALERTS = os.path.join(_ROOT, "config", "alerts.yml")

#: A YAML comment line. Stripped before anything is matched, because the prose
#: beside a rule QUOTES the broken form in order to explain it, and a checker
#: that reads its own explanation as a violation is a checker nobody keeps.
_COMMENT = re.compile(r"^\s*#")

#: A comparison against a literal, immediately followed by an arithmetic vector
#: match. Always a precedence bug: the arithmetic binds first and the
#: comparison is left comparing against a scalar-to-vector match that yields
#: nothing. The fix is always the same — parenthesise the comparison.
_UNPARENTHESISED = re.compile(
    r"(==|!=|>=|<=|>|<)\s*-?\d+(?:\.\d+)?\s*[*/+-]\s*(?:on|ignoring)\b")


def _text():
    with open(ALERTS) as handle:
        return handle.read()


def rule_text(name):
    """
    Everything written under one `- alert:`, comments removed.

    READ AS TEXT, NOT PARSED. `yaml` is not in the overseer image and has no
    business being added to it: the suite runs INSIDE that image precisely so a
    module the runtime does not have fails here, and a test-only dependency in
    `requirements.txt` is the one thing that would blunt that. Nothing below
    needs structure — these are substring and regex questions about an
    expression — so the file is sliced between rule headers instead.

    A missing or renamed rule raises rather than quietly checking nothing.
    """
    body = _text().split(f"- alert: {name}\n", 1)[1].split("- alert: ", 1)[0]
    return "\n".join(line for line in body.splitlines()
                      if not _COMMENT.match(line))


def rules():
    """(name, everything under it, comments removed) for every alert."""
    for chunk in _text().split("- alert: ")[1:]:
        body = chunk.split("- alert: ", 1)[0].splitlines()
        yield body[0].strip(), "\n".join(
            line for line in body[1:] if not _COMMENT.match(line))


class PrecedenceTest(unittest.TestCase):
    def test_no_rule_compares_before_it_joins(self):
        for name, expr in rules():
            with self.subTest(alert=name):
                self.assertIsNone(
                    _UNPARENTHESISED.search(expr),
                    f"{name} compares against a literal and then vector-matches "
                    f"on the result. `*` binds tighter than `==`, so this rule "
                    f"can never fire. Wrap the comparison: (a == 1) * on (...)")

    def test_the_guard_flags_the_expression_that_shipped(self):
        # A guard nobody has seen reject anything is a guard nobody knows
        # works. This is the rule exactly as it was written, verbatim.
        self.assertIsNotNone(_UNPARENTHESISED.search(
            "overseer_signal_unowned == 1\n"
            "  * on (service, cause) group_left (target)\n"
            "    overseer_service_dependency\n"))

    def test_the_guard_leaves_a_correct_rule_alone(self):
        self.assertIsNone(_UNPARENTHESISED.search(
            "(overseer_signal_unowned == 1)\n"
            "  * on (service, cause) group_left (target)\n"
            "    overseer_service_dependency\n"))


class JoinLabelTest(unittest.TestCase):
    """
    Every label an alert joins the dependency gauges on must exist on them.

    The gauges are read from the process rather than retyped here, so renaming
    a label fails this test instead of quietly emptying the alert.
    """

    def expr(self, name):
        return rule_text(name)

    def test_the_named_dependency_alert_joins_on_labels_that_exist(self):
        expr = self.expr("ThrottledByNamedDependency")
        for label in ("service", "cause"):
            self.assertIn(label, D.G_UNOWNED._labelnames)
            self.assertIn(label, D.G_TARGET._labelnames)
        self.assertIn("target", D.G_TARGET._labelnames)
        self.assertIn("on (service, cause)", expr)
        self.assertIn("group_left (target)", expr)

    def test_the_two_rules_cannot_both_fire_for_one_service(self):
        # The vague one exists so nothing goes unreported; the named one is the
        # useful page. Firing both means the reader gets the specific alert
        # beside a vaguer copy of itself, which is how the useful one is
        # learned to be ignored.
        unowned = self.expr("ThrottledByUnownedDependency")
        self.assertIn("unless on (service, cause)", unowned)
        self.assertIn("overseer_service_dependency", unowned)


if __name__ == "__main__":
    unittest.main()
