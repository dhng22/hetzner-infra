"""
Tests for the overseer's fleet arithmetic.

    python3 -m unittest discover -s overseer/tests -v

No cluster, no Hetzner, no VictoriaMetrics. Everything here is the pure part:
the packer, admission, the plan ladder and the resize state machine. That is
deliberate — those are the pieces where a bug is silent and expensive, because
the loop keeps running and simply decides the wrong thing.

These moved here wholesale when the fleet moved out of the autoscaler. They are
unchanged apart from the module they import, which is the point: a move that
needed the tests rewritten would not have been a move.

docker-py negotiates the API version when the client is constructed, so the
module cannot be imported without a socket. It is stubbed before import.
"""

import os
import sys
import time
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
# The repository root, for `signals` — shared with the autoscaler and dataguard,
# so it lives beside all three rather than inside any of them. The image copies
# it next to overseer.py; only a checkout needs this line.
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

os.environ.setdefault("HCLOUD_TOKEN", "test-token")
os.environ.setdefault("APP_NAME", "testcluster")

import docker  # noqa: E402

docker.DockerClient = lambda *a, **kw: types.SimpleNamespace(  # noqa: E731
    api=types.SimpleNamespace(), nodes=types.SimpleNamespace(),
    services=types.SimpleNamespace(), swarm=types.SimpleNamespace(),
    info=lambda: {},
)

import overseer as A  # noqa: E402
from signals import discovery, expressions, workloads as W  # noqa: E402




def res(cores, mb):
    return W.Res(int(cores * 1e9), int(mb * 1024 * 1024))


def workload(name, cores=0.5, mb=384, **policy):
    """A Workload with a policy built from labels, as discovery would."""
    labels = {"autoscale.enabled": "true"}
    labels.update({f"autoscale.{k}": str(v) for k, v in policy.items()})
    spec_replicas = policy.pop("spec_replicas", 1)
    return W.Workload(
        name=name, id=f"id-{name}",
        policy=W.policy_from_labels(name, labels, spec_replicas),
        spec_replicas=spec_replicas, cost=res(cores, mb), cpu_limit=cores * 2,
        mem_limit=int(mb * 2 * 1024 * 1024),
        pinned=False, rolling=False, component=name.split("_")[0],
        rolled_back=False, placement_pinned=False, muted=frozenset(),
    )


class LatencyDiscoveryTest(unittest.TestCase):
    """Picking a latency metric out of whatever an app happens to publish."""

    def test_prefers_a_real_histogram_over_a_timer(self):
        self.assertLess(discovery.rank_latency("http_server_requests_seconds"),
                        discovery.rank_latency("request_duration"))

    def test_ignores_metrics_that_are_not_request_latency(self):
        self.assertIsNone(discovery.rank_latency("jvm_gc_pause_seconds"))
        self.assertIsNone(discovery.rank_latency("go_gc_duration_seconds"))

    def test_recognises_the_ktor_name_that_started_this(self):
        self.assertIsNotNone(discovery.rank_latency("ktor_http_server_requests_seconds"))

    def test_unit_is_read_from_the_metric_name(self):
        self.assertEqual(expressions.unit_of("http_server_requests_seconds"), "seconds")
        self.assertEqual(expressions.unit_of("http_server_requests_millis"), "milliseconds")

    def test_mean_expr_divides_sum_by_count_and_scales_to_ms(self):
        expr = expressions.mean_expr("ktor_http_server_requests_seconds", "seconds")
        self.assertIn("_sum", expr)
        self.assertIn("_count", expr)
        self.assertIn("* 1000", expr)
        # Grouped by service, like every other per-component signal, or the
        # result cannot be joined back to the component it describes.
        self.assertIn("by (service)", expr)

    def test_p95_expression_scales_units(self):
        self.assertIn("* 1000", expressions.p95_expr("m", "seconds"))
        self.assertIn("* 1", expressions.p95_expr("m", "milliseconds"))
        self.assertIn("by (service, le)", expressions.p95_expr("m", "seconds"))


class PackerTest(unittest.TestCase):
    def test_fills_existing_bins_before_opening_another(self):
        items = [A.Item("a", res(0.5, 100), False)] * 4
        bins = [A.Bin("w1", res(2.0, 1000), False), A.Bin("w2", res(2.0, 1000), False)]
        assignment, unplaced = A.place(items, bins)
        self.assertEqual(unplaced, [])
        self.assertEqual(len(set(assignment.values())), 1)

    def test_master_is_used_last(self):
        items = [A.Item("a", res(0.5, 100), False)]
        bins = [A.Bin("master", res(9, 9000), True), A.Bin("w1", res(1, 500), False)]
        assignment, _ = A.place(items, bins)
        self.assertEqual(assignment[0], "w1")

    def test_a_pinned_item_never_lands_on_the_master(self):
        items = [A.Item("a", res(0.5, 100), True)]
        assignment, unplaced = A.place(items, [A.Bin("master", res(9, 9000), True)])
        self.assertEqual(assignment, {})
        self.assertEqual(unplaced, [0])

    def test_memory_can_be_the_binding_constraint(self):
        """The replica-unit model could not express this at all."""
        items = [A.Item("a", res(0.1, 900), False)]
        _, unplaced = A.place(items, [A.Bin("w", res(8, 500), False)])
        self.assertEqual(unplaced, [0])

    def test_output_is_deterministic(self):
        items = [A.Item("a", res(0.5, 100), False), A.Item("b", res(0.25, 50), False)]
        bins = [A.Bin("w2", res(1, 200), False), A.Bin("w1", res(1, 200), False)]
        first = A.place(items, bins)
        for _ in range(5):
            self.assertEqual(A.place(items, list(reversed(bins))), first)


class DemandTest(unittest.TestCase):
    def test_items_alternate_between_services(self):
        a, b = workload("a"), workload("b")
        items = A.demand_items([a, b], {"a": 2, "b": 2}, pinned_names=set())
        self.assertEqual([i.service for i in items], ["a", "b", "a", "b"])

    def test_uneven_counts_keep_every_replica(self):
        a, b = workload("a"), workload("b")
        items = A.demand_items([a, b], {"a": 3, "b": 1}, pinned_names=set())
        self.assertEqual(sorted(i.service for i in items), ["a", "a", "a", "b"])

    def test_constrained_items_are_placed_first(self):
        """
        Placing an unpinned item on a worker when it could have used the master,
        and thereby starving a pinned one, is the ordering mistake that produces
        pending tasks.
        """
        a, b = workload("a"), workload("b")
        items = A.demand_items([a, b], {"a": 1, "b": 1}, pinned_names={"a"})
        self.assertTrue(items[0].workers_only)


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        self.a = workload("a", 0.5, 384, min_replicas=1, max_replicas=10)
        self.b = workload("b", 0.25, 256, min_replicas=1, max_replicas=10)

    def test_growth_is_capped_not_granted(self):
        # 1.5 CPU holds two 0.5-CPU replicas AND the rollout slot a third would
        # need; 8 never fits. The bin was 1.0 while admission ignored rollouts,
        # which packed the node so full that the service could not be deployed.
        bins = [A.Bin("w", res(1.5, 2000), False)]
        admitted, capped, starved, _ = A.admit(
            [self.a], {"a": 8}, bins, {"a": 1})
        self.assertEqual(admitted["a"], 2)
        self.assertIn("a", capped)
        self.assertEqual(starved, set())

    def test_neither_service_starves_the_other(self):
        """Strict priority order would give one of them nearly everything."""
        # 2.0 CPU, not 1.5: two of each is 1.5, and the shared rollout slot is
        # the 0.5 on top. At 1.5 the pair fits exactly and neither could roll.
        bins = [A.Bin("w", res(2.0, 4000), False)]
        admitted, _, _, _ = A.admit([self.a, self.b], {"a": 9, "b": 9}, bins,
                                 {"a": 1, "b": 1})
        self.assertGreaterEqual(admitted["a"], 2)
        self.assertGreaterEqual(admitted["b"], 2)

    def test_growth_stops_one_replica_short_of_unrollable(self):
        # `order: start-first` starts the replacement before stopping the task
        # it replaces, so a service at N transiently needs room for N+1. Filling
        # the node to exactly N leaves every replica healthy and every deploy
        # stuck on `Pending — no suitable node`, serving the old image while the
        # panel reports the new one live.
        bins = [A.Bin("w", res(2.0, 4000), False)]
        admitted, capped, _, _ = A.admit([self.a], {"a": 10}, bins, {"a": 1})
        self.assertEqual(admitted["a"], 3)       # 4 x 0.5 fits, so 3 may run
        self.assertIn("a", capped)

    def test_the_rollout_slot_is_shared_not_charged_to_each_service(self):
        # One node loses one replica of room in total, not one per service.
        # Charging every service would idle memory on every multi-app node to
        # prevent a contention that resolves itself: the first rollout's old
        # task stops when its replacement starts, freeing what the second waits
        # for. `b` keeps both replicas even though `a` just gave one back.
        bins = [A.Bin("w", res(2.0, 4000), False)]
        admitted, _, _, _ = A.admit([self.a, self.b], {"a": 9, "b": 9}, bins,
                                 {"a": 1, "b": 1})
        self.assertEqual((admitted["a"], admitted["b"]), (2, 2))

    def test_the_floor_outranks_the_rollout_slot(self):
        # A service below its minimum is a live emergency; a stuck future deploy
        # is a possible one. The slot never takes a replica off the floor.
        a = workload("a", 0.5, 384, min_replicas=2, max_replicas=10)
        bins = [A.Bin("w", res(1.0, 2000), False)]
        admitted, _, starved, _ = A.admit([a], {"a": 2}, bins, {"a": 2})
        self.assertEqual(admitted["a"], 2)
        self.assertEqual(starved, set())

    def test_minimums_win_over_growth(self):
        a = workload("a", 0.5, 384, min_replicas=2, max_replicas=10)
        b = workload("b", 0.25, 256, min_replicas=2, max_replicas=10)
        bins = [A.Bin("w", res(1.5, 4000), False)]
        admitted, _, starved, _ = A.admit([a, b], {"a": 10, "b": 10}, bins,
                                       {"a": 2, "b": 2})
        self.assertGreaterEqual(admitted["a"], 2)
        self.assertGreaterEqual(admitted["b"], 2)
        self.assertEqual(starved, set())

    def test_an_unsatisfiable_minimum_is_reported(self):
        a = workload("a", 0.5, 384, min_replicas=6, max_replicas=6)
        bins = [A.Bin("w", res(1.0, 4000), False)]
        _, _, starved, _ = A.admit([a], {"a": 6}, bins, {"a": 1})
        self.assertIn("a", starved)

    def test_admission_never_scales_anything_down(self):
        """
        A transient overhead spike must not become a scale-down followed by a
        scale-up on the next loop.
        """
        bins = [A.Bin("w", W.ZERO, False)]
        admitted, _, _, _ = A.admit([self.a], {"a": 6}, bins, {"a": 6})
        self.assertEqual(admitted["a"], 6)

    def test_a_service_already_too_big_to_roll_is_reported_not_hidden(self):
        # Today's outage, in three lines. The service is running 3 replicas on
        # nodes that can only roll 1, so the never-shrink floor raises the
        # dispatched ceiling straight back to 3 and it reads as healthy. The
        # rollout ceiling is the number that is allowed to say otherwise — and
        # it has to, because the floor that hides the problem is the same rule
        # that stops anything fixing it, so the state never clears on its own.
        bins = [A.Bin("w", res(1.0, 3000), False)]
        admitted, _, _, rollable = A.admit([self.a], {"a": 3}, bins, {"a": 3})
        self.assertEqual(admitted["a"], 3)      # never shrunk under the autoscaler
        self.assertLess(rollable["a"], 3)       # and the truth is still exported

    def test_more_capacity_never_reduces_an_allocation(self):
        """Monotonicity: this is what stops the loop oscillating."""
        small = A.admit([self.a, self.b], {"a": 5, "b": 5},
                        [A.Bin("w", res(1.0, 3000), False)], {"a": 1, "b": 1})[0]
        large = A.admit([self.a, self.b], {"a": 5, "b": 5},
                        [A.Bin("w", res(3.0, 6000), False)], {"a": 1, "b": 1})[0]
        for name in ("a", "b"):
            self.assertGreaterEqual(large[name], small[name])


