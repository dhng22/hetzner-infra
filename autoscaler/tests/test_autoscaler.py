"""
Tests for what the autoscaler APPLIES.

    python3 -m unittest discover -s autoscaler/tests -v

The fleet arithmetic used to live here and now lives in overseer/tests: this
process no longer sizes anything. What is left is the half that writes to Swarm
— turning a dispatched direction and ceiling into a replica count, moving a
placement constraint, and measuring reservations.

docker-py negotiates the API version when the client is constructed, so the
module cannot be imported without a socket. It is stubbed before import.
"""

import json
import os
import sys
import time
import types
import unittest
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
# The repository root, for `signals` — shared with the overseer and dataguard,
# so it lives beside all three rather than inside any of them.
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

os.environ.setdefault("APP_NAME", "testcluster")

import docker  # noqa: E402

docker.DockerClient = lambda *a, **kw: types.SimpleNamespace(  # noqa: E731
    api=types.SimpleNamespace(), nodes=types.SimpleNamespace(),
    services=types.SimpleNamespace(), swarm=types.SimpleNamespace(),
    info=lambda: {},
)

import autoscaler as A  # noqa: E402
from signals import workloads as W  # noqa: E402




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


class RightSizingTest(unittest.TestCase):
    """
    Reservations measured instead of typed.

    The case that produced this: a component shipped with the form's default of
    0.5 CPU / 384MB, then ran at 0.008 cores and 151MB. The master had 0.19 CPU
    free, the reservation claimed 0.36, and so a worker was billed around the
    clock to hold one idle replica of an app using a fortieth of a core.
    """

    GB = 1024 ** 3

    def size(self, cpu_q, mem_mb, node_cpu=2, node_mem_gb=4,
             throttled_pct=0.0, cpu_limit_now=0.0):
        return A.right_size(cpu_q, mem_mb * 1024 * 1024,
                            int(node_cpu * 1e9), int(node_mem_gb * self.GB),
                            throttled_pct, cpu_limit_now)

    def test_a_throttled_service_is_believed_over_its_own_cpu_reading(self):
        """
        THE TRAP THIS EXISTS FOR. Every other input here is CPU *consumed*, and
        a container at its cap cannot consume past the cap — so a cap sized from
        consumption is self-confirming and a strangled service looks content
        forever.

        Live shape: an I/O-bound API measured 0.003 cores because it spends its
        life waiting on the network. Floored to a 0.02 reservation, that gave a
        0.08 limit — 8ms of CPU per 100ms period — so requests needing 30ms of
        CPU got chopped across five periods. 70% of requests under 5ms, NOTHING
        between 100 and 250ms, then a hard cluster at 250-500ms: not a tail, a
        second population quantised by the scheduler.
        """
        _, _, strangled, _ = self.size(0.003, 300)
        self.assertLess(strangled, 0.1)               # what usage alone concludes
        _, _, relieved, _ = self.size(0.003, 300, throttled_pct=12.0,
                                      cpu_limit_now=strangled)
        self.assertGreaterEqual(relieved, A.CPU_LIMIT_RELIEF_FLOOR)
        self.assertGreater(relieved, strangled * 4)

    def test_relief_keeps_climbing_for_an_appetite_the_floor_does_not_cover(self):
        # A service still throttled after the first raise needs more than the
        # floor, and the only evidence of how much more is that it is STILL
        # throttled. Two steps, not a twenty-percent crawl up an hourly cooldown.
        _, _, once, _ = self.size(0.003, 300, throttled_pct=12.0, cpu_limit_now=0.08)
        _, _, twice, _ = self.size(0.003, 300, throttled_pct=12.0, cpu_limit_now=once)
        self.assertGreater(twice, once)

    def test_a_raised_cap_is_not_walked_back_when_the_throttling_stops(self):
        """
        Throttling stopping is what the raise was FOR. Reading it as proof the
        raise was unnecessary drops the cap, re-throttles the service and
        oscillates on an hourly cycle — each turn of which restarts every
        replica. Swarm packs on reservations, so an unused ceiling costs
        nothing; a wrong one costs half a second a request.
        """
        _, _, kept, _ = self.size(0.003, 300, throttled_pct=0.0, cpu_limit_now=0.5)
        self.assertGreaterEqual(kept, 0.5)

    def test_relief_still_cannot_exceed_what_the_node_has(self):
        _, _, cap, _ = self.size(0.003, 300, node_cpu=2, throttled_pct=90.0,
                                 cpu_limit_now=1.9)
        self.assertLessEqual(cap, 2.0)

    def test_a_cap_change_alone_is_enough_to_apply_a_resize(self):
        # The limit used to be a pure function of the reservation, so testing
        # the reservation was the same test. A throttled service now needs a
        # bigger ceiling while its measured usage — capped by that ceiling — has
        # not moved at all, and that resize must still happen.
        self.assertTrue(A._changed_enough(0.08, A.CPU_LIMIT_RELIEF_FLOOR))

    def test_the_real_measurement_fits_on_the_master(self):
        cpu_res, mem_res, _, _ = self.size(0.0658, 151)
        self.assertLess(cpu_res, 0.19)      # what the master actually had free
        self.assertLess(mem_res, 870)

    def test_memory_is_sized_from_the_peak_not_a_quantile(self):
        """Memory is incompressible: under-reserving it is an OOM kill."""
        _, mem_res, _, _ = self.size(0.01, 200)
        self.assertGreaterEqual(mem_res, 200)

    def test_cpu_limit_leaves_room_for_a_startup_burst(self):
        """
        A JVM peaks far above its steady state while it compiles. That belongs
        in the limit; paying for it in the reservation forever is the bug.
        """
        cpu_res, _, cpu_lim, _ = self.size(0.0658, 151)
        self.assertGreater(cpu_lim, 0.376)   # the measured JVM startup peak
        self.assertGreater(cpu_lim, cpu_res)

    def test_floors_apply_to_an_idle_component(self):
        cpu_res, mem_res, _, _ = self.size(0.0, 1)
        self.assertGreaterEqual(cpu_res, A.CPU_RESERVE_FLOOR)
        self.assertGreaterEqual(mem_res, A.MEM_RESERVE_FLOOR_MB)

    def test_never_reserves_more_than_a_node_can_give(self):
        """
        A reservation no node can satisfy is not a size, it is a task that sits
        Pending forever while the panel reports the component down for no
        visible reason.
        """
        cpu_res, mem_res, _, _ = self.size(100, 999999, node_cpu=2, node_mem_gb=4)
        self.assertLessEqual(cpu_res, 1.0)          # half of a 2-core node
        self.assertLessEqual(mem_res, 2048)         # half of a 4GB node

    def test_small_drift_does_not_trigger_a_restart(self):
        """Every resize restarts every replica, so it must not chase noise."""
        self.assertFalse(A._changed_enough(0.10, 0.11))
        self.assertTrue(A._changed_enough(0.10, 0.50))

    def test_growth_from_nothing_always_counts_as_a_change(self):
        self.assertTrue(A._changed_enough(0, 0.05))
        self.assertTrue(A._changed_enough(None, 0.05))


