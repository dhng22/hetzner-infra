"""
Tests for the performance dispatcher.

    python3 -m unittest discover -s dispatcher/tests -v

Run from the repository root: the dispatcher imports `signals`, which is shared
with the autoscaler and lives there.
"""

import importlib.util
import json
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "dispatcher"))

# The docker socket does not exist in a test runner, and importing dispatcher
# opens one at module scope. Stub the client, not the module: everything else in
# the docker package is real, so a typo in an attribute name still fails.
_fake_docker = types.ModuleType("docker")
_fake_docker.DockerClient = lambda **kw: types.SimpleNamespace(services=None)
sys.modules.setdefault("docker", _fake_docker)

# Loaded by PATH, not by name. `dispatcher/` is also a directory on sys.path,
# so `import dispatcher` finds the namespace package rather than the module
# inside it — and the failure is an AttributeError on every symbol, which reads
# like the module is broken rather than like the wrong thing was imported.
_spec = importlib.util.spec_from_file_location(
    "dispatcher_app", os.path.join(ROOT, "dispatcher", "dispatcher.py"))
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)

from signals import classify, discovery  # noqa: E402


def service(name="api_app", **labels):
    base = {"infra.workload": "app", "infra.component": name.split("_")[0]}
    base.update(labels)
    return types.SimpleNamespace(name=name, attrs={"Spec": {
        "Labels": base,
        "TaskTemplate": {"Resources": {
            "Limits": {"NanoCPUs": 80_000_000, "MemoryBytes": 600 << 20}}},
    }})


class WatchedTest(unittest.TestCase):
    def test_policy_is_read_off_the_service_labels(self):
        w = D.Watched(service(**{"autoscale.slo_p95_ms": "800",
                                 "autoscale.up_p95_ratio": "0.5",
                                 "autoscale.busy_cpu_pct": "40"}))
        self.assertEqual(w.budget_ms, 400.0)
        self.assertEqual(w.busy_cpu, 40.0)
        self.assertEqual(w.thresholds.slo_ms, 800.0)

    def test_defaults_match_the_shared_library(self):
        # Two processes reading one service must agree about what "busy" means,
        # or the autoscaler refuses to scale something this says is its own
        # fault and nobody ever finds out.
        w = D.Watched(service())
        self.assertEqual((w.busy_cpu, w.busy_mem), (classify.BUSY_CPU, classify.BUSY_MEM))

    def test_a_junk_threshold_falls_back_instead_of_raising(self):
        w = D.Watched(service(**{"autoscale.slo_p95_ms": "not-a-number"}))
        self.assertEqual(w.thresholds.slo_ms, 500.0)

    def test_mute_list_is_parsed_and_junk_dropped(self):
        w = D.Watched(service(**{"autoscale.mute_causes": "upstream:tikdrama, nonsense"}))
        self.assertEqual(w.muted, frozenset({"upstream:tikdrama"}))


class AttributionTest(unittest.TestCase):
    def setUp(self):
        self.s = D.Watched(service())          # budget 400ms
        discovery.reset_caches()
        self.saved = (D.query.vm_query, D.query.vm_query_map)

    def tearDown(self):
        D.query.vm_query, D.query.vm_query_map = self.saved
        discovery.reset_caches()

    def test_busy_replicas_are_their_own_cause(self):
        self.assertEqual(D.attribute(self.s, 80.0, 5.0, {}, set()),
                         (classify.CAUSE_LOCAL, None))

    def test_memory_alone_makes_it_local(self):
        self.assertEqual(D.attribute(self.s, 2.0, 75.0, {}, set()),
                         (classify.CAUSE_LOCAL, None))

    def test_unknown_utilisation_is_not_busy(self):
        # Missing cadvisor must never read as "the replicas are fine".
        self.assertEqual(D.attribute(self.s, None, None, {}, set()),
                         (classify.CAUSE_UNKNOWN, None))

    def test_a_slow_outbound_timer_names_the_third_party(self):
        D.query.vm_query_map = lambda expr, label=None: {"tikdrama.example": 2200.0}
        deps = {"api_app": [(classify.CAUSE_UPSTREAM, "e", "http_client_requests_seconds", "host")]}
        self.assertEqual(D.attribute(self.s, 3.0, 4.0, deps, set()),
                         (classify.CAUSE_UPSTREAM, "tikdrama.example"))

    def test_an_outbound_timer_inside_budget_is_not_the_cause(self):
        D.query.vm_query_map = lambda expr, label=None: {"tikdrama.example": 12.0}
        deps = {"api_app": [(classify.CAUSE_UPSTREAM, "e", "http_client_requests_seconds", "host")]}
        self.assertEqual(D.attribute(self.s, 3.0, 4.0, deps, set()),
                         (classify.CAUSE_UNKNOWN, None))

    def test_the_worst_dependency_wins_when_several_are_over_budget(self):
        readings = {"db": [(classify.CAUSE_DATABASE, "e1", "mongodb_driver_commands_seconds", "host")],
                    }
        D.query.vm_query_map = lambda expr, label=None: (
            {"mongo.internal": 900.0} if "e1" in expr else {"tikdrama": 2200.0})
        deps = {"api_app": readings["db"] +
                [(classify.CAUSE_UPSTREAM, "e2", "http_client_requests_seconds", "host")]}
        cause, target = D.attribute(self.s, 3.0, 4.0, deps, set())
        self.assertEqual((cause, target), (classify.CAUSE_UPSTREAM, "tikdrama"))

    def test_a_busy_component_is_the_fallback_not_the_first_answer(self):
        self.assertEqual(D.attribute(self.s, 3.0, 4.0, {}, {"documents"}),
                         (classify.CAUSE_DATABASE, "documents"))