class FleetTest(unittest.TestCase):
    """
    The worked example: CPX31 master, CPX21 workers, two differently sized apps.

    master free   = 4 vCPU / 8192MB  -  infra 1.75 / 2304  =  2.25 CPU / 5888MB
    new worker    = 3 vCPU / 4096MB  -  per-node tax 0.20 / 224 = 2.80 / 3872MB

    Every number below is a count of HETZNER WORKERS. The master is not one, so
    0 is the free floor rather than a nonsense value.
    """

    MASTER = property(lambda self: res(2.25, 5888))
    NEW = property(lambda self: res(2.80, 3872))

    def setUp(self):
        self.a = workload("a", 0.5, 384, min_replicas=1, max_replicas=20)
        self.b = workload("b", 0.25, 256, min_replicas=1, max_replicas=20)

    def workers(self, wants, worker_bins=(), pressure=None):
        return A.workers_needed([self.a, self.b], wants, pressure, self.MASTER,
                                list(worker_bins), self.NEW)

    def test_idle_needs_no_worker_at_all(self):
        self.assertEqual(self.workers({"a": 2, "b": 1}), 0)

    def test_the_master_is_used_right_up_to_its_edge(self):
        # a=4, b=1 is exactly 2.25 CPU, which the master has. Nothing is bought.
        self.assertEqual(self.workers({"a": 4, "b": 1}), 0)

    def test_past_the_master_a_single_worker_is_reachable(self):
        # a=5, b=1 is 2.75 CPU. Over the master, but one CPX21 holds it all.
        self.assertEqual(self.workers({"a": 5, "b": 1}), 1)

    def test_two_workers_when_one_cannot_hold_everything(self):
        # a=8, b=4 -> 5.0 CPU / 4096MB. One CPX21 offers 2.80 / 3872.
        self.assertEqual(self.workers({"a": 8, "b": 4}), 2)

    def test_existing_capacity_is_filled_before_buying(self):
        big = A.Bin("w-big", res(3.8, 7968), False)      # a CPX31 worker
        small = A.Bin("w-small", res(2.8, 3872), False)  # a CPX21 worker
        # a=6, b=2 -> 3.5 CPU / 2816MB, which the CPX31 alone holds.
        self.assertEqual(self.workers({"a": 6, "b": 2}, [big, small]), 1)

    def test_node_pressure_buys_one_more(self):
        self.assertGreaterEqual(self.workers({"a": 2, "b": 1}, pressure=95.0), 1)

    def test_returning_to_the_floor_releases_every_worker(self):
        self.assertEqual(self.workers({"a": 2, "b": 1},
                                      [A.Bin("w", res(2.8, 3872), False)]), 0)

    def test_a_replica_larger_than_any_node_buys_NOTHING(self):
        """
        Not "does not buy forever" — does not buy AT ALL.

        The sizing loop proposes one machine at a time and asks how much of the
        shortfall it absorbs. When the answer is "none of it" the proposal is
        disproved, and it used to be counted anyway: the log said "no number of
        servers will ever place it" and the very same return value ordered a
        server. Nothing ever removed it either, because the want stayed at one
        forever, so an operator who typed a reservation bigger than their node
        plan bought an empty machine and paid for it until they noticed.
        """
        huge = workload("huge", 8.0, 16000, min_replicas=1, max_replicas=1)
        count = A.workers_needed([huge], {"huge": 1}, None, self.MASTER, [], self.NEW)
        self.assertEqual(count, 0)

    def test_an_unplaceable_replica_does_not_inflate_a_real_one(self):
        """The placeable half is still sized correctly beside the impossible half."""
        huge = workload("huge", 8.0, 16000, min_replicas=1, max_replicas=1)
        count = A.workers_needed([huge, self.a], {"huge": 1, "a": 5},
                                 None, self.MASTER, [], self.NEW)
        self.assertEqual(count, A.workers_needed([self.a], {"a": 5}, None,
                                                 self.MASTER, [], self.NEW))

    def test_the_floor_is_zero_by_default(self):
        """The master is not a worker, so a floor of 0 is the resting state."""
        self.assertEqual(A.MIN_WORKERS, 0)
        self.assertEqual(A.scheduled_floor(), 0)


class RemovalTest(unittest.TestCase):
    class Node:
        # `owner` defaults to the autoscaler because every node the fleet sizing
        # reasons about is one it created. A node with a different owner, or
        # none, is someone else's machine — see the ownership tests below.
        def __init__(self, node_id, created, hostname="w", owner=A.OWNER_AUTOSCALER):
            self.id = node_id
            labels = {A.NODE_OWNER_LABEL: owner} if owner else {}
            self.attrs = {"CreatedAt": created,
                          "Description": {"Hostname": hostname},
                          "Spec": {"Labels": labels}}

    def test_newest_first_when_both_are_safe(self):
        nodes = [self.Node("old", "2026-01-01T00:00:00Z"),
                 self.Node("new", "2026-06-01T00:00:00Z")]
        free = {"old": res(2.8, 3872), "new": res(2.8, 3872)}
        items = [A.Item("a", res(0.5, 384), True)]
        pick = A.pick_removal_candidate(nodes, free, items, None, {}, set())
        self.assertEqual(pick.id, "new")

    def test_a_node_the_autoscaler_does_not_own_is_never_removed(self):
        """
        A worker someone joined by hand is capacity this loop may use and must
        never delete. Ownership is the swarm label the autoscaler stamps on the
        servers matching its own Hetzner selector; without it the only thing
        protecting a foreign node was that nobody had joined one yet.
        """
        nodes = [self.Node("mine", "2026-01-01T00:00:00Z"),
                 self.Node("theirs", "2026-06-01T00:00:00Z", owner="")]
        free = {"mine": res(2.8, 3872), "theirs": res(2.8, 3872)}
        items = [A.Item("a", res(0.5, 384), True)]
        pick = A.pick_removal_candidate(nodes, free, items, None, {}, set())
        # Newest-first would have chosen "theirs".
        self.assertEqual(pick.id, "mine")

    def test_a_node_owned_by_another_manager_is_never_removed(self):
        """The owner is a NAME, so a future `managedby=dbmanager` node is
        refused here with no change to this code."""
        nodes = [self.Node("db", "2026-06-01T00:00:00Z", owner="dbmanager")]
        free = {"db": res(2.8, 3872)}
        items = [A.Item("a", res(0.5, 384), True)]
        self.assertIsNone(A.pick_removal_candidate(nodes, free, items, None, {}, set()))

    def test_a_node_whose_removal_strands_replicas_is_kept(self):
        nodes = [self.Node("only", "2026-01-01T00:00:00Z")]
        free = {"only": res(2.8, 3872)}
        items = [A.Item("a", res(0.5, 384), True)] * 4
        self.assertIsNone(A.pick_removal_candidate(nodes, free, items, None, {}, set()))

    def test_the_master_makes_the_last_worker_removable(self):
        """
        Without counting the master's capacity once the pin is off, the last
        worker is never removable and the cluster sticks one server above the
        floor forever.
        """
        nodes = [self.Node("last", "2026-01-01T00:00:00Z")]
        free = {"last": res(2.8, 3872)}
        items = [A.Item("a", res(0.5, 384), False)] * 4
        self.assertIsNone(A.pick_removal_candidate(nodes, free, items, None, {}, set()))
        pick = A.pick_removal_candidate(nodes, free, items, res(2.25, 5888), {}, set())
        self.assertIsNotNone(pick)

    def test_a_node_running_something_unmanaged_is_skipped(self):
        nodes = [self.Node("w", "2026-01-01T00:00:00Z")]
        tasks = {"w": [{"ServiceID": "mystery", "Status": {"State": "running"}}]}
        self.assertIsNone(A.pick_removal_candidate(
            nodes, {"w": res(2.8, 3872)}, [], None, tasks, set()))