class HandoverNudgeTest(unittest.TestCase):
    """
    A stalled handover has to act, not just complain.

    Releasing the worker pin does not move a running task — Swarm places a task
    when it is created and never rebalances one. So "wait for a replica on the
    master" was waiting for something that could not happen: 37 minutes of an
    error every loop, resolved only when an unrelated resize recreated the tasks
    and they landed there by accident.
    """

    def setUp(self):
        A._last_handover_nudge.clear()
        self.calls = []
        self._run = A.subprocess.run
        A.subprocess.run = self._fake

    def tearDown(self):
        A.subprocess.run = self._run
        A._last_handover_nudge.clear()

    def _fake(self, cmd, **kw):
        self.calls.append(cmd)
        class R:
            returncode = 0
            stdout = stderr = ""
        return R()

    def test_forces_a_replacement(self):
        w = workload("api_app")
        self.assertEqual(A.handover_nudge(["api_app"], [w]), ["api_app"])
        self.assertEqual(len(self.calls), 1)
        # --force is the whole point: it recreates tasks so placement is
        # reconsidered. Detached so the loop is not held for a rollout.
        self.assertIn("--force", self.calls[0])
        self.assertIn("--detach=true", self.calls[0])
        self.assertIn("api_app", self.calls[0])

    def test_does_not_nudge_twice_inside_the_cooldown(self):
        w = workload("api_app")
        A.handover_nudge(["api_app"], [w])
        self.assertEqual(A.handover_nudge(["api_app"], [w]), [])
        self.assertEqual(len(self.calls), 1)

    def test_never_nudges_a_service_that_is_already_rolling(self):
        """Forcing an update mid-rollout restarts it from the beginning."""
        w = workload("api_app")._replace(rolling=True)
        self.assertEqual(A.handover_nudge(["api_app"], [w]), [])
        self.assertEqual(self.calls, [])

    def test_dry_run_touches_nothing(self):
        A.DRY_RUN = True
        try:
            self.assertEqual(A.handover_nudge(["api_app"], [workload("api_app")]),
                             ["api_app"])
            self.assertEqual(self.calls, [])
        finally:
            A.DRY_RUN = False