class OwnershipTest(unittest.TestCase):
    """Who fixes it. Three answers, one of them a human's problem tonight."""

    def test_nothing_claims_it_so_a_human_hears_about_it(self):
        self.assertEqual(classify.verdict("database", None, frozenset(), set()),
                         (None, True))

    def test_a_manager_claiming_the_cause_silences_it(self):
        # dbmanager's entire integration: one label on its own service.
        self.assertEqual(classify.verdict("database", None, frozenset(), {"database"}),
                         ("claimed", False))

    def test_muting_one_target_does_not_mute_the_cause(self):
        muted = frozenset({"upstream:tikdrama"})
        self.assertEqual(classify.verdict("upstream", "tikdrama", muted, set()),
                         ("muted", False))
        self.assertEqual(classify.verdict("upstream", "stripe", muted, set()),
                         (None, True))

    def test_a_plain_claim_is_accepted(self):
        D.dkr = types.SimpleNamespace(services=types.SimpleNamespace(list=lambda: [
            service("dbmanager_app", **{"infra.handles": "database, upstream",
                                        "infra.handles.port": "9300"})]))
        self.assertEqual(D.claimed_causes(), {"database", "upstream"})


class GaugeTest(unittest.TestCase):
    def setUp(self):
        self.s = D.Watched(service())
        D._said.clear()

    def value(self, gauge, cause):
        return gauge.labels(service="api_app", cause=cause)._value.get()

    def verdict(self, cause, target, direction="hold", reason="idle"):
        return {"service": "api_app", "direction": direction, "reason": reason,
                "cause": cause, "target": target,
                "latency_ms": 904.0, "cpu_pct": 5.0, "mem_pct": 5.0}

    def test_only_the_attributed_cause_is_set(self):
        D.publish(self.s, self.verdict(classify.CAUSE_UPSTREAM, "tikdrama"), None, True)
        self.assertEqual(self.value(D.G_SIGNAL, "upstream"), 1)
        self.assertEqual(self.value(D.G_SIGNAL, "database"), 0)

    def test_a_claimed_cause_signals_but_does_not_alert(self):
        D.publish(self.s, self.verdict(classify.CAUSE_DATABASE, "documents"), "claimed", False)
        self.assertEqual(self.value(D.G_SIGNAL, "database"), 1)
        self.assertEqual(self.value(D.G_UNOWNED, "database"), 0)

    def test_a_muted_cause_signals_but_does_not_alert(self):
        D.publish(self.s, self.verdict(classify.CAUSE_UPSTREAM, "tikdrama"), "muted", False)
        self.assertEqual(self.value(D.G_SIGNAL, "upstream"), 1)
        self.assertEqual(self.value(D.G_UNOWNED, "upstream"), 0)

    def test_recovery_clears_every_cause(self):
        # Without this a service that recovered keeps its last verdict forever:
        # the gauge is only written while something is wrong, so the alert would
        # fire on a problem that ended hours ago.
        D.publish(self.s, self.verdict(classify.CAUSE_UPSTREAM, "tikdrama"), None, True)
        D.quiet(self.s)
        for cause in classify.CAUSES:
            self.assertEqual(self.value(D.G_SIGNAL, cause), 0)
            self.assertEqual(self.value(D.G_UNOWNED, cause), 0)


if __name__ == "__main__":
    unittest.main()