# ---------------------------------------------------------------------------
# vertical scaling
# ---------------------------------------------------------------------------

def st(name, cores, memory, disk, cpu_type="shared", arch="x86", deprecated=False):
    """A Hetzner server type, shaped like the ones the API returns."""
    return types.SimpleNamespace(name=name, cores=cores, memory=memory, disk=disk,
                                 cpu_type=cpu_type, architecture=arch,
                                 deprecated=deprecated)




#: The real hel1 catalogue, names and sizes exactly as the API returns them.
#: `cpx12` is ONE core and `cpx11` is two, which is the whole reason a ladder
#: sorted by name is wrong.
HEL1 = [
    st("cpx12", 1, 2, 40), st("cpx11", 2, 2, 40), st("cpx22", 2, 4, 80),
    st("cpx21", 3, 4, 80), st("cpx32", 4, 8, 160), st("cpx31", 4, 8, 160),
    st("cpx42", 8, 16, 320), st("cpx52", 12, 24, 480), st("cpx62", 16, 32, 640),
    st("cx23", 2, 4, 40), st("cx33", 4, 8, 80),                    # other family
    st("cax21", 4, 8, 80, arch="arm"),                             # other arch
    st("ccx23", 4, 16, 160, cpu_type="dedicated"),                 # dedicated
    st("cpx99", 4, 8, 160, deprecated=True),                       # retired
]

#: What hel1-dc2 will actually sell you. The cpx11/21/31 line does not exist
#: there at all, which is exactly why a hardcoded ladder is wrong.
HEL1_AVAILABLE = {"cpx12", "cpx22", "cpx32", "cpx42", "cpx52", "cpx62",
                  "cax21", "ccx23", "cx23", "cx33"}
class LadderTest(unittest.TestCase):
    def setUp(self):
        self._saved = (A.VERTICAL, A.WORKER_MAX_CORES,
                       A.WORKER_MAX_MEMORY_GB, A.hcloud, A.LOCATION)
        A.VERTICAL = True
        A.LOCATION = "hel1"
        A.WORKER_MAX_CORES = 8
        A.WORKER_MAX_MEMORY_GB = 16
        A._catalogue.update({"at": time.time(), "types": list(HEL1),
                             "dc": {"types": HEL1_AVAILABLE}})
        byname = {t.name: t for t in HEL1}
        A.hcloud = types.SimpleNamespace(
            server_types=types.SimpleNamespace(
                get_all=lambda: list(HEL1),
                get_by_name=lambda n: byname.get(n)))

    def tearDown(self):
        (A.VERTICAL, A.WORKER_MAX_CORES,
         A.WORKER_MAX_MEMORY_GB, A.hcloud, A.LOCATION) = self._saved
        A._catalogue.update({"at": 0.0, "types": None, "dc": {}})

    def test_the_ladder_is_ordered_by_size_not_by_name(self):
        names = [t.name for t in A.worker_ladder(HEL1_AVAILABLE)]
        self.assertEqual(names, ["cpx22", "cpx32", "cpx42"])

    def test_only_the_same_family_architecture_and_cpu_type(self):
        names = {t.name for t in A.worker_ladder(None)}
        # cx is shared/x86 too, so cpu_type and architecture alone do not
        # separate it — the name prefix is what does.
        self.assertNotIn("cx33", names)
        self.assertNotIn("cax21", names)      # arm: the image would not boot
        self.assertNotIn("ccx23", names)      # dedicated
        self.assertNotIn("cpx99", names)      # deprecated

    def test_the_floor_is_worker_type_and_the_disk_never_shrinks(self):
        names = {t.name for t in A.worker_ladder(None)}
        # Below the floor in cores, memory or disk: a resize can never go there,
        # because the server keeps its original disk and Hetzner refuses a plan
        # whose disk is smaller than the one the server has.
        self.assertNotIn("cpx12", names)
        self.assertNotIn("cpx11", names)

    def test_the_ceiling_is_a_capacity_not_a_plan_name(self):
        names = {t.name for t in A.worker_ladder(None)}
        self.assertIn("cpx42", names)         # 8 / 16 — exactly at the ceiling
        self.assertNotIn("cpx52", names)      # 12 / 24
        self.assertNotIn("cpx62", names)
        A.WORKER_MAX_CORES, A.WORKER_MAX_MEMORY_GB = 16, 32
        self.assertIn("cpx62", {t.name for t in A.worker_ladder(None)})

    def test_availability_is_per_datacenter(self):
        # cpx21/cpx31 exist in the catalogue and are the right family and size,
        # but hel1 does not sell them. Ordering one is an error, not a resize.
        every = {t.name for t in A.worker_ladder(None)}
        self.assertIn("cpx21", every)
        self.assertNotIn("cpx21", {t.name for t in A.worker_ladder(HEL1_AVAILABLE)})

    def test_next_rung_walks_and_stops(self):
        lad = A.worker_ladder(HEL1_AVAILABLE)
        self.assertEqual(A.next_rung("cpx22", lad, 1).name, "cpx32")
        self.assertEqual(A.next_rung("cpx32", lad, 1).name, "cpx42")
        self.assertIsNone(A.next_rung("cpx42", lad, 1))       # at the ceiling
        self.assertEqual(A.next_rung("cpx42", lad, -1).name, "cpx32")
        self.assertIsNone(A.next_rung("cpx22", lad, -1))      # at the floor

    def test_a_worker_above_a_lowered_ceiling_can_still_come_down(self):
        """
        Drop the ceiling under a worker that already grew, and it is off the
        ladder entirely. It must never grow again, and it must still be able to
        shrink — stranding an oversized worker with no way back is the one
        outcome worth avoiding here.
        """
        A.WORKER_MAX_CORES, A.WORKER_MAX_MEMORY_GB = 4, 8
        lad = A.worker_ladder(HEL1_AVAILABLE)
        self.assertEqual([t.name for t in lad], ["cpx22", "cpx32"])
        self.assertIsNone(A.next_rung("cpx42", lad, 1))
        self.assertEqual(A.next_rung("cpx42", lad, -1).name, "cpx32")

    def test_no_ceiling_means_no_ladder_at_all(self):
        A.VERTICAL = False
        self.assertEqual(A.worker_ladder(HEL1_AVAILABLE), [])


def wnode(node_id, hostname, owner=A.OWNER_AUTOSCALER, created="2026-01-01T00:00:00Z"):
    labels = {A.NODE_OWNER_LABEL: owner} if owner else {}
    return types.SimpleNamespace(
        id=node_id, attrs={"CreatedAt": created,
                           "Description": {"Hostname": hostname},
                           "Spec": {"Role": "worker", "Availability": "active",
                                    "Labels": labels},
                           "Status": {"State": "ready"}})


def hserver(hostname, type_name, status="running", dc="hel1-dc2", disk=80):
    """
    A Hetzner server as the API really returns one: `server_type` is a stub with
    only a name, `datacenter` is None, and `primary_disk_size` is the disk the
    machine ACTUALLY has — 80 GB for anything created as cpx22, whatever plan it
    has since been grown onto, because every resize passes upgrade_disk=False.
    """
    return types.SimpleNamespace(
        name=hostname, status=status, primary_disk_size=disk,
        server_type=types.SimpleNamespace(name=type_name),
        datacenter=None)


