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
