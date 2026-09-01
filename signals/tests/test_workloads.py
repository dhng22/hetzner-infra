"""
Tests for what a workload IS, what it costs and what policy it carries.

    python3 -m unittest discover -s signals/tests -v

These live here rather than in either process because BOTH read them. The
overseer sizes the fleet from `cost` and `max_replicas`; the autoscaler writes
the replica count those same numbers imply. A parsing bug that reached only one
of them would leave the two acting on different beliefs about the same service,
with nothing failing while they did.
"""

import unittest

from signals import workloads as W




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
        rolled_back=False, placement_pinned=False,
    )


class ResTest(unittest.TestCase):
    def test_arithmetic_is_integer(self):
        a = res(0.05, 64) + res(0.10, 128) + res(0.05, 32)
        self.assertEqual(a, res(0.20, 224))
        # The float version of this sum is 0.19999999999999998, which compares
        # the wrong way against a 0.2 budget on roughly half of all inputs.
        self.assertIsInstance(a.cpu, int)

    def test_subtraction_clamps_at_zero(self):
        self.assertEqual(res(1, 100) - res(2, 300), W.ZERO)

    def test_fits_is_componentwise(self):
        self.assertTrue(res(0.5, 100).fits_in(res(1, 200)))
        self.assertFalse(res(0.5, 300).fits_in(res(1, 200)))   # memory binds
        self.assertFalse(res(2.0, 100).fits_in(res(1, 200)))   # cpu binds