class VerticalPlanTest(unittest.TestCase):
    """
    Which worker gets grown, which gets shrunk, and — mostly — when neither does.

    The refusals are the point. Growing a worker drains it and powers it off for
    minutes, so every case below that ends in None is a small outage that did
    not happen.
    """

    def setUp(self):
        self._saved = {k: getattr(A, k) for k in
                       ("VERTICAL", "WORKER_MAX_CORES",
                        "WORKER_MAX_MEMORY_GB", "hcloud", "LOCATION")}
        A.LOCATION = "hel1"
        A.VERTICAL = True
        A.WORKER_MAX_CORES, A.WORKER_MAX_MEMORY_GB = 8, 16
        A._catalogue.update({"at": 0.0, "types": None, "dc": {}})
        self.servers = {"w-a": hserver("w-a", "cpx22"), "w-b": hserver("w-b", "cpx22")}
        A.hcloud = types.SimpleNamespace(
            server_types=types.SimpleNamespace(get_all=lambda: list(HEL1)),
            servers=types.SimpleNamespace(get_by_name=lambda n: self.servers.get(n)),
            datacenters=types.SimpleNamespace(get_all=lambda: [
                types.SimpleNamespace(
                    name="hel1-dc2",
                    location=types.SimpleNamespace(name="hel1"),
                    server_types=types.SimpleNamespace(
                        available=[t for t in HEL1 if t.name in HEL1_AVAILABLE])),
                types.SimpleNamespace(   # another location: must not widen the set
                    name="ash-dc1",
                    location=types.SimpleNamespace(name="ash"),
                    server_types=types.SimpleNamespace(
                        available=[t for t in HEL1 if t.name == "cpx21"]))]))
        self.a, self.b = wnode("w-a", "w-a"), wnode("w-b", "w-b")
        self.ready = [self.a, self.b]
        # Two cpx22 workers, each with 2 cores / 4 GB free for apps.
        self.free = {"w-a": res(2, 4096), "w-b": res(2, 4096)}
        self.tasks, self.app_ids = {}, {"svc"}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(A, k, v)
        A._catalogue.update({"at": 0.0, "types": None, "dc": {}})

    def items(self, n, cores=1.0, mb=2048):
        w = workload("svc", cores=cores, mb=mb)
        return A.demand_items([w], {"svc": n}, pinned_names={"svc"})

    def plan(self, live, want, direction=1, ready=None, free=None, workers=2,
             manager_free=None):
        return A.plan_resize(
            ready if ready is not None else self.ready,
            free if free is not None else self.free,
            live, want, manager_free, self.tasks, self.app_ids,
            getattr(self, "workloads", []), direction)

    # --- growing ----------------------------------------------------------

    def test_grows_the_smaller_worker_rather_than_buying(self):
        self.servers["w-a"] = hserver("w-a", "cpx32")     # already grown once
        self.free["w-a"] = res(4, 8192)
        # Four running replicas fit; a fifth does not, and one more rung holds it.
        got = self.plan(self.items(4), self.items(5))
        self.assertIsNotNone(got)
        node, _, target = got
        self.assertEqual(node.id, "w-b")                  # the SMALLER one
        self.assertEqual(target.name, "cpx32")

    def test_refuses_when_only_one_worker_exists(self):
        """
        The HA rule, stated by the operator: never take the fleet to nothing.
        Even with the master free to catch everything, a single worker being
        power-cycled is the whole worker pool gone.
        """
        got = self.plan(self.items(1), self.items(4), ready=[self.a], workers=1,
                        manager_free=res(8, 16384))
        self.assertIsNone(got)

    def test_refuses_when_the_rest_cannot_hold_what_is_running(self):
        """The drain window is a real outage if the survivors have no room."""
        # Both workers full: nothing can move off either one.
        got = self.plan(self.items(4), self.items(6))
        self.assertIsNone(got)

    def test_never_power_cycles_a_worker_it_does_not_own(self):
        """
        A worker someone joined by hand is capacity the packer may use and a
        machine this must never reboot. Two assertions, because "it picked the
        other one" and "it picked nothing" are different guarantees.
        """
        self.ready = [self.a, wnode("w-b", "w-b", owner="")]
        self.assertEqual(self.plan(self.items(1), self.items(5))[0].id, "w-a")

        self.ready = [wnode("w-a", "w-a", owner=""), wnode("w-b", "w-b", owner="")]
        self.assertIsNone(self.plan(self.items(1), self.items(5)))

    def test_never_power_cycles_a_worker_another_manager_owns(self):
        """The owner is a NAME: a future `managedby=dbmanager` node is refused
        by the same check, with nothing here to change."""
        self.ready = [self.a, wnode("w-b", "w-b", owner="dbmanager")]
        self.assertEqual(self.plan(self.items(1), self.items(5))[0].id, "w-a")

    def test_refuses_a_worker_holding_state_it_does_not_manage(self):
        self.tasks = {"w-b": [{"ServiceID": "postgres", "Status": {"State": "running"}}]}
        self.tasks["w-a"] = []
        got = self.plan(self.items(1), self.items(5))
        self.assertEqual(got[0].id, "w-a")               # never w-b

    def test_refuses_when_growing_would_not_be_enough(self):
        """
        One more rung does not cover the demand, so buying a server is the right
        answer and this must stand aside rather than power-cycle a node for
        nothing.
        """
        self.assertIsNone(self.plan(self.items(1), self.items(20)))

    def test_refuses_at_the_ceiling(self):
        self.servers["w-a"] = hserver("w-a", "cpx42")
        self.servers["w-b"] = hserver("w-b", "cpx42")
        self.free = {"w-a": res(8, 16384), "w-b": res(8, 16384)}
        self.assertIsNone(self.plan(self.items(1), self.items(40)))

    def test_off_when_no_ceiling_is_configured(self):
        A.VERTICAL = False
        self.assertIsNone(self.plan(self.items(1), self.items(5)))

    def test_the_choice_is_deterministic(self):
        first = self.plan(self.items(1), self.items(5))
        for _ in range(5):
            self.assertEqual(self.plan(self.items(1), self.items(5))[0].id, first[0].id)

    # --- shrinking --------------------------------------------------------

    def test_shrinks_the_largest_worker_first(self):
        self.servers["w-a"] = hserver("w-a", "cpx42")
        self.servers["w-b"] = hserver("w-b", "cpx32")
        self.free = {"w-a": res(8, 16384), "w-b": res(4, 8192)}
        live = self.items(2)
        got = self.plan(live, live, direction=-1)
        self.assertEqual(got[0].id, "w-a")
        self.assertEqual(got[2].name, "cpx32")

    def test_never_shrinks_below_the_floor(self):
        live = self.items(1)
        self.assertIsNone(self.plan(live, live, direction=-1))   # both at cpx22

    def test_refuses_a_shrink_the_fleet_could_not_absorb(self):
        self.servers["w-a"] = hserver("w-a", "cpx42")
        self.free = {"w-a": res(8, 16384), "w-b": res(2, 4096)}
        # Ten replicas need every core there is; dropping a rung strands them.
        live = self.items(10, cores=0.9, mb=1800)
        self.assertIsNone(self.plan(live, live, direction=-1))

    def test_never_targets_a_plan_this_location_does_not_sell(self):
        """
        The catalogue is global; what you can BUY is per location. `cpx21` is a
        real cpx plan, one rung above cpx22 by size, and hel1 does not sell it —
        so a ladder built from the catalogue alone picks it and the resize fails
        at `change_type`.

        A server object comes back with `datacenter=None`, which is why this is
        keyed on the configured LOCATION and not on the server's datacenter: the
        per-datacenter version of this filter silently never ran.
        """
        got = self.plan(self.items(1), self.items(5))
        self.assertIsNotNone(got)
        self.assertEqual(got[2].name, "cpx32")
        self.assertIn(got[2].name, HEL1_AVAILABLE)

    def test_another_location_cannot_widen_the_ladder(self):
        """Intersection, not union: servers are created with a LOCATION and
        Hetzner picks the datacenter, so a plan sold in only one of them is a
        plan that may not be there when it is wanted."""
        names = {t.name for t in A.worker_ladder(A.location_types())}
        self.assertNotIn("cpx21", names)      # only ash-dc1 has it in this fixture

    def test_growing_then_shrinking_is_not_a_flap(self):
        """
        The oscillation that matters: grow a worker because the demand needs it,
        and the very next loop must NOT decide the fleet is oversized and shrink
        it back. Two power cycles a minute, forever, on live traffic.
        """
        # Two cpx22s hold 4 cores / 8 GB between them. Two replicas are running
        # (so one worker can take the other's load during the power cycle), and
        # five are wanted, which needs a rung more than the fleet has.
        want = self.items(5)
        got = self.plan(self.items(2), want)
        self.assertIsNotNone(got)
        node, _, target = got
        # Apply the resize, then ask the downscale the same question.
        self.servers[node.id] = hserver(node.id, target.name)
        self.free[node.id] = self.free[node.id] + (
            A.type_res(target) - A.type_res(A.type_by_name("cpx22")))
        self.assertIsNone(self.plan(want, want, direction=-1))

    def test_shrink_and_removal_agree_about_safety(self):
        """
        `fits_without` is shared on purpose. If the resize planner and the
        removal planner ever disagreed about whether a node can go, the fleet
        would drain a node one loop and refuse to the next.
        """
        self.servers["w-a"] = hserver("w-a", "cpx42")
        self.free = {"w-a": res(8, 16384), "w-b": res(2, 4096)}
        for n in (1, 3, 6, 10):
            live = self.items(n, cores=0.9, mb=1800)
            removable = A.pick_removal_candidate(
                self.ready, self.free, live, None, self.tasks, self.app_ids)
            safe = A.fits_without("w-a", self.ready, self.free, live, None)
            self.assertEqual(bool(removable and removable.id == "w-a"), safe,
                             f"disagreement at {n} replica(s)")

    def test_a_hand_upgraded_disk_blocks_the_downgrade_instead_of_retrying(self):
        """
        The one-way door. Upgrading a disk is irreversible on Hetzner, so a
        worker somebody grew through the console — disk and all — can never come
        back down. This must notice from the server's real disk and stop, rather
        than order a downgrade Hetzner refuses on every loop forever.
        """
        self.servers["w-a"] = hserver("w-a", "cpx32", disk=160)   # disk grew too
        self.servers["w-b"] = hserver("w-b", "cpx22", disk=80)
        self.free = {"w-a": res(4, 8192), "w-b": res(2, 4096)}
        live = self.items(1)
        got = self.plan(live, live, direction=-1)
        # w-a is disk-locked on cpx32; w-b is already at the floor. Neither moves.
        self.assertIsNone(got)

    def test_a_grown_worker_keeps_its_original_disk_and_can_come_back_down(self):
        """
        The property the whole downscale path rests on: `upgrade_disk=False`
        leaves an 80 GB disk on a plan whose nominal disk is 320, and every rung
        below still offers at least 80, so the way back is open.
        """
        self.servers["w-a"] = hserver("w-a", "cpx42", disk=80)    # grown, disk kept
        self.free = {"w-a": res(8, 16384), "w-b": res(2, 4096)}
        live = self.items(1)
        got = self.plan(live, live, direction=-1)
        self.assertIsNotNone(got)
        self.assertEqual(got[0].id, "w-a")
        self.assertEqual(got[2].name, "cpx32")

    # --- availability, which is not capacity ------------------------------

    def _spread(self, on_a, on_b):
        """Running tasks of one service, placed across the two workers."""
        w = workload("svc", cores=1.0, mb=2048)._replace(spec_replicas=on_a + on_b)
        self.workloads = [w]
        # app_ids must be the SERVICE ids, or every task reads as foreign state
        # and the candidate is refused for the wrong reason entirely.
        self.app_ids = {w.id}
        run = lambda n: [{"ServiceID": w.id, "Status": {"State": "running"}}] * n
        self.tasks = {"w-a": run(on_a), "w-b": run(on_b)}
        return w

    def test_never_drains_the_node_holding_the_only_running_replica(self):
        """
        A drain STOPS a task and Swarm starts its replacement afterwards — there
        is no start-first for rescheduling. So the sole replica of a service
        going through a drain is an outage, not reduced capacity, however much
        room the other node has.

        Two assertions, because "it picked the other node" and "it picked
        nothing" are different guarantees: resizing a node that holds nothing is
        perfectly safe and must still be allowed.
        """
        self._spread(on_a=1, on_b=0)
        self.assertEqual(self.plan(self.items(1), self.items(5))[0].id, "w-b")

        # ...and when the node holding it is the only candidate, nothing moves.
        self.ready = [self.a]
        self.ready = [self.a, wnode("w-b", "w-b", owner="")]      # w-b not ours
        self.assertIsNone(self.plan(self.items(1), self.items(5)))

    def test_allows_it_once_the_service_is_spread(self):
        """Two replicas across two nodes: the survivor serves throughout, which
        is the whole reason to run two of anything."""
        self._spread(on_a=1, on_b=1)
        self.assertIsNotNone(self.plan(self.items(2), self.items(5)))

    def test_the_same_guard_applies_to_shrinking(self):
        self.servers["w-a"] = hserver("w-a", "cpx42", disk=80)
        self.free = {"w-a": res(8, 16384), "w-b": res(2, 4096)}
        live = self.items(1)
        # Sole replica on the big node: it is the only one with a rung to give
        # up, and it must not be drained.
        self._spread(on_a=1, on_b=0)
        self.assertIsNone(self.plan(live, live, direction=-1))
        self._spread(on_a=1, on_b=1)
        self.assertEqual(self.plan(live, live, direction=-1)[0].id, "w-a")

    def test_the_ladder_is_a_round_trip_not_a_ratchet(self):
        """
        Up every rung and back down again on one machine, which is the question
        the whole downscale path turns on: "once a worker is upscaled, can it
        ever come back?"

        It can, and only because the disk never grows. The server is created as
        cpx22 with an 80 GB disk; every `change_type` passes upgrade_disk=False,
        so it still has 80 GB on cpx42 whose nominal disk is 320 — and every rung
        below offers at least 80, so each step down is legal. Had the disk been
        upgraded on the way up, cpx42 would be terminal.
        """
        lad = A.worker_ladder(A.location_types())
        names = [t.name for t in lad]
        self.assertEqual(names, ["cpx22", "cpx32", "cpx42"])

        disk = 80                                   # created as cpx22, never grown
        up = ["cpx22"]
        while A.next_rung(up[-1], [t for t in lad if t.disk >= disk], 1):
            up.append(A.next_rung(up[-1], [t for t in lad if t.disk >= disk], 1).name)
        self.assertEqual(up, ["cpx22", "cpx32", "cpx42"])

        down = ["cpx42"]
        while A.next_rung(down[-1], [t for t in lad if t.disk >= disk], -1):
            down.append(A.next_rung(down[-1], [t for t in lad if t.disk >= disk], -1).name)
        self.assertEqual(down, ["cpx42", "cpx32", "cpx22"])
        self.assertEqual(down, list(reversed(up)))

    def test_a_stub_server_type_never_reads_as_free_capacity(self):
        """
        The API hands back a server whose `.server_type` carries a name and
        nothing else. Sizing off that stub makes every delta zero, so a resize
        looks like it buys infinite capacity for nothing.
        """
        self.servers["w-a"] = hserver("w-a", "not-a-real-plan")
        got = self.plan(self.items(1), self.items(5))
        self.assertEqual(got[0].id, "w-b")               # w-a skipped, loudly