_UNSET = object()


def verdict(direction="hold", reason="", cause="local", target=None,
            latency_ms=None, cpu_pct=None, mem_pct=None,
            replica_ceiling=_UNSET, pinned=None):
    """
    A delivery as the overseer sends it.

    `replica_ceiling` defaults to ABSENT rather than None, because the two mean
    different things on the wire and the difference is load-bearing: absent is
    an older overseer that does not send one, None is a current one saying it
    could not read the fleet. Both must read as "no cap", never as zero.
    """
    body = {"service": "api_app", "direction": direction, "reason": reason,
            "cause": cause, "target": target, "latency_ms": latency_ms,
            "cpu_pct": cpu_pct, "mem_pct": mem_pct, "pinned": pinned}
    if replica_ceiling is not _UNSET:
        body["replica_ceiling"] = replica_ceiling
    return body


class DispatchedScalingTest(unittest.TestCase):
    """
    The autoscaler no longer judges whether a service is slow, and no longer
    decides what fits. It receives a DIRECTION and a CEILING, and turns them
    into a count inside that service's own bounds.
    """

    def setUp(self):
        self.w = workload("api_app", min_replicas=2, max_replicas=8, up_factor=0.5)

    def at(self, current, **kw):
        return A.target_replicas(self.w._replace(spec_replicas=current),
                                 verdict(**kw) if kw else None)

    def test_up_grows_by_the_factor_with_a_floor_of_one(self):
        self.assertEqual(self.at(2, direction="up"), 3)
        self.assertEqual(self.at(4, direction="up"), 6)

    def test_up_never_passes_the_ceiling(self):
        self.assertEqual(self.at(8, direction="up"), 8)

    def test_down_steps_one_at_a_time_and_stops_at_the_floor(self):
        self.assertEqual(self.at(4, direction="down"), 3)
        self.assertEqual(self.at(2, direction="down"), 2)

    def test_hold_does_nothing(self):
        self.assertEqual(self.at(4, direction="hold"), 4)

    def test_NO_VERDICT_HOLDS(self):
        """
        The shape of the whole split. A missing verdict is "nobody has told us
        anything", not "nothing is wrong" — so an overseer outage stops the
        cluster changing instead of returning this loop to a worse rule at the
        moment it is least able to afford it.
        """
        for current in (1, 2, 5, 9):
            self.assertEqual(self.at(current), current)

    def test_a_disabled_service_ignores_even_a_dispatched_verdict(self):
        fixed = workload("fixed_app", enabled="false", spec_replicas=3)
        self.assertEqual(A.target_replicas(fixed, verdict(direction="up")), 3)

    def test_the_dispatched_ceiling_caps_growth(self):
        """The overseer knows what fits; this loop is not allowed to exceed it."""
        self.assertEqual(self.at(4, direction="up", replica_ceiling=5), 5)

    def test_the_ceiling_never_forces_a_shrink(self):
        """
        A ceiling below what is RUNNING is a node that has gone away, and the
        answer to that is a graceful drain, not this loop discovering that four
        no longer fit and cutting to two in one step.
        """
        self.assertEqual(self.at(4, direction="hold", replica_ceiling=2), 4)
        self.assertEqual(self.at(4, direction="up", replica_ceiling=1), 4)

    def test_a_ceiling_still_allows_the_shrink_the_signals_asked_for(self):
        self.assertEqual(self.at(4, direction="down", replica_ceiling=2), 3)

    def test_no_ceiling_at_all_is_not_zero(self):
        """
        The overseer sends None when it could not read the fleet. That must mean
        "unknown", never "nothing fits" — reading it as a number would drain
        every service the first time the Hetzner API timed out.
        """
        self.assertEqual(self.at(2, direction="up", replica_ceiling=None), 3)