class PolicyTest(unittest.TestCase):
    def test_no_labels_means_fixed_at_the_live_count(self):
        p = W.policy_from_labels("api_app", {}, 4)
        self.assertFalse(p.autoscale)
        self.assertEqual((p.min_replicas, p.max_replicas), (4, 4))

    def test_a_fixed_service_follows_a_manual_scale(self):
        """Its bounds are read fresh each loop, so it is respected, not fought."""
        self.assertEqual(W.policy_from_labels("x", {}, 9).min_replicas, 9)

    def test_garbage_never_raises_and_falls_back(self):
        p = W.policy_from_labels("api_app", {
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
        p = W.policy_from_labels("x", {"autoscale.enabled": "true",
                                       "autoscale.min_replicas": "5",
                                       "autoscale.max_replicas": "2"}, 1)
        self.assertEqual((p.min_replicas, p.max_replicas), (5, 5))

    def test_crossed_thresholds_revert_both_sides(self):
        """Repairing one side alone produces a config nobody wrote."""
        p = W.policy_from_labels("x", {"autoscale.enabled": "true",
                                       "autoscale.up_p95_ratio": "0.4",
                                       "autoscale.down_p95_ratio": "0.8"}, 1)
        self.assertEqual((p.up_ratio, p.down_ratio), (0.8, 0.4))

    def test_metric_name_is_validated(self):
        """A label that is not a metric name must never reach a query."""
        p = W.policy_from_labels("x", {"autoscale.enabled": "true",
                                       "autoscale.p95_histogram": "sum(evil{a=1})"}, 1)
        self.assertEqual(p.histogram, "")
        # And it must not be treated as an explicit choice either, or the
        # rejected string would still be the one selected over discovery.
        self.assertFalse(p.histogram_explicit)

    def test_a_valid_metric_name_is_kept_and_marked_explicit(self):
        p = W.policy_from_labels("x", {"autoscale.enabled": "true",
                                       "autoscale.p95_histogram": "ktor_x_seconds_bucket"}, 1)
        self.assertEqual(p.histogram, "ktor_x_seconds_bucket")
        self.assertTrue(p.histogram_explicit)

    def test_no_label_means_discovery(self):
        """
        The old default was a Spring metric name. It matched nothing for every
        other framework, and an empty histogram_quantile looks exactly like an
        idle service — so p95 read n/a forever and nobody could tell why.
        """
        p = W.policy_from_labels("x", {"autoscale.enabled": "true"}, 1)
        self.assertEqual(p.histogram, "")
        self.assertFalse(p.histogram_explicit)


class WorkerPinSpellingTest(unittest.TestCase):
    """
    The pin must be recognisable in every spelling Swarm or the renderer uses.

    `--constraint-rm` matches the stored string exactly. The renderer writes
    `node.role == worker`; the shared constant has no spaces. Removing
    the first by the name of the second silently removed nothing, so the pin
    could never be released and the fleet could never reach zero — while the
    loop logged a placement change every minute and bumped the service version
    thousands of times.
    """

    SPELLINGS = [
        "node.role == worker",
        "node.role==worker",
        "node.role  ==  worker",
        " node.role == worker ",
    ]

    def test_every_spelling_is_recognised_as_the_pin(self):
        for text in self.SPELLINGS:
            self.assertTrue(W._WORKER_PIN.match(text), text)

    def test_our_own_constant_is_recognised(self):
        self.assertTrue(W._WORKER_PIN.match(W.WORKER_CONSTRAINT))

    def test_the_renderer_spelling_is_recognised(self):
        """The exact string admin/components/app.py writes."""
        self.assertTrue(W._WORKER_PIN.match("node.role == worker"))

    def test_a_different_constraint_is_not_the_pin(self):
        for text in ("node.role == manager", "node.labels.role == worker",
                     "node.role != worker"):
            self.assertIsNone(W._WORKER_PIN.match(text), text)



    def test_the_live_spelling_is_what_gets_removed(self):
        """
        `worker_pin_constraints` returns what is ON the service, not what we
        would have written. Returning our own constant is the bug: the CLI
        matches exactly, so removing `node.role==worker` from a service pinned
        with `node.role == worker` removes nothing and reports success.
        """
        import types
        service = types.SimpleNamespace(attrs={"Spec": {"TaskTemplate": {
            "Placement": {"Constraints": ["node.role == worker",
                                          "node.labels.dedicated != true"]}}}})
        self.assertEqual(W.worker_pin_constraints(service), ["node.role == worker"])

    def test_nothing_to_remove_is_an_empty_list_not_a_guess(self):
        import types
        service = types.SimpleNamespace(attrs={"Spec": {"TaskTemplate": {}}})
        self.assertEqual(W.worker_pin_constraints(service), [])


if __name__ == "__main__":
    unittest.main()


class ContradictoryLabelTest(unittest.TestCase):
    """
    A service labelled as BOTH an application and a dataguard-managed database.

    It should never happen, and the whole point is that when it does, nothing
    downstream has to remember to check. It used to be caught in one of the
    three places that act on a workload — the replica count — and missed by the
    other two, and those two are the dangerous ones: rewriting a member's node
    constraint moves a mongod to a machine where its volume does not exist, and
    rewriting its reservations restarts it. Excluding it from the list is the
    only guard that also covers the fourth caller somebody adds next year.
    """

    def service(self, **labels):
        base = {W.WORKLOAD_LABEL: W.WORKLOAD_APP, "autoscale.enabled": "true"}
        base.update(labels)
        return type("S", (), {
            "name": "docs_mongo-2",
            "id": "svc-docs-mongo-2",
            "attrs": {"Spec": {
                "Labels": base,
                "Mode": {"Replicated": {"Replicas": 1}},
                "TaskTemplate": {"Resources": {"Reservations": {
                    "NanoCPUs": int(0.5e9), "MemoryBytes": 512 * 1024 * 1024}}},
            }},
        })()

    def test_an_ordinary_application_is_still_a_workload(self):
        self.assertIsNotNone(W.workload_from_service(self.service()))

    def test_a_dataguard_managed_service_is_not_one_however_it_is_labelled(self):
        found = W.workload_from_service(self.service(
            **{W.MANAGED_BY_LABEL: W.MANAGED_BY_DATAGUARD}))
        self.assertIsNone(found)

    def test_it_is_absent_from_discovery_rather_than_refused_later(self):
        both = [self.service(),
                self.service(**{W.MANAGED_BY_LABEL: W.MANAGED_BY_DATAGUARD})]
        dkr = type("D", (), {"services": type("S", (), {
            "list": staticmethod(lambda: both)})()})()
        found, _services, ok = W.discover_workloads(dkr)
        self.assertTrue(ok)
        self.assertEqual(1, len(found))


class CountTest(unittest.TestCase):
    """
    Turning a direction into a count.

    Two things used to be true and are not any more: a scale-up was a fixed
    percentage however far over the line the service was, and a scale-down was
    always exactly one replica, so walking 20 back to 2 took eighteen cooldowns
    and cost money for every one of them.
    """

    def policy(self, **labels):
        base = {"infra.workload": "app", "autoscale.enabled": "true",
                "autoscale.min_replicas": "2", "autoscale.max_replicas": "40"}
        base.update(labels)
        return W.policy_from_labels("api", base, 20)

    def walk(self, policy, direction, start, reading, limit=30):
        """Every count the loop would pass through, until it settles."""
        seen, n = [start], start
        for _ in range(limit):
            nxt = W.bounded(policy, W.desired_replicas(
                policy, direction, n,
                held=reading if direction == W.classify.DIRECTION_UP else None,
                peak=reading if direction == W.classify.DIRECTION_DOWN else None))
            if nxt == n:
                break
            seen.append(nxt)
            n = nxt
        return seen

    def test_the_target_is_the_middle_of_the_band_not_either_edge(self):
        """
        A count that lands on the scale-up line scales up again next loop, and
        one that lands on the scale-down line scales down again. Between them is
        the only place the loop can stop.
        """
        p = self.policy()
        self.assertEqual(W.aim(p.down_cpu, p.up_cpu), 50.0)
        self.assertGreater(W.aim(p.down_cpu, p.up_cpu), p.down_cpu)
        self.assertLess(W.aim(p.down_cpu, p.up_cpu), p.up_cpu)

    def test_an_idle_service_walks_back_in_a_few_steps_not_eighteen(self):
        p = self.policy()
        steps = self.walk(p, W.classify.DIRECTION_DOWN, 20, (None, 5.0, 10.0))
        self.assertEqual(steps[-1], 2)
        self.assertLessEqual(len(steps), 6, steps)

    def test_a_barely_quiet_service_shrinks_more_carefully_than_an_idle_one(self):
        """
        The whole point of a ratio: how far under the line it is decides how far
        it moves. A fixed percentage would take the same step for both.
        """
        p = self.policy()
        idle = self.walk(p, W.classify.DIRECTION_DOWN, 20, (None, 5.0, 10.0))
        nearly = self.walk(p, W.classify.DIRECTION_DOWN, 20, (None, 28.0, 40.0))
        self.assertLess(idle[1], nearly[1])

    def test_a_shrink_can_never_land_a_service_over_its_own_scale_up_line(self):
        """
        The safety property behind allowing a big step at all. A service may
        only shrink when its peak per-replica CPU is under `down_cpu`, and the
        arithmetic aims at the middle of the band — so the count it lands on is
        one whose projected load is below `up_cpu`, not one that bounces
        straight back up.
        """
        p = self.policy()
        for peak in (1.0, 10.0, 20.0, 29.9):
            with self.subTest(peak=peak):
                after = W.bounded(p, W.desired_replicas(
                    p, W.classify.DIRECTION_DOWN, 20, peak=(None, peak, 10.0)))
                projected = 20 * peak / after
                self.assertLess(projected, p.up_cpu)

    def test_a_shrink_always_moves_at_least_one_replica(self):
        """Otherwise a DOWN verdict is silently a hold and nothing says so."""
        p = self.policy()
        for peak in (29.9, 20.0, 5.0):
            with self.subTest(peak=peak):
                after = W.desired_replicas(p, W.classify.DIRECTION_DOWN, 20,
                                           peak=(None, peak, 10.0))
                self.assertLess(after, 20)

    def test_a_shrink_never_exceeds_the_down_factor_cap(self):
        p = self.policy(**{"autoscale.down_factor": "0.25"})
        after = W.desired_replicas(p, W.classify.DIRECTION_DOWN, 20,
                                   peak=(None, 0.0, 0.0))
        self.assertEqual(after, 15)

    def test_a_growth_never_exceeds_the_up_factor_cap(self):
        """
        `up_factor` was the step and is now the cap, so nothing scales up faster
        than it ever did. A ratio that asks for more is clamped to it.
        """
        p = self.policy()
        after = W.desired_replicas(p, W.classify.DIRECTION_UP, 20,
                                   held=(None, 400.0, 10.0))
        self.assertEqual(after, 30)                    # 20 + int(20 * 0.5)

    def test_a_latency_breach_with_idle_replicas_adds_one_not_a_percentage(self):
        """
        The incident in `classify.decide`'s docstring, one layer down. Latency
        is not linear in replica count and is not necessarily this service's
        fault at all, so it never gets a multiplier — a request five times the
        SLO is not a request for five times the replicas.
        """
        p = self.policy()
        after = W.desired_replicas(p, W.classify.DIRECTION_UP, 8,
                                   held=(4000.0, 26.0, 30.0))
        self.assertEqual(after, 9)

    def test_the_resource_that_needs_the_most_replicas_decides(self):
        """
        Kubernetes takes the max across metrics, and so does this: satisfying
        the worst one satisfies the others, and satisfying any other one leaves
        the worst still over its line.
        """
        p = self.policy()
        cpu_only = W.desired_replicas(p, W.classify.DIRECTION_DOWN, 20,
                                      peak=(None, 5.0, None))
        with_mem = W.desired_replicas(p, W.classify.DIRECTION_DOWN, 20,
                                      peak=(None, 5.0, 65.0))
        self.assertGreater(with_mem, cpu_only)

    def test_a_missing_reading_falls_back_to_the_step_it_always_took(self):
        """
        A gap in cadvisor must produce a cautious number, never an arbitrary one
        computed from an absence — and an autoscaler talking to an overseer that
        does not send peaks yet is exactly this case.
        """
        p = self.policy()
        self.assertEqual(W.desired_replicas(p, W.classify.DIRECTION_DOWN, 20), 19)
        self.assertEqual(W.desired_replicas(p, W.classify.DIRECTION_UP, 20), 30)
        self.assertEqual(
            W.desired_replicas(p, W.classify.DIRECTION_DOWN, 20,
                               peak=(None, None, None)), 19)

    def test_bounds_still_win_over_the_arithmetic(self):
        p = self.policy()
        self.assertEqual(W.bounded(p, W.desired_replicas(
            p, W.classify.DIRECTION_DOWN, 3, peak=(None, 0.0, 0.0))), 2)
        self.assertEqual(W.bounded(p, W.desired_replicas(
            p, W.classify.DIRECTION_UP, 38, held=(None, 200.0, 0.0))), 40)

    def test_a_service_that_is_not_scaled_is_never_moved(self):
        p = W.fixed_policy(4, {}, "fixed")
        for direction in (W.classify.DIRECTION_UP, W.classify.DIRECTION_DOWN):
            self.assertEqual(W.desired_replicas(p, direction, 4,
                                                held=(None, 99.0, 99.0),
                                                peak=(None, 0.0, 0.0)), 4)


class StabilizerTest(unittest.TestCase):
    """
    A shrink has to keep being the answer before it is acted on.

    `sustain_down` damps the SIGNAL; this damps the RECOMMENDATION. It only
    became worth having once the count stopped being a flat -1: a metric that
    oscillates either side of the window boundary now produces a count that
    oscillates with it.
    """

    def setUp(self):
        self.s = W.Stabilizer()

    def test_growth_is_never_delayed(self):
        """Slow to add capacity is an outage; slow to remove it is a bill."""
        self.assertEqual(self.s.stabilise("api", 8, 4, 300, 1000.0), 8)
        self.assertEqual(self.s.stabilise("api", 9, 8, 300, 1001.0), 9)

    def test_a_shrink_waits_for_the_window_to_forget_the_larger_answer(self):
        self.s.stabilise("api", 10, 10, 300, 1000.0)
        # One quiet minute is not enough to shed capacity.
        self.assertEqual(self.s.stabilise("api", 4, 10, 300, 1060.0), 10)
        self.assertEqual(self.s.stabilise("api", 4, 10, 300, 1299.0), 10)
        # Past the window, the larger answer is gone and the shrink lands.
        self.assertEqual(self.s.stabilise("api", 4, 10, 300, 1301.0), 4)

    def test_one_spike_inside_the_window_holds_the_whole_shrink(self):
        """The oscillating metric this exists for."""
        self.s.stabilise("api", 4, 10, 300, 1000.0)
        self.s.stabilise("api", 10, 10, 300, 1100.0)     # the spike
        self.assertEqual(self.s.stabilise("api", 4, 10, 300, 1200.0), 10)

    def test_it_remembers_the_raw_answer_and_never_its_own_output(self):
        """
        Recording the damped value would make the window self-reinforcing: a
        held-back shrink would keep re-recording the larger number and the
        service could never shrink at all.
        """
        for at in range(0, 400, 50):
            self.s.stabilise("api", 4, 10, 300, 1000.0 + at)
        self.assertEqual(self.s.stabilise("api", 4, 10, 300, 1400.0), 4)

    def test_a_zero_window_turns_it_off_entirely(self):
        self.s.stabilise("api", 10, 10, 0, 1000.0)
        self.assertEqual(self.s.stabilise("api", 4, 10, 0, 1001.0), 4)

    def test_a_deleted_service_is_forgotten_rather_than_kept_forever(self):
        self.s.stabilise("gone", 10, 10, 300, 1000.0)
        self.s.stabilise("stays", 10, 10, 300, 1000.0)
        self.s.forget({"stays"})
        # A name that comes back is new, not resumed from a stale window.
        self.assertEqual(self.s.stabilise("gone", 4, 10, 300, 1001.0), 4)
        self.assertEqual(self.s.stabilise("stays", 4, 10, 300, 1001.0), 10)

    def test_a_restart_shrinks_sooner_and_never_later(self):
        """
        This is the only state in either loop that is not re-derived every pass.
        Losing it must therefore be safe: an empty history is exactly the
        behaviour this replaced, so a restart mid-decision cannot make the
        cluster do something it would not otherwise have done.
        """
        fresh = W.Stabilizer()
        self.assertEqual(fresh.stabilise("api", 4, 10, 300, 1000.0), 4)