class FakeServer:
    """A Hetzner server that behaves like one: change_type only works off."""
    def __init__(self, name, type_name, dc="hel1-dc2"):
        self.name, self.status = name, "running"
        self.server_type = types.SimpleNamespace(name=type_name)
        self.datacenter = types.SimpleNamespace(name=dc)
        self.calls, self.upgrade_disk = [], None

    def power_off(self):
        self.calls.append("off")
        self.status = "off"

    def power_on(self):
        self.calls.append("on")
        self.status = "running"

    def change_type(self, server_type, upgrade_disk):
        self.calls.append(("change", server_type.name, upgrade_disk))
        if self.status != "off":
            raise RuntimeError("server must be powered off to change its type")
        self.upgrade_disk = upgrade_disk
        self.server_type = types.SimpleNamespace(name=server_type.name)


class FakeNode:
    def __init__(self, node_id, hostname, state="ready"):
        self.id = node_id
        self.attrs = {"Description": {"Hostname": hostname},
                      "Spec": {"Role": "worker", "Availability": "active",
                               "Labels": {A.NODE_OWNER_LABEL: A.OWNER_AUTOSCALER,
                                          "zone": "eu"}},
                      "Status": {"State": state}}

    def update(self, spec):
        self.attrs["Spec"] = spec


