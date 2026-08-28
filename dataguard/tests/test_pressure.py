"""
Tests for what the world is saying about a database, and how long it must say it.

    python3 -m unittest discover -s dataguard/tests -v

The two properties here are the ones that cost money or data when they regress:
a reading that is MISSING must not read as "empty", and a spike must not move a
database whose cheapest response takes an hour.
"""

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
import engines  # noqa: E402


class Component:
    def __init__(self, name="docs", kind="mongo"):
        self.name = name
        self.kind = kind


class NodeUsageTest(unittest.TestCase):
    def setUp(self):
        self._q = D.query.vm_query
        self._nodes = dict(D._lease_nodes)

    def tearDown(self):
        D.query.vm_query = self._q
        D._lease_nodes.clear()
        D._lease_nodes.update(self._nodes)

    def test_an_unmeasured_node_is_absent_not_empty(self):
        """
        Reading a missing series as zero would tell the loop a machine has room
        it does not have — and the room it is deciding about is disk.
        """
        D._lease_nodes.clear()
        D._lease_nodes["docs/2"] = {"hostname": "db-1", "state": "ready"}
        D.query.vm_query = lambda expr: None
        self.assertEqual(D.node_usage_by_component({"docs": Component()}), {})

    def test_the_worst_machine_wins_not_the_average(self):
        """
        One member out of disk is the problem whatever the others are doing.
        Averaging would hide it behind two healthy machines.
        """
        D._lease_nodes.clear()
        D._lease_nodes["docs/2"] = {"hostname": "db-1", "state": "ready"}
        D._lease_nodes["docs/3"] = {"hostname": "db-2", "state": "ready"}
        D.query.vm_query = lambda expr: 95.0 if "db-1" in expr else 5.0
        usage = D.node_usage_by_component({"docs": Component()})
        self.assertEqual(usage["docs"]["disk"], 95.0)

    def test_a_component_with_no_machine_of_its_own_is_not_reported(self):
        """It is on the master, and the master's numbers are everybody's."""
        D._lease_nodes.clear()
        D.query.vm_query = lambda expr: 50.0
        self.assertEqual(D.node_usage_by_component({"docs": Component()}), {})


class SustainTest(unittest.TestCase):
    def setUp(self):
        D._pressure_since.clear()

    def tearDown(self):
        D._pressure_since.clear()

    def test_a_spike_does_not_move_a_database(self):
        """
        The autoscaler's ninety seconds is right for a replica that starts in
        five. Here the cheapest response is an initial sync, so nothing reacts
        until the condition has held for the whole window.
        """
        now = 1_000_000.0
        self.assertFalse(D._sustained("docs", "capacity", True, now))
        self.assertFalse(D._sustained("docs", "capacity", True,
                                      now + D.PRESSURE_SUSTAIN_SECONDS - 1))

    def test_a_condition_that_holds_for_the_window_counts(self):
        now = 1_000_000.0
        D._sustained("docs", "capacity", True, now)
        self.assertTrue(D._sustained("docs", "capacity", True,
                                     now + D.PRESSURE_SUSTAIN_SECONDS))

    def test_the_clock_restarts_the_moment_it_stops_being_true(self):
        now = 1_000_000.0
        D._sustained("docs", "capacity", True, now)
        D._sustained("docs", "capacity", False, now + 100)
        D._sustained("docs", "capacity", True, now + 200)
        self.assertFalse(D._sustained("docs", "capacity", True,
                                      now + D.PRESSURE_SUSTAIN_SECONDS))


class ReadWriteSplitTest(unittest.TestCase):
    """
    Which half is slow decides between two OPPOSITE actions: more replicas for
    reads, a bigger machine for writes. Getting it wrong is not a smaller
    version of getting it right.
    """

    def split(self, reads, writes):
        engine = types.SimpleNamespace(op_latencies=lambda: (reads, writes))
        return D._read_write_split(Component(), engine)

    def test_slower_reads_say_reads(self):
        self.assertEqual(self.split(900.0, 100.0), (True, False))

    def test_slower_writes_say_writes(self):
        self.assertEqual(self.split(100.0, 900.0), (False, True))

    def test_unknown_is_neither(self):
        """
        Guessing "reads" for a database whose accounting could not be read would
        add a replica to a write-bound set — which cannot help, and costs a
        machine to find out.
        """
        self.assertEqual(self.split(None, None), (False, False))
        self.assertEqual(self.split(0.0, 0.0), (False, False))

    def test_an_engine_that_cannot_answer_is_neither(self):
        """Redis has no equivalent accounting, and says so by not having it."""
        self.assertEqual(
            D._read_write_split(Component(kind="redis"), engines.RedisEngine(None, "x")),
            (False, False))


if __name__ == "__main__":
    unittest.main()


class QuietWindowTest(unittest.TestCase):
    """
    What it takes before a database is allowed to give a member back.

    Both of these are about the same accident, found by driving thirty simulated
    days of an ordinary office traffic pattern through `read_pressure`: a
    database busy 09:00-17:00 bought a machine every morning, copied its whole
    dataset into it, and dropped it again every evening. Thirty machines and
    thirty initial syncs to end each night exactly where it started.
    """

    def setUp(self):
        D._pressure_since.clear()
        self.c = Component()

    def tearDown(self):
        D._pressure_since.clear()

    def quiet_after(self, seconds, busy_for=0.0, blip_at=None):
        """
        Run the real `read_pressure` over a stretch of calm and report whether
        it ever declared the component quiet. `blip_at` puts one loud sample in
        the middle of it.
        """
        now, saw = 1_000_000.0, False
        end = now + busy_for + seconds
        loud_until = now + busy_for
        while now <= end:
            tight = now < loud_until or (blip_at is not None
                                         and abs(now - (1_000_000.0 + blip_at)) < 1.0)
            usage = {self.c.name: {"cpu": 95.0 if tight else 5.0,
                                   "memory": 5.0, "disk": 5.0}}
            p = D.read_pressure(self.c, None, {}, usage, now)
            saw = saw or p["quiet"]
            now += 60.0
        return saw

    def test_quiet_outlasts_a_daily_cycle(self):
        """
        The window has to be longer than the quiet half of a day, or a load that
        comes back every morning is shed every night and rebuilt every morning.
        Sixteen hours is the overnight lull of a 09:00-17:00 workload.
        """
        self.assertFalse(self.quiet_after(16 * 3600))
        self.assertTrue(self.quiet_after(25 * 3600))

    def test_one_loud_sample_does_not_reset_the_whole_window(self):
        """
        A backup, a log rotation, a cron job: two minutes at 80% on one node, at
        03:00, once. Reading that as "this database still needs its extra
        member" would restart a day-long clock, and a database that blips once a
        night could then never shrink at all. Only a SUSTAINED signal counts,
        and one sample cannot make one.
        """
        self.assertTrue(self.quiet_after(25 * 3600, blip_at=12 * 3600))

    def test_pressure_that_really_holds_does_still_cancel_quiet(self):
        """The blip tolerance must not become a deaf ear."""
        self.assertFalse(self.quiet_after(4 * 3600, busy_for=2 * 3600))
