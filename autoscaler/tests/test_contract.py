"""
The dispatch boundary, end to end over a real socket.

    python3 -m unittest discover -s autoscaler/tests

The dispatcher decides and POSTs; the autoscaler receives and acts. Those are
two processes in two images, and the thing between them is a JSON body that no
type checker will ever see. So this test runs the REAL receiver on a real port
and calls the REAL `deliver()` against it — if the two ever disagree about the
shape, a scaling decision is silently dropped and the fleet simply stops
responding, which is the hardest possible failure to notice.

It lives in the autoscaler's suite because that image carries both sets of
dependencies: the dispatcher needs docker, requests and prometheus_client, all
of which are here, while hcloud is here and not there.
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

import autoscaler as A  # noqa: E402
from signals import classify  # noqa: E402

# By path: `dispatcher/` is also a directory on sys.path, so `import dispatcher`
# finds the namespace package rather than the module inside it.
_spec = importlib.util.spec_from_file_location(
    "dispatcher_app", os.path.join(ROOT, "dispatcher", "dispatcher.py"))
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)


def workload(name, **policy):
    labels = {"autoscale.enabled": "true"}
    labels.update({f"autoscale.{k}": str(v) for k, v in policy.items()})
    return A.Workload(
        name=name, id=f"id-{name}",
        policy=A.policy_from_labels(name, labels, 2),
        spec_replicas=2, cost=A.Res(int(2e7), 300 << 20), cpu_limit=0.08,
        mem_limit=600 << 20, pinned=False, rolling=False, component="api",
        rolled_back=False, placement_pinned=False)


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
                   "cpu_pct": 40.0, "mem_pct": 12.0}
        verdict.update(extra)
        self.assertTrue(D.deliver(self.manager, [verdict]),
                        "the dispatcher could not deliver to the real receiver")
        return verdict

    def test_a_dispatched_up_arrives_and_scales(self):
        self.dispatch(classify.DIRECTION_UP)
        got = A.dispatched_for("api_app")
        self.assertIsNotNone(got, "the receiver did not store what deliver() sent")
        self.assertEqual(got["direction"], classify.DIRECTION_UP)
        w = workload("api_app", min_replicas=2, max_replicas=8, up_factor=0.5)
        self.assertEqual(A.desired_replicas(w, got, 2), 3)

    def test_a_dispatched_down_arrives_and_shrinks(self):
        self.dispatch(classify.DIRECTION_DOWN)
        w = workload("api_app", min_replicas=1, max_replicas=8)
        self.assertEqual(A.desired_replicas(w, A.dispatched_for("api_app"), 4), 3)

    def test_a_dispatched_hold_moves_nothing(self):
        self.dispatch(classify.DIRECTION_HOLD, reason="replicas are idle",
                      cause=classify.CAUSE_UPSTREAM)
        w = workload("api_app", min_replicas=1, max_replicas=8)
        self.assertEqual(A.desired_replicas(w, A.dispatched_for("api_app"), 4), 4)

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
                    "latency_ms", "cpu_pct", "mem_pct"):
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

    def test_a_dispatcher_that_stops_leaves_no_verdict_at_all(self):
        """
        The point of the whole restructure. No signal, no action — the fleet
        stops changing rather than falling back to a worse rule.
        """
        import time
        self.dispatch(classify.DIRECTION_UP)
        stale = time.time() + A.SIGNAL_TTL_SECONDS + 1
        self.assertIsNone(A.dispatched_for("api_app", now=stale))
        w = workload("api_app", min_replicas=2, max_replicas=8)
        self.assertEqual(A.desired_replicas(w, A.dispatched_for("api_app", now=stale), 5), 5)


if __name__ == "__main__":
    unittest.main()