class ResizeMachineTest(unittest.TestCase):
    """
    The power-cycle itself, one loop-step at a time.

    Every one of these is about a machine that is switched off. The property
    that matters more than the resize succeeding is that the node ALWAYS ends up
    back in service — a drained, powered-off worker is capacity being paid for
    and not used, and nothing else in the system will notice it.
    """

    def setUp(self):
        self._saved = {k: getattr(A, k) for k in
                       ("hcloud", "dkr", "tasks_on_node", "DRY_RUN",
                        "POST_DRAIN_GRACE", "_resize", "_last_node_resize")}
        A.DRY_RUN = False
        A.POST_DRAIN_GRACE = 0
        A._resize = None
        self.server = FakeServer("w-a", "cpx22")
        self.node = FakeNode("w-a", "w-a")
        self.tasks = []
        A.tasks_on_node = lambda node_id: list(self.tasks)
        A.dkr = types.SimpleNamespace(nodes=types.SimpleNamespace(get=lambda i: self.node))
        A.hcloud = types.SimpleNamespace(
            servers=types.SimpleNamespace(get_by_name=lambda n: self.server),
            server_types=types.SimpleNamespace(
                get_by_name=lambda n: next((t for t in HEL1 if t.name == n), None)))

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(A, k, v)

    def begin(self, target="cpx32"):
        A.begin_resize(self.node, self.server,
                       next(t for t in HEL1 if t.name == target), 1)

    def run_until_done(self, limit=40):
        phases = []
        for _ in range(limit):
            if not A.advance_resize():
                break
            phases.append(A._resize["phase"] if A._resize else None)
        return phases

    def availability(self):
        return self.node.attrs["Spec"]["Availability"]

    def test_a_clean_resize_walks_every_phase_and_ends_in_service(self):
        self.begin()
        phases = self.run_until_done()
        self.assertEqual(
            [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]],
            ["draining", "verifying", "powering_off", "changing",
             "powering_on", "rejoining"])
        self.assertEqual(self.server.server_type.name, "cpx32")
        self.assertEqual(self.server.status, "running")
        self.assertEqual(self.availability(), "active")
        self.assertFalse(A.resize_busy())

    def test_the_disk_is_never_upgraded(self):
        """
        A grown disk can never shrink, and Hetzner refuses any plan whose disk is
        smaller than the one the server has. Upgrade it once and every future
        downscale is a permanent no — the feature would only ever ratchet up.
        """
        self.begin()
        self.run_until_done()
        self.assertIs(self.server.upgrade_disk, False)

    def test_the_server_is_off_before_the_plan_changes(self):
        self.begin()
        self.run_until_done()
        change = [c for c in self.server.calls if isinstance(c, tuple)][0]
        self.assertEqual(self.server.calls.index("off"),
                         self.server.calls.index(change) - 1)

    def test_it_drains_before_powering_off_and_waits_for_tasks(self):
        self.tasks = [{"ID": "t1"}]
        self.begin()
        for _ in range(3):
            A.advance_resize()
        self.assertEqual(self.availability(), "drain")
        self.assertEqual(self.server.status, "running")   # nothing switched off
        self.tasks = []
        self.run_until_done()
        self.assertEqual(self.server.server_type.name, "cpx32")

    def test_labels_survive_the_drain(self):
        """
        Docker REPLACES a node spec. Writing availability with a partial payload
        drops `managedby` — and a worker with no owner is a server nothing will
        ever remove.
        """
        self.begin()
        self.run_until_done()
        labels = self.node.attrs["Spec"]["Labels"]
        self.assertEqual(labels.get(A.NODE_OWNER_LABEL), A.OWNER_AUTOSCALER)
        self.assertEqual(labels.get("zone"), "eu")

    def test_a_failed_plan_change_still_returns_the_node_to_service(self):
        def explode(server_type, upgrade_disk):
            raise RuntimeError("no capacity in hel1-dc2")
        self.server.change_type = explode
        self.begin()
        for _ in range(30):
            try:
                if not A.advance_resize():
                    break
            except Exception:
                A.end_resize(False, "boom")
                break
        self.assertEqual(self.availability(), "active")
        self.assertEqual(self.server.status, "running")
        self.assertEqual(self.server.server_type.name, "cpx22")   # unchanged
        self.assertFalse(A.resize_busy())

    def _with_service(self, running_elsewhere):
        """One app service, with N of its replicas running off this node."""
        A.begin_resize(self.node, self.server,
                       next(t for t in HEL1 if t.name == "cpx32"), 1,
                       [workload("svc")._replace(spec_replicas=2)])
        sid = list(A._resize["services"])[0]
        A.dkr.api = types.SimpleNamespace(tasks=lambda filters=None: [
            {"ServiceID": sid, "NodeID": "other", "Status": {"State": "running"}}
        ] * running_elsewhere)

    def test_waits_for_the_drained_tasks_to_actually_restart_elsewhere(self):
        """
        Tasks LEAVING the node is not the same as tasks RUNNING somewhere else,
        and the difference is the outage. The packer models CPU and memory —
        not published ports, volume affinity, max_replicas_per_node, or a
        constraint only this node satisfies.
        """
        self._with_service(running_elsewhere=0)
        for _ in range(6):
            A.advance_resize()
        self.assertEqual(A._resize["phase"], "verifying")
        self.assertEqual(self.server.status, "running")     # still up, still drained
        self.assertNotIn("off", self.server.calls)

        sid = list(A._resize["services"])[0]
        A.dkr.api = types.SimpleNamespace(tasks=lambda filters=None: [
            {"ServiceID": sid, "NodeID": "other", "Status": {"State": "running"}}])
        self.run_until_done()
        self.assertEqual(self.server.server_type.name, "cpx32")

    def test_un_drains_when_the_tasks_never_come_back(self):
        """
        Cheapest recovery available: the machine is still here and can take its
        own tasks straight back. That option stops existing the moment it is
        powered off, which is why verification happens BEFORE the power-off and
        not after.
        """
        self._with_service(running_elsewhere=0)
        A.advance_resize()
        while A.resize_busy() and A._resize["phase"] != "verifying":
            A.advance_resize()
        A._resize["since"] = 0.0                      # verification deadline blown
        A.advance_resize()
        self.assertFalse(A.resize_busy())
        self.assertEqual(self.availability(), "active")
        self.assertEqual(self.server.status, "running")
        self.assertNotIn("off", self.server.calls)
        self.assertEqual(self.server.server_type.name, "cpx22")

    def test_a_stuck_drain_abandons_rather_than_cutting_tasks_off(self):
        """
        `remove_worker` powers through a stuck drain because removal has to
        complete for the fleet to reach its floor. A resize does not have to
        happen at all, so killing live tasks to save a few euros is the wrong
        trade — the node goes back into service on its old plan.
        """
        self.tasks = [{"ID": "stuck"}]
        self.begin()
        A.advance_resize()
        A._resize["since"] = 0.0                 # drain deadline blown
        A.advance_resize()
        self.assertFalse(A.resize_busy())
        self.assertEqual(self.availability(), "active")
        self.assertEqual(self.server.status, "running")
        self.assertEqual(self.server.server_type.name, "cpx22")   # never changed
        self.assertNotIn("off", self.server.calls)                # never cut off

    def test_a_stuck_phase_times_out_and_recovers(self):
        self.server.power_off = lambda: None      # never actually powers down
        self.begin()
        A._resize["phase"] = "powering_off"
        A._resize["since"] = 0.0                  # already past the deadline
        A.advance_resize()
        self.assertFalse(A.resize_busy())
        self.assertEqual(self.availability(), "active")

    def test_a_vanished_swarm_node_ends_the_resize(self):
        self.begin()
        def gone(_):
            raise RuntimeError("no such node")
        A.dkr.nodes.get = gone
        A.advance_resize()
        self.assertFalse(A.resize_busy())

    def test_a_deleted_server_ends_the_resize(self):
        self.begin()
        while A.resize_busy() and A._resize["phase"] in ("draining", "verifying"):
            A.advance_resize()                    # neither phase reads the server
        A.hcloud.servers.get_by_name = lambda n: None
        A.advance_resize()
        self.assertFalse(A.resize_busy())
        self.assertEqual(self.availability(), "active")

    def test_dry_run_changes_nothing_at_all(self):
        A.DRY_RUN = True
        self.assertTrue(self.begin() or True)
        self.assertFalse(A.resize_busy())
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.availability(), "active")

    def test_the_cooldown_starts_when_the_resize_ENDS(self):
        """Measured from the end, not the start: back-to-back power cycles of the
        same fleet are the flap this is here to prevent."""
        A._last_node_resize = 0.0
        self.begin()
        self.run_until_done()
        self.assertGreater(A._last_node_resize, 0.0)


class PackerPropertyTest(unittest.TestCase):
    """
    Properties of `place()` over many generated inputs, rather than one case.

    These are the two claims the anti-oscillation argument rests on. A
    counterexample to either is a fleet that buys a worker and deletes it again
    on the next loop, which no single fixed test would catch.
    """

    def cases(self):
        import itertools, random
        rng = random.Random(20260826)
        for _ in range(300):
            n_items = rng.randint(1, 12)
            items = [A.Item(f"s{rng.randint(0, 2)}",
                            res(rng.choice([0.1, 0.25, 0.5, 1.0]),
                                rng.choice([128, 256, 512, 1024])),
                            rng.random() < 0.3)
                     for _ in range(n_items)]
            bins = [A.Bin(f"n{i}", res(rng.choice([1, 2, 4]),
                                       rng.choice([1024, 2048, 4096])),
                          i == 0 and rng.random() < 0.5)
                    for i in range(rng.randint(1, 4))]
            yield items, bins

    def test_more_capacity_never_places_fewer(self):
        """
        Monotone. If growing a node could REDUCE how much fits, then a resize
        that adds capacity could shed a replica, and the loop after would add it
        back — a flap driven by the packer itself.
        """
        for items, bins in self.cases():
            _, before = A.place(items, bins)
            bigger = [A.Bin(b.key, b.free + res(1, 1024), b.is_manager) for b in bins]
            _, after = A.place(items, bigger)
            self.assertLessEqual(len(after), len(before))

    def test_placement_is_deterministic(self):
        for items, bins in self.cases():
            first = A.place(items, bins)
            for _ in range(3):
                self.assertEqual(A.place(items, bins), first)

    def test_a_manager_bin_never_takes_a_workers_only_item(self):
        """The pin is a correctness boundary, not a preference: an item marked
        workers_only landing on the master is a task Swarm will refuse."""
        for items, bins in self.cases():
            assignment, _ = A.place(items, bins)
            by_key = {b.key: b for b in bins}
            for idx, key in assignment.items():
                if items[idx].workers_only:
                    self.assertFalse(by_key[key].is_manager)


