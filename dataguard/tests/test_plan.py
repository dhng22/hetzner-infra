"""
Tests for when a database may change shape, and — mostly — when it may not.

    python3 -m unittest discover -s dataguard/tests -v

Every gate gets a test that it REFUSES, named for the accident it prevents.
That is the point of this file: the actions are the easy half, and a gate that
quietly stops working looks exactly like a gate that had nothing to refuse.
"""

import os
import sys
import time
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import engines  # noqa: E402
import plan  # noqa: E402


def member(host, state=engines.SECONDARY, lag=0.0, votes=1, priority=1.0, hidden=False):
    return engines.Member(name=host.split(":")[0], host=host, state=state,
                          lag_seconds=lag, votes=votes, priority=priority,
                          hidden=hidden)


def topology(*members, ok=True):
    primary = next((m for m in members if m.state == engines.PRIMARY), None)
    return engines.Topology(list(members), primary=primary, ok=ok)


class FakeComponent:
    """
    A component with the knobs the planner reads, and nothing else.

    Deliberately not the real class: that one reads Swarm labels, and a test
    that needs a docker client to ask "would this break the majority" is testing
    the wrong thing.
    """

    def __init__(self, **kw):
        self.name = kw.get("name", "docs")
        self.kind = "mongo"
        self.enabled = kw.get("enabled", True)
        self.dry_run = kw.get("dry_run", False)
        self.pool = kw.get("pool", 4)
        self.max_members = kw.get("max_members", 5)
        self.lag_budget_seconds = kw.get("lag_budget_seconds", 10.0)
        self.secondary_reads = kw.get("secondary_reads", True)
        self.backup_target = kw.get("backup_target", "s3")
        # NOT plan names, and the fake said otherwise long after the real
        # component stopped doing so. A fixture that has drifted from the class
        # it stands in for tests a component that no longer exists.
        self.node_type = ""
        self.bigger_node_type = "bigger"
        self.cooldown_seconds = kw.get("cooldown_seconds", 14400)
        self.backup_max_age_seconds = kw.get("backup_max_age_seconds", 86400)
        self.disk_headroom = 2.5
        self.last_change_at = kw.get("last_change_at", 0.0)

    @property
    def master_member(self):
        return f"{self.name}_mongo-1"

    @property
    def master_host(self):
        return f"{self.name}_mongo-1:27017"

    def member_host(self, index):
        return f"{self.name}_mongo-{index}:27017"


M1 = "docs_mongo-1:27017"
M2 = "docs_mongo-2:27017"
M3 = "docs_mongo-3:27017"
M4 = "docs_mongo-4:27017"

GROW = {"capacity": True, "latency": False, "quiet": False, "read": False, "write": False}
QUIET = {"capacity": False, "latency": False, "quiet": True, "read": False, "write": False}
CALM = {"capacity": False, "latency": False, "quiet": False, "read": False, "write": False}


class StateTest(unittest.TestCase):
    def test_one_member_on_the_master_is_state_one(self):
        c = FakeComponent()
        self.assertEqual(plan.current_state(c, topology(member(M1, engines.PRIMARY))),
                         plan.STATE_MASTER)

    def test_the_master_plus_one_machine_is_state_two(self):
        c = FakeComponent()
        self.assertEqual(
            plan.current_state(c, topology(member(M1), member(M2, engines.PRIMARY))),
            plan.STATE_PAIRED)

    def test_no_member_on_the_master_is_state_three(self):
        c = FakeComponent()
        self.assertEqual(
            plan.current_state(c, topology(member(M2, engines.PRIMARY), member(M3))),
            plan.STATE_DEDICATED)

    def test_an_unreachable_set_has_no_state_at_all(self):
        """
        Not "state 1". An unreachable database looks empty, and reading that as
        "one member, on the master" invites the machine to build the whole thing
        again on top of a database that is running perfectly well.
        """
        c = FakeComponent()
        self.assertIsNone(plan.current_state(c, topology(ok=False)))


class GateTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000.0
        self.c = FakeComponent(last_change_at=self.now - 99999)
        self.t = topology(member(M1, engines.PRIMARY), member(M2), member(M3))

    def gates(self, **kw):
        kw.setdefault("now", self.now)
        kw.setdefault("backup_age", 60.0)
        return plan.refusals(self.c, self.t, **kw)

    def test_a_healthy_component_has_no_gates(self):
        self.assertEqual(self.gates(), [])

    def test_an_unreachable_set_refuses_and_says_nothing_else(self):
        """
        Every other gate below reads a topology we cannot currently see, so
        reporting them too would be five reasons invented from no data.
        """
        self.t = topology(ok=False)
        self.assertEqual(self.gates(), ["unreachable"])

    def test_a_set_with_no_primary_is_refused(self):
        self.t = topology(member(M1), member(M2), member(M3))
        self.assertIn("no_primary", self.gates())

    def test_a_set_that_has_lost_its_majority_is_refused(self):
        self.t = topology(member(M1, engines.PRIMARY),
                          member(M2, engines.DOWN), member(M3, engines.DOWN))
        self.assertIn("no_majority", self.gates())

    def test_a_member_still_doing_its_initial_sync_is_refused(self):
        self.t = topology(member(M1, engines.PRIMARY), member(M2),
                          member(M3, engines.STARTUP))
        self.assertIn("member_syncing", self.gates())

    def test_only_one_initial_sync_may_run_in_the_whole_cluster(self):
        """
        A sync reads the entire dataset off a live member. Two at once turns a
        busy database into an unavailable one, and neither of them is the
        emergency.
        """
        self.assertIn("another_sync_in_flight", self.gates(syncing_elsewhere=True))

    def test_the_cooldown_is_hours_not_seconds(self):
        self.c.last_change_at = self.now - 60
        self.assertIn("cooldown", self.gates())

    def test_a_stale_backup_refuses_a_topology_change(self):
        """
        A topology change can lose data. Doing one without a recent VERIFIED
        backup is betting the database on the change going well.
        """
        self.assertIn("backup_stale", self.gates(backup_age=999_999))

    def test_a_component_that_asked_for_backups_and_has_none_is_refused(self):
        """
        Not the same as a stale one. No backup at all means the backup path is
        not working, and changing shape then is betting the database on the
        change going well.
        """
        self.assertIn("no_backup", self.gates(backup_age=None))

    def test_a_component_with_no_backup_target_is_not_blocked_by_backups(self):
        self.c.backup_target = ""
        self.assertEqual(self.gates(backup_age=None), [])

    def test_a_disk_that_cannot_hold_the_data_refuses(self):
        """
        CPU and memory are recoverable mistakes. A full disk on a database is
        not, so this is checked before anything is moved rather than after.
        """
        self.assertIn("insufficient_disk",
                      self.gates(disk_free_bytes=10 << 30, data_bytes=8 << 30))

    def test_enough_disk_with_headroom_passes(self):
        self.assertEqual(self.gates(disk_free_bytes=100 << 30, data_bytes=8 << 30), [])

    def test_disabled_and_dry_run_are_gates_like_any_other(self):
        self.c.enabled = False
        self.assertIn("disabled", self.gates())
        self.c.enabled, self.c.dry_run = True, True
        self.assertIn("dry_run", self.gates())


class MajorityTest(unittest.TestCase):
    def test_majority_counts_configured_members_not_survivors(self):
        """
        The arithmetic that decides whether writes are possible at all. Counting
        only the healthy members says a three-member set with two down is fine,
        which is exactly backwards.
        """
        t = topology(member(M1, engines.PRIMARY), member(M2, engines.DOWN),
                     member(M3, engines.DOWN))
        self.assertFalse(t.has_majority)

    def test_two_of_three_is_a_majority(self):
        t = topology(member(M1, engines.PRIMARY), member(M2), member(M3, engines.DOWN))
        self.assertTrue(t.has_majority)

    def test_removing_a_member_that_would_break_the_majority_is_refused(self):
        t = topology(member(M1, engines.PRIMARY), member(M2), member(M3, engines.DOWN))
        self.assertTrue(plan.would_break_majority(t, M2))

    def test_removing_a_member_the_set_can_spare_is_allowed(self):
        t = topology(member(M1, engines.PRIMARY), member(M2), member(M3))
        self.assertFalse(plan.would_break_majority(t, M3))

    def test_a_non_voting_engine_has_no_majority_to_break(self):
        """
        Redis replicas do not vote — the SENTINELS do, and they are a separate
        quorum. Applying the replica-set arithmetic to them would refuse every
        removal for a reason that does not exist.
        """
        t = topology(member(M1, engines.PRIMARY), member(M2))
        self.assertFalse(plan.would_break_majority(t, M2, voting=False))