class ReceiverTest(unittest.TestCase):
    """Deliveries are level-triggered and they expire."""

    def setUp(self):
        A._dispatched.clear()

    def tearDown(self):
        A._dispatched.clear()

    def test_a_delivery_is_readable_back(self):
        n = A.record_dispatch({"signals": [verdict("up", reason="busy")]})
        self.assertEqual(n, 1)
        self.assertEqual(A.dispatched_for("api_app")["direction"], "up")

    def test_a_later_delivery_replaces_an_earlier_one(self):
        A.record_dispatch({"signals": [verdict("up")]})
        A.record_dispatch({"signals": [verdict("down")]})
        self.assertEqual(A.dispatched_for("api_app")["direction"], "down")

    def test_a_stale_verdict_is_no_verdict(self):
        A.record_dispatch({"signals": [verdict("up")]})
        future = time.time() + A.SIGNAL_TTL_SECONDS + 1
        self.assertIsNone(A.dispatched_for("api_app", now=future))

    def test_a_service_nobody_mentioned_has_no_verdict(self):
        A.record_dispatch({"signals": [verdict("up")]})
        self.assertIsNone(A.dispatched_for("other_app"))

    def test_an_entry_without_a_service_name_is_dropped_not_stored(self):
        A.record_dispatch({"signals": [{"direction": "up"}]})
        self.assertEqual(A._dispatched, {})


class ManagedServiceRefusalTest(unittest.TestCase):
    """
    The guard whose accident is corruption rather than an outage.

    A dataguard member service is one mongod with one volume. Scaling it to two
    replicas starts a SECOND mongod on the same data directory. These services
    already carry no `infra.workload=app` label and so are already invisible to
    discovery — this is the explicit refusal for the case where one is
    mislabelled, because relying on an absence is not a guard.
    """

    def setUp(self):
        self._dkr = A.dkr
        A._warned.clear()

    def tearDown(self):
        A.dkr = self._dkr
        A._warned.clear()

    def _cluster(self, **labels):
        service = types.SimpleNamespace(name="docs_mongo-1", id="id-m1", attrs={
            "Spec": {"Labels": labels}})
        A.dkr = types.SimpleNamespace(
            services=types.SimpleNamespace(get=lambda n: service))

    def test_a_dataguard_managed_service_is_refused(self):
        self._cluster(**{W.MANAGED_BY_LABEL: W.MANAGED_BY_DATAGUARD,
                         W.WORKLOAD_LABEL: W.WORKLOAD_APP})
        before = A.M_REFUSED.labels(reason="dataguard")._value.get()
        self.assertTrue(A.refuse_if_managed("docs_mongo-1"))
        self.assertEqual(A.M_REFUSED.labels(reason="dataguard")._value.get(), before + 1)

    def test_an_ordinary_application_is_not_refused(self):
        self._cluster(**{W.WORKLOAD_LABEL: W.WORKLOAD_APP})
        self.assertFalse(A.refuse_if_managed("api_app"))

    def test_a_service_that_cannot_be_read_is_not_refused(self):
        """
        An unreadable service is not evidence of anything. Refusing on a docker
        API blip would silently stop scaling the whole cluster, which is a much
        worse failure than the one this guard exists for — and that one is
        already prevented by the label being absent.
        """
        def boom(_name):
            raise RuntimeError("docker is having a moment")
        A.dkr = types.SimpleNamespace(services=types.SimpleNamespace(get=boom))
        self.assertFalse(A.refuse_if_managed("api_app"))


if __name__ == "__main__":
    unittest.main()