class ConfigTest(unittest.TestCase):
    """
    An empty environment variable is an ABSENT setting, not a value.

    `stacks/monitoring.yml` passes every setting as "${KEY}", and
    `docker stack deploy` substitutes a key that infra.env does not carry with
    the empty string rather than leaving it unset. So the container receives
    KEY="" and `os.environ.get(key, default)` never reaches its default: every
    cluster built before a setting existed hands "" to a reader expecting a
    number. `int("")` raises during import, the process exits 1, and Swarm
    restarts it forever — which is what adding the vertical-scaling ceilings did
    to a running autoscaler, taking scaling down and the panel's Autoscaler tab
    with it.
    """

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("PROBE_INT", "PROBE_STR", "PROBE_BOOL")}

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_an_empty_value_falls_back_to_the_default(self):
        os.environ["PROBE_INT"] = ""
        self.assertEqual(A._env("PROBE_INT", "8", int), 8)
        os.environ["PROBE_INT"] = "   "
        self.assertEqual(A._env("PROBE_INT", "8", int), 8)

    def test_an_absent_value_falls_back_to_the_default(self):
        os.environ.pop("PROBE_INT", None)
        self.assertEqual(A._env("PROBE_INT", "8", int), 8)

    def test_a_real_value_still_wins(self):
        os.environ["PROBE_INT"] = "3"
        self.assertEqual(A._env("PROBE_INT", "8", int), 3)
        os.environ["PROBE_BOOL"] = "true"
        self.assertTrue(A._env("PROBE_BOOL", "false", bool))

    def test_a_blank_default_is_still_a_default(self):
        # SCHEDULE_FLOOR ships blank and means "no floor". Empty-as-absent must
        # resolve it to the blank default rather than raising.
        os.environ["PROBE_STR"] = ""
        self.assertEqual(A._env("PROBE_STR", ""), "")

    def test_no_value_and_no_default_is_still_loud(self):
        os.environ["PROBE_INT"] = ""
        with self.assertRaises(RuntimeError):
            A._env("PROBE_INT")


class LeaseTest(unittest.TestCase):
    """
    Machines held for a database. The overseer buys them; it never deletes one.
    """

    def setUp(self):
        self._saved = {k: getattr(A, k) for k in ("hcloud", "dkr", "DRY_RUN")}
        A.DRY_RUN = False
        A._leases_lock = A.threading.Lock()
        with A._leases_lock:
            A._leases.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(A, k, v)
        with A._leases_lock:
            A._leases.clear()

    def test_a_manager_replaces_its_whole_lease_set_every_time(self):
        """
        Level triggered, like every other delivery here. A request that fails to
        arrive is corrected by the next one a minute later, and there is no queue
        to get stuck — which also means a dataguard restart re-asserts everything
        it holds within one loop.
        """
        A.record_leases({"from": "dataguard", "leases": [
            {"lease": "docs/2", "type": "cpx31"},
            {"lease": "docs/3", "type": "cpx31"}]})
        self.assertEqual(set(A.live_leases()), {"docs/2", "docs/3"})
        A.record_leases({"from": "dataguard", "leases": [{"lease": "docs/2"}]})
        self.assertEqual(set(A.live_leases()), {"docs/2"})

    def test_one_manager_cannot_drop_another_manager_s_leases(self):
        A.record_leases({"from": "dataguard", "leases": [{"lease": "docs/2"}]})
        A.record_leases({"from": "somebody-else", "leases": [{"lease": "other/1"}]})
        self.assertEqual(set(A.live_leases()), {"docs/2", "other/1"})

    def test_a_lease_nobody_renews_expires(self):
        A.record_leases({"from": "dataguard", "leases": [{"lease": "docs/2"}]})
        stale = time.time() + A.LEASE_TTL_SECONDS + 1
        self.assertEqual(A.live_leases(now=stale), {})

    def test_what_a_lease_reports_includes_whether_it_may_schedule(self):
        """
        A leased machine joins PAUSED, because `docker swarm join` cannot set
        node labels and an unlabelled node has nothing for
        `node.labels.dedicated != true` to match. So `availability` has to cross
        the wire: dataguard treats ready-but-paused as not ready, and without
        this field it could not tell the difference.
        """
        node = FakeNode("w-b", "w-b")
        node.attrs["Spec"]["Labels"] = {A.NODE_LEASE_LABEL: "docs/2"}
        node.attrs["Spec"]["Availability"] = "pause"
        A.dkr = types.SimpleNamespace(
            nodes=types.SimpleNamespace(list=lambda: [node]))
        reported = A.lease_nodes()
        self.assertEqual(reported["docs/2"]["availability"], "pause")
        self.assertEqual(reported["docs/2"]["state"], "ready")

    def test_a_dedicated_node_is_invisible_to_application_capacity(self):
        """
        A leased machine is not packed, not counted as room for replicas and
        never picked for removal by the app scale-down path. Counting it would
        buy one worker fewer than the applications need and put the shortfall on
        a database's disk.
        """
        plain = FakeNode("w-a", "w-a")
        leased = FakeNode("w-b", "w-b")
        leased.attrs["Spec"]["Labels"] = {A.NODE_DEDICATED_LABEL: "true",
                                          A.NODE_LEASE_LABEL: "docs/2"}
        A.dkr = types.SimpleNamespace(
            nodes=types.SimpleNamespace(list=lambda: [plain, leased]))
        self.assertTrue(A.is_dedicated(leased))
        self.assertFalse(A.is_dedicated(plain))
        self.assertEqual([n.id for n in A.swarm_ready_workers()], ["w-a"])

    def test_releasing_a_lease_never_deletes_the_machine(self):
        """
        The failure mode of a dataguard outage must be a server that costs money,
        never one that takes a database with it. A released lease makes the node
        ORDINARY — and the ordinary removal path still refuses a node with
        foreign state on it.
        """
        node = FakeNode("w-b", "w-b")
        node.attrs["Spec"]["Labels"] = {A.NODE_LEASE_LABEL: "docs/2"}
        A.dkr = types.SimpleNamespace(
            nodes=types.SimpleNamespace(list=lambda: [node]))
        A.hcloud = types.SimpleNamespace(
            servers=types.SimpleNamespace(get_all=lambda **kw: []))
        deleted = []
        A.hcloud.servers.delete = lambda s: deleted.append(s)
        A.reconcile_leases()          # no leases held at all
        self.assertEqual(deleted, [])


class BasePlanTest(unittest.TestCase):
    """
    Which plan the cluster builds on, and why it is not a setting.

    `WORKER_TYPE=cpx22` was one for a while, and it was the wrong shape twice
    over: Hetzner adds and retires plans constantly and does not sell the same
    family in every location, so a name typed into infra.env is a name that stops
    existing — silently, leaving a cluster that cannot buy a worker at all.
    """

    def setUp(self):
        self._saved = (A.hcloud, A.LOCATION)
        A.LOCATION = "hel1"
        A._catalogue.update({"at": time.time(), "types": list(HEL1),
                             "dc": {"types": HEL1_AVAILABLE}})
        A.hcloud = types.SimpleNamespace(
            server_types=types.SimpleNamespace(get_all=lambda: list(HEL1)))

    def tearDown(self):
        A.hcloud, A.LOCATION = self._saved
        A._catalogue.update({"at": 0.0, "types": None, "dc": {}})

    def test_it_picks_the_smallest_plan_that_meets_the_requirement(self):
        """
        2 cores, 4 GB, 80 GB of disk, shared x86. In hel1 today that is cpx22 —
        an OUTPUT, not a configuration, and it will quietly become something
        else the day the line is renamed.
        """
        self.assertEqual(A.base_type_name(), "cpx22")

    def test_the_disk_floor_excludes_the_cheap_tier_without_naming_it(self):
        """
        cx23 is 2 cores and 4 GB and would otherwise win on size. Its 40 GB disk
        is what rules it out — which is how "not the cost-optimised line" gets
        enforced against an API that has no such field.
        """
        base = A.base_server_type()
        self.assertGreaterEqual(base.disk, A.BASE_MIN_DISK_GB)
        self.assertNotEqual(base.name, "cx23")

    def test_it_never_picks_dedicated_or_arm(self):
        """The image would not boot on ARM, and dedicated is a different bill."""
        base = A.base_server_type()
        self.assertEqual(base.cpu_type, "shared")
        self.assertEqual(base.architecture, "x86")

    def test_a_plan_this_location_does_not_sell_is_not_chosen(self):
        """
        The failure that motivated reading availability at all: cpx21 exists in
        the catalogue and cannot be bought in hel1.
        """
        A._catalogue["dc"]["types"] = {"cpx21", "cpx31"}
        self.assertEqual(A.base_type_name(), "cpx21")
        A._catalogue["dc"]["types"] = HEL1_AVAILABLE
        self.assertEqual(A.base_type_name(), "cpx22")

    def test_nothing_suitable_is_nothing_rather_than_a_guess(self):
        """
        None means "the catalogue could not answer", and every caller treats
        that as "do not act". Falling back to a name is exactly how the setting
        it replaced failed.
        """
        A._catalogue["dc"]["types"] = {"cax21"}
        self.assertIsNone(A.base_server_type())
        self.assertEqual(A.base_type_name(), "")

    def test_a_bigger_machine_is_the_next_rung_of_the_same_ladder(self):
        """
        A manager asks for "bigger", never for a plan name — only this process
        can read the catalogue, and the two must not drift apart.
        """
        A.VERTICAL, saved = True, (A.VERTICAL, A.WORKER_MAX_CORES, A.WORKER_MAX_MEMORY_GB)
        A.WORKER_MAX_CORES, A.WORKER_MAX_MEMORY_GB = 8, 16
        try:
            self.assertEqual(A.lease_plan({}), "cpx22")
            self.assertEqual(A.lease_plan({"bigger": True}), "cpx32")
        finally:
            A.VERTICAL, A.WORKER_MAX_CORES, A.WORKER_MAX_MEMORY_GB = saved


if __name__ == "__main__":
    unittest.main()