class ActionTest(unittest.TestCase):
    def setUp(self):
        self.c = FakeComponent()

    def act(self, state, t, pressure):
        return plan.next_action(self.c, state, t, pressure)

    def test_growing_off_the_master_asks_for_one_machine(self):
        a = self.act(plan.STATE_MASTER, topology(member(M1, engines.PRIMARY)), GROW)
        self.assertEqual((a.verb, a.index), ("provision", 2))

    def test_a_set_of_two_is_grown_to_three_before_anything_else(self):
        """
        Two voting members cannot elect: either one going away leaves one, and
        one is not a majority of two. A pair is a transitional state, never a
        resting one.
        """
        t = topology(member(M1), member(M2, engines.PRIMARY))
        a = self.act(plan.STATE_PAIRED, t, GROW)
        self.assertEqual((a.verb, a.index), ("provision", 3))

    def test_the_master_is_only_dropped_once_three_others_are_healthy(self):
        t = topology(member(M1), member(M2, engines.PRIMARY), member(M3), member(M4))
        a = self.act(plan.STATE_PAIRED, t, GROW)
        self.assertEqual((a.verb, a.host), ("remove", self.c.master_host))

    def test_a_caught_up_member_is_enfranchised_before_anything_else(self):
        """
        The cheapest action there is, and always safe. Leaving a synced member
        non-voting leaves the set less resilient than it looks.
        """
        t = topology(member(M1, engines.PRIMARY), member(M2),
                     member(M3, lag=1.0, votes=0, priority=0))
        a = self.act(plan.STATE_DEDICATED, t, CALM)
        self.assertEqual((a.verb, a.host), ("enfranchise", M3))

    def test_a_member_that_is_still_behind_is_not_enfranchised(self):
        t = topology(member(M1, engines.PRIMARY), member(M2),
                     member(M3, lag=600.0, votes=0, priority=0))
        self.assertNotEqual(self.act(plan.STATE_DEDICATED, t, CALM).verb, "enfranchise")

    def test_a_lagging_member_is_taken_out_of_read_rotation(self):
        t = topology(member(M1, engines.PRIMARY), member(M2), member(M3, lag=60.0))
        a = self.act(plan.STATE_DEDICATED, t, CALM)
        self.assertEqual((a.verb, a.host), ("hide", M3))

    def test_a_recovered_member_gets_its_reads_back(self):
        t = topology(member(M1, engines.PRIMARY), member(M2),
                     member(M3, lag=1.0, hidden=True, priority=0))
        a = self.act(plan.STATE_DEDICATED, t, CALM)
        self.assertEqual((a.verb, a.host), ("unhide", M3))

    def test_read_latency_with_secondary_reads_off_refuses_to_pretend(self):
        """
        The honest core of the whole read-splitting story. Adding replicas
        cannot help reads that all go to the primary, and doing it anyway would
        look like an action while changing nothing a user could feel.
        """
        self.c.secondary_reads = False
        t = topology(member(M1, engines.PRIMARY), member(M2), member(M3))
        pressure = dict(CALM, latency=True, read=True)
        a = self.act(plan.STATE_DEDICATED, t, pressure)
        self.assertEqual(a.verb, "cannot_help")
        self.assertIn("secondary reads", a.reason)

    def test_read_latency_with_secondary_reads_on_adds_a_replica(self):
        t = topology(member(M1, engines.PRIMARY), member(M2), member(M3))
        a = self.act(plan.STATE_DEDICATED, t, dict(CALM, latency=True, read=True))
        self.assertEqual((a.verb, a.node_type), ("provision", self.c.node_type))

    def test_write_latency_asks_for_a_bigger_machine_not_more_of_them(self):
        """
        Every write goes to the primary whatever the topology is, so replicas
        cannot absorb them. Delivered as a member on a bigger node and then a
        promotion — a database is never power-cycled onto another plan.
        """
        t = topology(member(M1, engines.PRIMARY), member(M2), member(M3))
        a = self.act(plan.STATE_DEDICATED, t, dict(CALM, latency=True, write=True))
        self.assertEqual((a.verb, a.node_type, a.promote),
                         ("provision", self.c.bigger_node_type, True))

    def test_the_member_ceiling_is_honoured(self):
        self.c.max_members = 3
        t = topology(member(M1, engines.PRIMARY), member(M2), member(M3))
        a = self.act(plan.STATE_DEDICATED, t, dict(CALM, latency=True, read=True))
        self.assertEqual((a.verb, a.limit), ("at_ceiling", 3))

    def test_quiet_sheds_a_replica_but_never_the_primary(self):
        M5 = "docs_mongo-5:27017"
        t = topology(member(M2, engines.PRIMARY), member(M3, lag=5.0),
                     member(M4, lag=1.0), member(M5, lag=9.0))
        a = self.act(plan.STATE_DEDICATED, t, QUIET)
        self.assertEqual(a.verb, "remove")
        self.assertNotEqual(a.host, M2)
        # The laggiest goes: it contributes least, and its removal is the one
        # least likely to be noticed.
        self.assertEqual(a.host, M5)

    def test_quiet_at_three_members_returns_to_the_master_rather_than_to_two(self):
        """
        A three-member set cannot shed one and stay electable — two voting
        members have no majority either can reach alone. So the way DOWN from
        three is to put a member back on the master, not to drop to two.
        """
        t = topology(member(M2, engines.PRIMARY), member(M3), member(M4))
        a = self.act(plan.STATE_DEDICATED, t, QUIET)
        self.assertEqual((a.verb, a.index, a.on_master), ("provision", 1, True))

    def test_calm_does_nothing_at_all(self):
        t = topology(member(M1, engines.PRIMARY), member(M2), member(M3))
        self.assertEqual(self.act(plan.STATE_DEDICATED, t, CALM).verb, "hold")


