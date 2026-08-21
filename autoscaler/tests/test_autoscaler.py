"""
Tests for the autoscaler's decision logic.

    python3 -m unittest discover -s autoscaler/tests -v

No cluster, no Hetzner, no VictoriaMetrics. Everything here is the pure part:
policy parsing, the packer, admission, and the fleet arithmetic. That is
deliberate — those are the pieces where a bug is silent and expensive, because
the loop keeps running and simply decides the wrong thing.

docker-py negotiates the API version when the client is constructed, so the
module cannot be imported without a socket. It is stubbed before import.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HCLOUD_TOKEN", "test-token")
os.environ.setdefault("APP_NAME", "testcluster")

import docker  # noqa: E402

docker.DockerClient = lambda *a, **kw: types.SimpleNamespace(  # noqa: E731
    api=types.SimpleNamespace(), nodes=types.SimpleNamespace(),
    services=types.SimpleNamespace(), swarm=types.SimpleNamespace(),
    info=lambda: {},
)

import autoscaler as A  # noqa: E402


def res(cores, mb):
    return A.Res(int(cores * 1e9), int(mb * 1024 * 1024))


def workload(name, cores=0.5, mb=384, **policy):
    """A Workload with a policy built from labels, as discovery would."""
    labels = {"autoscale.enabled": "true"}
    labels.update({f"autoscale.{k}": str(v) for k, v in policy.items()})
    spec_replicas = policy.pop("spec_replicas", 1)
    return A.Workload(
        name=name, id=f"id-{name}",
        policy=A.policy_from_labels(name, labels, spec_replicas),
        spec_replicas=spec_replicas, cost=res(cores, mb), cpu_limit=cores * 2,
        pinned=False, rolling=False, component=name.split("_")[0],
        rolled_back=False,
    )


class ResTest(unittest.TestCase):
    def test_arithmetic_is_integer(self):
        a = res(0.05, 64) + res(0.10, 128) + res(0.05, 32)
        self.assertEqual(a, res(0.20, 224))
        # The float version of this sum is 0.19999999999999998, which compares
        # the wrong way against a 0.2 budget on roughly half of all inputs.
        self.assertIsInstance(a.cpu, int)

    def test_subtraction_clamps_at_zero(self):
        self.assertEqual(res(1, 100) - res(2, 300), A.ZERO)

    def test_fits_is_componentwise(self):
        self.assertTrue(res(0.5, 100).fits_in(res(1, 200)))
        self.assertFalse(res(0.5, 300).fits_in(res(1, 200)))   # memory binds
        self.assertFalse(res(2.0, 100).fits_in(res(1, 200)))   # cpu binds


class PolicyTest(unittest.TestCase):
    def test_no_labels_means_fixed_at_the_live_count(self):
        p = A.policy_from_labels("api_app", {}, 4)
        self.assertFalse(p.autoscale)
        self.assertEqual((p.min_replicas, p.max_replicas), (4, 4))

    def test_a_fixed_service_follows_a_manual_scale(self):
        """Its bounds are read fresh each loop, so it is respected, not fought."""
        self.assertEqual(A.policy_from_labels("x", {}, 9).min_replicas, 9)

    def test_garbage_never_raises_and_falls_back(self):
        p = A.policy_from_labels("api_app", {
            "autoscale.enabled": "true",
            "autoscale.min_replicas": "not-a-number",
            "autoscale.slo_p95_ms": "",
            "autoscale.max_replicas": "999999",
        }, 1)
        self.assertTrue(p.autoscale)
        self.assertEqual(p.min_replicas, 1)
        self.assertEqual(p.slo_ms, 500.0)
        self.assertEqual(p.max_replicas, 1)     # out of range -> default (= min)

    def test_max_below_min_is_repaired(self):
        p = A.policy_from_labels("x", {"autoscale.enabled": "true",
                                       "autoscale.min_replicas": "5",
                                       "autoscale.max_replicas": "2"}, 1)
        self.assertEqual((p.min_replicas, p.max_replicas), (5, 5))

    def test_crossed_thresholds_revert_both_sides(self):
        """Repairing one side alone produces a config nobody wrote."""
        p = A.policy_from_labels("x", {"autoscale.enabled": "true",
                                       "autoscale.up_p95_ratio": "0.4",
                                       "autoscale.down_p95_ratio": "0.8"}, 1)
        self.assertEqual((p.up_ratio, p.down_ratio), (0.8, 0.4))

    def test_metric_name_is_validated(self):
        p = A.policy_from_labels("x", {"autoscale.enabled": "true",
                                       "autoscale.p95_histogram": "sum(evil{a=1})"}, 1)
        self.assertEqual(p.histogram, "http_server_requests_seconds_bucket")

    def test_p95_expression_scales_units(self):
        self.assertIn("* 1000", A.p95_expr("m", "seconds"))
        self.assertIn("* 1", A.p95_expr("m", "milliseconds"))
        self.assertIn("by (service, le)", A.p95_expr("m", "seconds"))


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
        bins = [A.Bin("w", res(1.0, 2000), False)]
        admitted, capped, starved = A.admit(
            [self.a], {"a": 8}, bins, {"a": 1})
        self.assertEqual(admitted["a"], 2)       # 2 x 0.5 CPU fits, 8 does not
        self.assertIn("a", capped)
        self.assertEqual(starved, set())

    def test_neither_service_starves_the_other(self):
        """Strict priority order would give one of them nearly everything."""
        bins = [A.Bin("w", res(1.5, 4000), False)]
        admitted, _, _ = A.admit([self.a, self.b], {"a": 9, "b": 9}, bins,
                                 {"a": 1, "b": 1})
        self.assertGreaterEqual(admitted["a"], 2)
        self.assertGreaterEqual(admitted["b"], 2)

    def test_minimums_win_over_growth(self):
        a = workload("a", 0.5, 384, min_replicas=2, max_replicas=10)
        b = workload("b", 0.25, 256, min_replicas=2, max_replicas=10)
        bins = [A.Bin("w", res(1.5, 4000), False)]
        admitted, _, starved = A.admit([a, b], {"a": 10, "b": 10}, bins,
                                       {"a": 2, "b": 2})
        self.assertGreaterEqual(admitted["a"], 2)
        self.assertGreaterEqual(admitted["b"], 2)
        self.assertEqual(starved, set())

    def test_an_unsatisfiable_minimum_is_reported(self):
        a = workload("a", 0.5, 384, min_replicas=6, max_replicas=6)
        bins = [A.Bin("w", res(1.0, 4000), False)]
        _, _, starved = A.admit([a], {"a": 6}, bins, {"a": 1})
        self.assertIn("a", starved)

    def test_admission_never_scales_anything_down(self):
        """
        A transient overhead spike must not become a scale-down followed by a
        scale-up on the next loop.
        """
        bins = [A.Bin("w", A.ZERO, False)]
        admitted, _, _ = A.admit([self.a], {"a": 6}, bins, {"a": 6})
        self.assertEqual(admitted["a"], 6)

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

    def test_a_replica_larger_than_any_node_does_not_buy_forever(self):
        huge = workload("huge", 8.0, 16000, min_replicas=1, max_replicas=1)
        count = A.workers_needed([huge], {"huge": 1}, None, self.MASTER, [], self.NEW)
        self.assertLessEqual(count, A.MAX_WORKERS + 2)

    def test_the_floor_is_zero_by_default(self):
        """The master is not a worker, so a floor of 0 is the resting state."""
        self.assertEqual(A.MIN_WORKERS, 0)
        self.assertEqual(A.scheduled_floor(), 0)


class RemovalTest(unittest.TestCase):
    class Node:
        def __init__(self, node_id, created, hostname="w"):
            self.id = node_id
            self.attrs = {"CreatedAt": created, "Description": {"Hostname": hostname}}

    def test_newest_first_when_both_are_safe(self):
        nodes = [self.Node("old", "2026-01-01T00:00:00Z"),
                 self.Node("new", "2026-06-01T00:00:00Z")]
        free = {"old": res(2.8, 3872), "new": res(2.8, 3872)}
        items = [A.Item("a", res(0.5, 384), True)]
        pick = A.pick_removal_candidate(nodes, free, items, None, {}, set())
        self.assertEqual(pick.id, "new")

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


class MetricLifecycleTest(unittest.TestCase):
    def test_children_of_deleted_components_are_removed(self):
        """
        A labelled child persists at its last value forever, so a deleted
        component would keep alerting — or keep an alert suppressed.
        """
        A.S_RUNNING.labels("gone_app").set(3)
        A.S_RUNNING.labels("stays_app").set(1)
        A._exported_services.update({"gone_app", "stays_app"})
        A.forget_vanished({"stays_app"})
        samples = {s.labels["service"] for m in A.S_RUNNING.collect() for s in m.samples}
        self.assertNotIn("gone_app", samples)
        self.assertIn("stays_app", samples)


if __name__ == "__main__":
    unittest.main()