class DatabaseCeilingTest(unittest.TestCase):
    """
    How big a database is allowed to get, and what happens at the top.

    This is the half that was missing. The floor was inferred and the ladder
    existed, but `bigger` was resolved from the BASE plan every time and gated
    on the WORKER vertical switch — so a database could never climb past the
    second rung, and with vertical worker scaling off it could not climb at all
    while still being handed a machine and billed for it. The loop above it has
    no way to notice: it asks for bigger, gets a machine, promotes it, is under
    exactly the same pressure, waits out the cooldown and asks again.
    """

    def setUp(self):
        self._saved = (A.VERTICAL, A.WORKER_MAX_CORES, A.WORKER_MAX_MEMORY_GB,
                       A.DB_MAX_CORES, A.DB_MAX_MEMORY_GB, A.DB_MAX_STORAGE,
                       A.hcloud, A.LOCATION)
        A.VERTICAL = True
        A.LOCATION = "hel1"
        A.WORKER_MAX_CORES, A.WORKER_MAX_MEMORY_GB = 8, 16
        A.DB_MAX_CORES, A.DB_MAX_MEMORY_GB = 16, 32
        # Set explicitly and generously here so the CORES/MEMORY cases are about
        # cores and memory. The storage ceiling has its own tests below, and its
        # real default is deliberately tighter than this.
        A.DB_MAX_STORAGE = 1024 * 1024
        A._catalogue.update({"at": time.time(), "types": list(HEL1),
                             "dc": {"types": HEL1_AVAILABLE}})
        byname = {t.name: t for t in HEL1}
        A.hcloud = types.SimpleNamespace(
            server_types=types.SimpleNamespace(
                get_all=lambda: list(HEL1),
                get_by_name=lambda n: byname.get(n)))
        self._servers = []
        A.hetzner_servers = lambda: list(self._servers)

    def tearDown(self):
        (A.VERTICAL, A.WORKER_MAX_CORES, A.WORKER_MAX_MEMORY_GB,
         A.DB_MAX_CORES, A.DB_MAX_MEMORY_GB, A.DB_MAX_STORAGE,
         A.hcloud, A.LOCATION) = self._saved
        A._catalogue.update({"at": 0.0, "types": None, "dc": {}})

    def hold(self, lease, type_name):
        """A Hetzner server carrying a database lease, on a given plan."""
        self._servers.append(types.SimpleNamespace(
            name=f"host-{lease.replace('/', '-')}",
            labels={"lease": lease},
            server_type=types.SimpleNamespace(name=type_name)))

    # --- the ladder itself -------------------------------------------------

    def test_the_database_ladder_does_not_stop_when_worker_resizing_is_off(self):
        """
        Turning off vertical WORKER scaling must not silently cap databases.

        The two are different mechanisms: a worker is power-cycled onto a bigger
        plan, which is why an operator gets to refuse it; a database member is
        replaced by a bigger one that has already synced, so that risk is not
        the one being declined. Sharing the switch meant declining a risk that
        does not exist and accepting one that does — a purchase with no effect.
        """
        A.VERTICAL = False
        self.assertEqual([], A.worker_ladder(HEL1_AVAILABLE))
        self.assertTrue([t.name for t in A.db_ladder(HEL1_AVAILABLE)])

    def test_the_database_ceiling_is_its_own_and_is_higher(self):
        names = [t.name for t in A.db_ladder(HEL1_AVAILABLE)]
        self.assertEqual(["cpx22", "cpx32", "cpx42", "cpx52", "cpx62"], names)
        # The worker ceiling stops two rungs earlier, and neither moved the other.
        self.assertEqual(["cpx22", "cpx32", "cpx42"],
                         [t.name for t in A.worker_ladder(HEL1_AVAILABLE)])

    def test_a_ceiling_of_zero_is_a_ladder_of_nothing(self):
        A.DB_MAX_CORES = 0
        self.assertEqual([], A.db_ladder(HEL1_AVAILABLE))

    def test_the_storage_ceiling_cuts_the_ladder_too(self):
        """
        Disk is the third dimension and it binds independently of the other two.

        cpx62 is 16 cores and 32GB — exactly at both other ceilings — and 640GB
        of disk. Without this filter a database with plenty of cores and memory
        headroom climbs onto storage nobody chose, and a plan's disk cannot be
        reduced afterwards.
        """
        A.DB_MAX_STORAGE = 320 * 1024
        self.assertEqual(["cpx22", "cpx32", "cpx42"],
                         [t.name for t in A.db_ladder(HEL1_AVAILABLE)])

    def test_the_shipped_storage_default_binds_nothing_on_its_own(self):
        """
        At its shipped default the storage ceiling must not shorten the ladder.

        640GB is the disk of cpx62, which is also exactly the cores and memory
        ceiling — so out of the box the three defaults stop at the same rung and
        storage is a cap to LOWER, not a third gate to trip over on the way to
        the first two. Pinned because it is the kind of default that goes wrong
        silently: set it below the largest allowed plan and an operator raising
        DB_MAX_CORES sees nothing change, with nothing anywhere saying why.
        """
        # The literals, not settings_def: this suite runs against the OVERSEER
        # image, which does not carry admin/. Keeping these three numbers equal
        # to the panel's and the cloud-init's is `test_defaults_agree_everywhere`
        # in admin/tests, which scans all three files for exactly this reason.
        A.DB_MAX_STORAGE, A.DB_MAX_CORES, A.DB_MAX_MEMORY_GB = 655360, 16, 32
        with_storage = [t.name for t in A.db_ladder(HEL1_AVAILABLE)]
        A.DB_MAX_STORAGE = 1024 * 1024          # effectively no storage ceiling
        self.assertEqual([t.name for t in A.db_ladder(HEL1_AVAILABLE)], with_storage)
        self.assertEqual(["cpx22", "cpx32", "cpx42", "cpx52", "cpx62"], with_storage)

    def test_a_storage_ceiling_of_zero_is_a_ladder_of_nothing(self):
        A.DB_MAX_STORAGE = 0
        self.assertEqual([], A.db_ladder(HEL1_AVAILABLE))

    def test_the_storage_ceiling_does_not_touch_workers(self):
        """
        A worker's disk holds images and logs, not data. It is sized by whatever
        plan the cpu and memory ceilings land on, and the database's storage
        budget has no business narrowing it.
        """
        A.DB_MAX_STORAGE = 81 * 1024        # barely above the base plan's 80GB
        self.assertEqual(["cpx22", "cpx32", "cpx42"],
                         [t.name for t in A.worker_ladder(HEL1_AVAILABLE)])
        self.assertEqual(["cpx22"], [t.name for t in A.db_ladder(HEL1_AVAILABLE)])

    # --- what `bigger` resolves to ------------------------------------------

    def test_the_first_machine_is_the_base_plan(self):
        self.assertEqual("cpx22", A.lease_plan({"lease": "docs/2"}))

    def test_bigger_climbs_from_what_the_component_already_holds(self):
        """
        The bug this exists for: `bigger` was always one rung above the BASE, so
        the second upgrade bought the same plan as the first, and the third, and
        every one after that.
        """
        self.hold("docs/2", "cpx22")
        self.assertEqual("cpx32", A.lease_plan({"lease": "docs/3", "bigger": True}))
        self.hold("docs/3", "cpx32")
        self.assertEqual("cpx42", A.lease_plan({"lease": "docs/4", "bigger": True}))
        self.hold("docs/4", "cpx42")
        self.assertEqual("cpx52", A.lease_plan({"lease": "docs/5", "bigger": True}))

    def test_one_database_does_not_climb_because_another_did(self):
        """
        Leases are grouped by the component that holds them. A big Mongo next
        door must not make a small Redis start at the top of the ladder.
        """
        self.hold("docs/2", "cpx42")
        self.hold("cache/2", "cpx22")
        self.assertEqual("cpx32", A.lease_plan({"lease": "cache/3", "bigger": True}))

    def test_at_the_top_it_stops_instead_of_buying_the_same_thing_again(self):
        self.hold("docs/2", "cpx62")
        self.assertTrue(A.at_max_plan("docs/2"))
        # Still answers with a plan — a transition already under way must not be
        # stranded half done — but the caller is told not to ask again.
        self.assertEqual("cpx62", A.lease_plan({"lease": "docs/3", "bigger": True}))

    def test_a_component_on_the_master_is_not_at_the_ceiling(self):
        """
        No leases is the BOTTOM of the ladder, not the top. Reading "nothing
        held" as "nothing left" would stop every database growing off the master.
        """
        self.assertFalse(A.at_max_plan("docs/2"))

    def test_the_ceiling_reaches_the_manager_that_has_to_act_on_it(self):
        """
        Dataguard cannot read the Hetzner catalogue and must not learn a plan
        name. So the one fact it needs travels on the lease response.
        """
        self.hold("docs/2", "cpx62")
        node = types.SimpleNamespace(
            id="n1",
            attrs={"Description": {"Hostname": "host-docs-2",
                                   "Resources": {"NanoCPUs": 16_000_000_000,
                                                 "MemoryBytes": 32 * 1024 ** 3}},
                   "Status": {"State": "ready"},
                   "Spec": {"Availability": "active",
                            "Labels": {"lease": "docs/2"}}})
        A.dkr = types.SimpleNamespace(nodes=types.SimpleNamespace(
            list=lambda: [node]))
        reported = A.lease_nodes()
        self.assertTrue(reported["docs/2"]["at_max_plan"])