if __name__ == "__main__":
    unittest.main()


SMALL = (4, 8 * 1024 ** 3)
BIG = (8, 16 * 1024 ** 3)

WRITE = {"capacity": False, "latency": True, "quiet": False, "read": False,
         "write": True}


class SlotTest(unittest.TestCase):
    """
    A member index is a SLOT, not a count of members.

    Every member is a Swarm service rendered up front — `docs_mongo-1` through
    `-<pool>` — so `len(members) + 1` is only the next free index while nothing
    has ever been removed. It stops being one at exactly the moment the set has
    been through a failure, which is when this matters most.
    """

    def test_the_next_slot_is_the_lowest_free_one_not_the_next_number(self):
        c = FakeComponent(pool=4)
        # Member 2 was removed; the set is {1, 3}. `len + 1` is 3, which is a
        # member that is already running: the "new" member would be an add of a
        # host already in the config.
        topo = topology(member(M1), member(M3, engines.PRIMARY))
        self.assertEqual(2, plan.free_index(c, topo))

    def test_slot_one_is_never_handed_out_for_growth(self):
        """It is the copy on the master, and it is asked for by name."""
        c = FakeComponent(pool=4)
        topo = topology(member(M2, engines.PRIMARY), member(M3), member(M4))
        self.assertIsNone(plan.free_index(c, topo))
        self.assertEqual(1, plan.free_index(c, topo, on_master=True))

    def test_a_full_pool_has_no_free_slot(self):
        c = FakeComponent(pool=3)
        topo = topology(member(M1), member(M2, engines.PRIMARY), member(M3))
        self.assertIsNone(plan.free_index(c, topo))

    def test_growth_stops_at_the_pool_rather_than_colliding(self):
        c = FakeComponent(pool=3, max_members=9)
        topo = topology(member(M1), member(M2, engines.PRIMARY), member(M3))
        action = plan.next_action(c, plan.STATE_DEDICATED, topo,
                                  {"latency": True, "read": True})
        self.assertEqual("at_ceiling", action.verb)


