"""
The dispatch boundary, end to end over a real socket.

    python3 -m unittest discover -s overseer/tests

The overseer decides and POSTs; the autoscaler receives and acts. Those are two
processes in two images, and the thing between them is a JSON body that no type
checker will ever see. So this test runs the REAL receiver on a real port and
calls the REAL `deliver()` against it — if the two ever disagree about the
shape, a scaling decision is silently dropped and the cluster simply stops
responding, which is the hardest possible failure to notice.

It lives in the OVERSEER's suite, and it moved here when the fleet did: this
image carries both sets of dependencies now. The overseer needs hcloud, which
the autoscaler image no longer has, while everything autoscaler.py imports —
docker, requests, prometheus_client — is here too.
"""

import importlib.util
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, ROOT)

os.environ.setdefault("HCLOUD_TOKEN", "test-token")
os.environ.setdefault("APP_NAME", "testcluster")

import docker  # noqa: E402

docker.DockerClient = lambda *a, **kw: types.SimpleNamespace(  # noqa: E731
    api=types.SimpleNamespace(), nodes=types.SimpleNamespace(),
    services=types.SimpleNamespace(), swarm=types.SimpleNamespace(), info=lambda: {},
)

import overseer as D  # noqa: E402
from signals import classify, workloads as W  # noqa: E402

# By path: `autoscaler/` is a directory, not this image's own module, and the
# two register different metric names so there is no registry clash.
_spec = importlib.util.spec_from_file_location(
    "autoscaler_app", os.path.join(ROOT, "autoscaler", "autoscaler.py"))
A = importlib.util.module_from_spec(_spec)
sys.modules["autoscaler_app"] = A
_spec.loader.exec_module(A)


def workload(name, **policy):
    labels = {"autoscale.enabled": "true"}
    labels.update({f"autoscale.{k}": str(v) for k, v in policy.items()})
    return W.Workload(
        name=name, id=f"id-{name}",
        policy=W.policy_from_labels(name, labels, 2),
        spec_replicas=2, cost=W.Res(int(2e7), 300 << 20), cpu_limit=0.08,
        mem_limit=600 << 20, pinned=False, rolling=False, component="api",
        rolled_back=False, placement_pinned=False, muted=frozenset())


class ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Port 0: the OS picks a free one, so the suite cannot collide with a
        # real autoscaler or with a parallel run of itself.
        A.SIGNAL_PORT = 0
        cls.server = A.serve_signals()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        A._dispatched.clear()
        self.manager = D.Manager("autoscaler", frozenset({classify.CAUSE_LOCAL}),
                                 f"http://127.0.0.1:{self.port}/signal")

    def dispatch(self, direction, reason="because", cause="local", **extra):
        verdict = {"service": "api_app", "direction": direction, "reason": reason,
                   "cause": cause, "target": None, "latency_ms": 904.0,
                   "cpu_pct": 40.0, "mem_pct": 12.0,
                   "replica_ceiling": 8, "pinned": False}
        verdict.update(extra)
        self.assertTrue(D.deliver(self.manager, [verdict]),
                        "the overseer could not deliver to the real receiver")
        return verdict

    def test_a_dispatched_up_arrives_and_scales(self):
        self.dispatch(classify.DIRECTION_UP)
        got = A.dispatched_for("api_app")
        self.assertIsNotNone(got, "the receiver did not store what deliver() sent")
        self.assertEqual(got["direction"], classify.DIRECTION_UP)
        w = workload("api_app", min_replicas=2, max_replicas=8, up_factor=0.5)
        self.assertEqual(A.target_replicas(w, got), 3)

    def test_a_dispatched_down_arrives_and_shrinks(self):
        self.dispatch(classify.DIRECTION_DOWN)
        w = workload("api_app", min_replicas=1, max_replicas=8)._replace(spec_replicas=4)
        self.assertEqual(A.target_replicas(w, A.dispatched_for("api_app")), 3)

    def test_a_dispatched_hold_moves_nothing(self):
        self.dispatch(classify.DIRECTION_HOLD, reason="replicas are idle",
                      cause=classify.CAUSE_UPSTREAM)
        w = workload("api_app", min_replicas=1, max_replicas=8)._replace(spec_replicas=4)
        self.assertEqual(A.target_replicas(w, A.dispatched_for("api_app")), 4)

    def test_the_numbers_survive_the_wire_for_the_panel(self):
        # The autoscaler stopped querying these; they arrive in the verdict
        # precisely so `autoscaler_service_*` keeps meaning what it meant.
        self.dispatch(classify.DIRECTION_UP)
        got = A.dispatched_for("api_app")
        self.assertEqual((got["latency_ms"], got["cpu_pct"], got["mem_pct"]),
                         (904.0, 40.0, 12.0))

    def test_every_field_the_receiver_reads_is_a_field_the_sender_sends(self):
        """
        The contract itself. Both sides name these keys in code that never meets,
        so the set is asserted rather than assumed.
        """
        self.dispatch(classify.DIRECTION_UP)
        got = A.dispatched_for("api_app")
        for key in ("service", "direction", "reason", "cause", "target",
                    "latency_ms", "cpu_pct", "mem_pct",
                    # Added when the fleet moved: the autoscaler no longer packs
                    # anything, so what fits and where it may run have to cross
                    # the wire or it cannot act on either.
                    "replica_ceiling", "pinned"):
            self.assertIn(key, got, f"the wire lost {key}")

    def test_a_second_delivery_replaces_the_first(self):
        self.dispatch(classify.DIRECTION_UP)
        self.dispatch(classify.DIRECTION_DOWN)
        self.assertEqual(A.dispatched_for("api_app")["direction"],
                         classify.DIRECTION_DOWN)

    def test_an_undeliverable_manager_is_reported_not_raised(self):
        # Nothing is queued or retried: the next delivery a minute later carries
        # the same world, and a retry would deliver a stale one.
        dead = D.Manager("gone", frozenset({"local"}), "http://127.0.0.1:1/signal")
        self.assertFalse(D.deliver(dead, [{"service": "api_app"}]))

    def test_the_receiver_refuses_a_body_that_is_not_a_dispatch(self):
        import requests
        url = f"http://127.0.0.1:{self.port}/signal"
        self.assertEqual(requests.post(url, data=b"{{{", timeout=5).status_code, 400)
        self.assertEqual(requests.post(url, data=b"", timeout=5).status_code, 400)
        # And a refusal must not poison what was already accepted.
        self.dispatch(classify.DIRECTION_UP)
        requests.post(url, data=b"nonsense", timeout=5)
        self.assertEqual(A.dispatched_for("api_app")["direction"], classify.DIRECTION_UP)

    def test_a_overseer_that_stops_leaves_no_verdict_at_all(self):
        """
        The point of the whole restructure. No signal, no action — the fleet
        stops changing rather than falling back to a worse rule.
        """
        import time
        self.dispatch(classify.DIRECTION_UP)
        stale = time.time() + A.SIGNAL_TTL_SECONDS + 1
        self.assertIsNone(A.dispatched_for("api_app", now=stale))
        w = workload("api_app", min_replicas=2, max_replicas=8)._replace(spec_replicas=5)
        self.assertEqual(A.target_replicas(w, A.dispatched_for("api_app", now=stale)), 5)


    def test_the_ceiling_crosses_the_wire_and_caps_the_count(self):
        """
        The half of the split that is new. The overseer packs; the autoscaler
        must not exceed what it was told fits, or the extra replicas sit pending
        forever and look exactly like a healthy scale-up.
        """
        self.dispatch(classify.DIRECTION_UP, replica_ceiling=3)
        w = workload("api_app", min_replicas=2, max_replicas=8, up_factor=1.0)
        self.assertEqual(A.target_replicas(w, A.dispatched_for("api_app")), 3)

    def test_a_null_ceiling_means_unknown_and_never_zero(self):
        """
        The overseer sends None when it could not read the fleet. Reading that
        as a number would drain every service in the cluster the first time the
        Hetzner API timed out.
        """
        self.dispatch(classify.DIRECTION_UP, replica_ceiling=None)
        w = workload("api_app", min_replicas=2, max_replicas=8, up_factor=0.5)
        self.assertEqual(A.target_replicas(w, A.dispatched_for("api_app")), 3)


if __name__ == "__main__":
    unittest.main()
