"""
Tests for snapshot retention — the only thing here that DELETES a backup.

    python3 -m unittest discover -s dataguard/tests -v

Everything else in dataguard can be wrong and cost a machine or an hour. This
can be wrong and cost the copy you were going to restore from, so most of these
are tests that it REFUSES: an unreadable list, a limit of zero, a failed delete
partway through. The happy path is one test; the rest are the brakes.
"""

import json
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

os.environ.setdefault("APP_NAME", "testcluster")

import docker  # noqa: E402

docker.DockerClient = lambda *a, **kw: types.SimpleNamespace(  # noqa: E731
    secrets=None, services=None, nodes=None, containers=None)

import dataguard as D  # noqa: E402


class Component:
    def __init__(self, keep=3, kind="mongo", name="docs"):
        self.name = name
        self.kind = kind
        self.max_snapshots = keep


def snapshots(*names):
    return json.dumps({"snapshots": [{"name": n, "restoreTo": n} for n in names]})


class Controller:
    """
    A stand-in for the pbm-ctl sidecar that records what it was asked to run.

    `list_out` is what `pbm list` answers; `fail_on` names a snapshot whose
    delete fails, because a delete that fails halfway is the case where "carry
    on with the next one" and "stop" are different amounts of data lost.
    """

    def __init__(self, list_out, ok=True, fail_on=None):
        self.list_out = list_out
        self.ok = ok
        self.fail_on = fail_on
        self.deleted = []
        self.calls = []

    def __call__(self, container, argv, timeout=1800):
        self.calls.append(argv)
        if argv[:2] == ["pbm", "list"]:
            return self.ok, self.list_out
        if argv[:2] == ["pbm", "delete-backup"]:
            name = argv[-1]
            if name == self.fail_on:
                return False, "storage unreachable"
            self.deleted.append(name)
            return True, ""
        return True, ""


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self._exec = D._exec
        self._dry = D.DRY_RUN

    def tearDown(self):
        D._exec = self._exec
        D.DRY_RUN = self._dry

    def prune(self, component, controller):
        D._exec = controller
        D.prune_snapshots(component, object())
        return controller

    def test_the_oldest_go_first_and_only_the_excess_goes(self):
        c = self.prune(Component(keep=3),
                       Controller(snapshots("2024-01-01T00:00:00Z",
                                            "2024-01-02T00:00:00Z",
                                            "2024-01-03T00:00:00Z",
                                            "2024-01-04T00:00:00Z",
                                            "2024-01-05T00:00:00Z")))
        self.assertEqual(["2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z"],
                         c.deleted)

    def test_the_list_order_is_not_trusted(self):
        """
        `pbm list` is not promised in any order, and "oldest" decided by
        position in a list is "whichever one it happened to print first".
        """
        c = self.prune(Component(keep=1),
                       Controller(snapshots("2024-03-01T00:00:00Z",
                                            "2024-01-01T00:00:00Z",
                                            "2024-02-01T00:00:00Z")))
        self.assertEqual(["2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z"],
                         c.deleted)

    def test_at_the_limit_nothing_is_deleted(self):
        c = self.prune(Component(keep=3),
                       Controller(snapshots("a", "b", "c")))
        self.assertEqual([], c.deleted)

    def test_below_the_limit_nothing_is_deleted(self):
        c = self.prune(Component(keep=7), Controller(snapshots("a", "b")))
        self.assertEqual([], c.deleted)

    def test_a_list_that_cannot_be_read_deletes_NOTHING(self):
        """
        The dangerous shape: `pbm list` fails, an empty list is inferred, and
        the code decides there is nothing to keep. Unreadable is not empty.
        """
        c = self.prune(Component(keep=1), Controller("", ok=False))
        self.assertEqual([], c.deleted)

    def test_unparseable_output_deletes_NOTHING(self):
        c = self.prune(Component(keep=1), Controller("not json at all"))
        self.assertEqual([], c.deleted)

    def test_an_empty_snapshot_list_deletes_nothing(self):
        c = self.prune(Component(keep=1), Controller(json.dumps({"snapshots": []})))
        self.assertEqual([], c.deleted)

    def test_a_limit_of_zero_is_not_read_as_delete_everything(self):
        """A missing or zero limit means "no retention", never "keep none"."""
        c = self.prune(Component(keep=0), Controller(snapshots("a", "b", "c")))
        self.assertEqual([], c.deleted)
        self.assertEqual([], c.calls)

    def test_a_failed_delete_stops_the_run(self):
        """
        Carrying on past a failure would keep deleting against storage that has
        already said no once, and the next call is just as likely to be a
        success that removes something. Stop; the next backup tries again.
        """
        c = self.prune(Component(keep=1),
                       Controller(snapshots("a", "b", "c", "d"),
                                  fail_on="b"))
        self.assertEqual(["a"], c.deleted)

    def test_dry_run_deletes_nothing(self):
        D.DRY_RUN = True
        c = self.prune(Component(keep=1), Controller(snapshots("a", "b", "c")))
        self.assertEqual([], c.deleted)

    def test_a_component_with_no_pbm_is_left_alone(self):
        """
        Redis has no backup controller rendered yet. Running mongo's commands
        against whatever container answered would be worse than doing nothing.
        """
        c = self.prune(Component(keep=1, kind="redis"), Controller(snapshots("a", "b")))
        self.assertEqual([], c.calls)


class LabelTest(unittest.TestCase):
    def test_the_retention_label_is_the_one_the_panel_writes(self):
        """
        Spelled out rather than built from `D.DG`, which would agree with itself
        whatever it said. The panel is the other end of this string and cannot
        be imported here; `test_dataguard_labels_agree_across_the_wire` in
        admin/tests is what checks both ends at once.
        """
        self.assertEqual("dataguard.max_snapshots", D.L_MAX_SNAPSHOTS)