class ManagerDiscoveryTest(unittest.TestCase):
    """Managers are found by label, and told where to send by label."""

    def cluster(self, *services):
        D.dkr = types.SimpleNamespace(
            services=types.SimpleNamespace(list=lambda: list(services)))

    def test_a_claim_plus_a_port_is_a_deliverable_manager(self):
        self.cluster(service("monitoring_autoscaler", **{
            "infra.handles": "local", "infra.handles.port": "9201"}))
        [m] = D.managers()
        self.assertEqual(m.causes, frozenset({"local"}))
        # Service DNS on the overlay. Nothing is configured with an address.
        self.assertEqual(m.url, "http://monitoring_autoscaler:9201/signal")

    def test_the_path_is_overridable(self):
        self.cluster(service("dbm", **{"infra.handles": "database",
                                       "infra.handles.port": "8080",
                                       "infra.handles.path": "/hooks/perf"}))
        self.assertEqual(D.managers()[0].url, "http://dbm:8080/hooks/perf")

    def test_a_claim_with_no_port_is_refused_loudly_not_guessed(self):
        self.cluster(service("dbm", **{"infra.handles": "database"}))
        self.assertEqual(D.managers(), [])

    def test_two_managers_can_claim_different_causes(self):
        self.cluster(
            service("monitoring_autoscaler", **{"infra.handles": "local",
                                                "infra.handles.port": "9201"}),
            service("dbmanager", **{"infra.handles": "database",
                                    "infra.handles.port": "9300"}))
        self.assertEqual(D.claimed_causes(), {"local", "database"})

    def test_a_claim_on_a_target_is_refused(self):
        # A manager handles a KIND of thing. `database:documents` would be a
        # claim nothing could satisfy for the next database somebody creates.
        self.cluster(service("dbm", **{"infra.handles": "database:documents",
                                       "infra.handles.port": "9300"}))
        self.assertEqual(D.managers(), [])


class DeliveryTest(unittest.TestCase):
    """The wire contract, and what happens when it fails."""

    def setUp(self):
        self.sent = []
        self.saved = D.requests.post
        D._said.clear()

    def tearDown(self):
        D.requests.post = self.saved

    def ok(self, url, data=None, **kw):
        self.sent.append((url, json.loads(data)))
        return types.SimpleNamespace(raise_for_status=lambda: None)

    def boom(self, url, data=None, **kw):
        raise OSError("connection refused")

    def manager(self):
        return D.Manager("monitoring_autoscaler", frozenset({"local"}),
                         "http://monitoring_autoscaler:9201/signal")

    def test_a_delivery_carries_the_whole_current_world(self):
        D.requests.post = self.ok
        payload = [{"service": "api_app", "direction": "up", "cause": "local"},
                   {"service": "web_app", "direction": "hold", "cause": "local"}]
        self.assertTrue(D.deliver(self.manager(), payload))
        url, body = self.sent[0]
        self.assertEqual(url, "http://monitoring_autoscaler:9201/signal")
        # LEVEL-TRIGGERED: every service every time, never a delta. A delivery
        # that fails is corrected by the next one rather than lost.
        self.assertEqual([s["service"] for s in body["signals"]], ["api_app", "web_app"])
        self.assertIn("at", body)

    def test_a_failed_delivery_is_reported_not_raised(self):
        D.requests.post = self.boom
        self.assertFalse(D.deliver(self.manager(), [{"service": "api_app"}]))
        self.assertEqual(
            D.G_DELIVERY.labels(manager="monitoring_autoscaler")._value.get(), 0)

    def test_a_recovered_manager_flips_the_gauge_back(self):
        D.requests.post = self.boom
        D.deliver(self.manager(), [{"service": "api_app"}])
        D.requests.post = self.ok
        D.deliver(self.manager(), [{"service": "api_app"}])
        self.assertEqual(
            D.G_DELIVERY.labels(manager="monitoring_autoscaler")._value.get(), 1)

    def test_a_manager_only_receives_the_causes_it_claims(self):
        D.requests.post = self.ok
        D.dkr = types.SimpleNamespace(services=types.SimpleNamespace(list=lambda: [
            service("api_app"),
            service("monitoring_autoscaler", **{"infra.handles": "local",
                                                "infra.handles.port": "9201"}),
            service("dbmanager", **{"infra.handles": "database",
                                    "infra.handles.port": "9300"})]))
        decided = {
            "api_app": {"service": "api_app", "cause": "local", "direction": "up"},
            "web_app": {"service": "web_app", "cause": "database", "direction": "hold"},
        }
        for m in D.managers():
            payload = [v for v in decided.values() if v["cause"] in m.causes]
            if payload:
                D.deliver(m, payload)
        by_url = {url: body for url, body in self.sent}
        self.assertEqual([s["service"] for s in
                          by_url["http://monitoring_autoscaler:9201/signal"]["signals"]],
                         ["api_app"])
        self.assertEqual([s["service"] for s in
                          by_url["http://dbmanager:9300/signal"]["signals"]],
                         ["web_app"])