class UpgradeCeilingTest(unittest.TestCase):
    """
    Where growing UP stops.

    The horizontal ceiling — `max_members` — was already checked on the read
    branch and missing from the write/capacity one, which is the branch that
    buys a machine every time. Combined with a `bigger` that always resolved to
    the same plan, a database under sustained write pressure bought an identical
    machine every cooldown, forever, and each one looked like an upgrade in the
    log.
    """

    def dedicated(self):
        return topology(member(M2, engines.PRIMARY), member(M3), member(M4))

    def test_write_pressure_asks_for_a_bigger_machine(self):
        c = FakeComponent(pool=6, max_members=6)
        action = plan.next_action(c, plan.STATE_DEDICATED, self.dedicated(), WRITE)
        self.assertEqual(("provision", "bigger", True),
                         (action.verb, action.node_type, action.promote))

    def test_write_pressure_stops_at_the_member_ceiling(self):
        c = FakeComponent(pool=6, max_members=3)
        action = plan.next_action(c, plan.STATE_DEDICATED, self.dedicated(), WRITE)
        self.assertEqual("at_ceiling", action.verb)

    def test_write_pressure_stops_at_the_PLAN_ceiling(self):
        """
        The one that was missing entirely. `max_members` cannot catch this: the
        set is not too big, the machines are simply as large as they may get,
        and nothing inside a replica set can see that.
        """
        c = FakeComponent(pool=6, max_members=6)
        action = plan.next_action(c, plan.STATE_DEDICATED, self.dedicated(), WRITE,
                                  at_max_plan=True)
        self.assertEqual("at_ceiling", action.verb)
        self.assertIn("DB_MAX_CORES", action.reason)

    def test_the_plan_ceiling_does_not_stop_adding_read_replicas(self):
        """
        Different pressure, different remedy. Being on the biggest machine
        allowed says nothing about whether another replica would help reads.
        """
        c = FakeComponent(pool=6, max_members=6, secondary_reads=True)
        action = plan.next_action(c, plan.STATE_DEDICATED, self.dedicated(),
                                  {"latency": True, "read": True}, at_max_plan=True)
        self.assertEqual("provision", action.verb)


class UpgradeHandoverTest(unittest.TestCase):
    """
    Finishing an upgrade, read from the cluster rather than remembered.

    `promote=True` used to be set on the action and consumed by nothing at all:
    the bigger member was added and the small primary was never retired, so the
    set grew by one machine per upgrade and kept the machine it was replacing.
    """

    def sizes(self, **kw):
        return {globals()[k]: v for k, v in kw.items()}

    def test_a_caught_up_member_on_a_bigger_machine_is_promoted(self):
        c = FakeComponent()
        topo = topology(member(M2, engines.PRIMARY), member(M3),
                        member(M4, lag=1.0))
        action = plan.next_action(
            c, plan.STATE_DEDICATED, topo, CALM,
            sizes=self.sizes(M2=SMALL, M3=SMALL, M4=BIG))
        self.assertEqual(("promote", M4), (action.verb, action.host))

    def test_a_lagging_bigger_member_is_not_promoted(self):
        """Promoting a member that is behind is choosing to lose those writes."""
        c = FakeComponent(lag_budget_seconds=10.0)
        topo = topology(member(M2, engines.PRIMARY), member(M3),
                        member(M4, lag=400.0))
        action = plan.next_action(
            c, plan.STATE_DEDICATED, topo, CALM,
            sizes=self.sizes(M2=SMALL, M3=SMALL, M4=BIG))
        self.assertNotEqual("promote", action.verb)

    def test_the_small_member_goes_only_after_the_big_one_has_taken_over(self):
        c = FakeComponent()
        # M4 is now primary and M2 is the leftover small one.
        topo = topology(member(M2), member(M3), member(M4, engines.PRIMARY),
                        member("docs_mongo-5:27017"))
        sizes = {M2: SMALL, M3: BIG, M4: BIG, "docs_mongo-5:27017": BIG}
        action = plan.next_action(c, plan.STATE_DEDICATED, topo, CALM, sizes=sizes)
        self.assertEqual(("remove", M2), (action.verb, action.host))

    def test_the_leftover_is_kept_when_dropping_it_would_leave_too_few(self):
        c = FakeComponent()
        topo = topology(member(M2), member(M3), member(M4, engines.PRIMARY))
        action = plan.next_action(c, plan.STATE_DEDICATED, topo, CALM,
                                  sizes={M2: SMALL, M3: BIG, M4: BIG})
        self.assertEqual("hold", action.verb)

    def test_the_member_on_the_master_is_never_a_promotion_candidate(self):
        """
        Its machine runs the control plane and every unmanaged component, so its
        size is not capacity this database may have. Unknown, not measured.
        """
        c = FakeComponent()
        topo = topology(member(M1), member(M2, engines.PRIMARY), member(M3))
        action = plan.next_action(c, plan.STATE_DEDICATED, topo, CALM,
                                  sizes={M2: SMALL, M3: SMALL})
        self.assertEqual("hold", action.verb)

    def test_with_no_sizes_at_all_nothing_is_promoted_or_retired(self):
        """A dataguard that cannot see the machines does not guess at them."""
        c = FakeComponent()
        topo = topology(member(M2, engines.PRIMARY), member(M3), member(M4))
        self.assertEqual("hold", plan.next_action(
            c, plan.STATE_DEDICATED, topo, CALM).verb)
