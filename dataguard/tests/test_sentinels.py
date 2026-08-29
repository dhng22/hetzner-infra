"""
Tests for the Redis sentinel quorum — how many run, and when they start.

    python3 -m unittest discover -s dataguard/tests -v

Two bugs live here, and they are the same bug seen from two sides.

The first is arithmetic: three sentinels started with the component, so a
single-server Redis on a single-node cluster ran three quorum members on the one
machine they were watching. That is not a quorum, it is one point of failure
counted three times, guarding a server with no replica to promote.

The second is identity, and it is the dangerous one: a sentinel service carries
the same `infra.component`, `infra.type` and `dataguard.member` as a data member
does. Discovery keyed on exactly those, so `members[2]` was whichever of
`cache_redis-2` and `cache_sentinel-2` Docker happened to list second — and with
the sentinels running and the data members stopped, dataguard read a one-server
component as a three-member set that needed nothing.
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


class FakeService:
    """A docker-py service, reduced to the two things discovery reads."""

    def __init__(self, name, labels, replicas):
        self.name = name
        self.attrs = {"Spec": {"Labels": labels,
                               "Mode": {"Replicated": {"Replicas": replicas}}}}


def member(component, index, replicas, kind="redis"):
    return FakeService(f"{component}_{kind}-{index}", {
        "infra.managed_by": "dataguard",
        "infra.component": component,
        "infra.type": kind,
        "dataguard.member": str(index),
        "dataguard.role": "member",
        "dataguard.pool": "4",
    }, replicas)


def sentinel(component, index, replicas):
    return FakeService(f"{component}_sentinel-{index}", {
        "infra.managed_by": "dataguard",
        "infra.component": component,
        "infra.type": "redis",
        "dataguard.member": str(index),
        "dataguard.role": "sentinel",
    }, replicas)


def viewer(component):
    return FakeService(f"{component}_viewer", {
        "infra.managed_by": "dataguard",
        "infra.component": component,
        "infra.type": "redis",
        "dataguard.role": "viewer",
    }, 1)


class DiscoveryTest(unittest.TestCase):
    def discover(self, services):
        D.dkr = types.SimpleNamespace(services=types.SimpleNamespace(
            list=lambda: services))
        return D.managed_components()

    def test_a_sentinel_is_not_counted_as_a_data_member(self):
        """
        The live shape this got wrong: one server up, three sentinels up, three
        servers stopped. Read as members, the sentinels made it a three-member
        set — so nothing would ever grow it, and a shrink would have scaled the
        quorum away believing it was stopping a database.

        Sentinels are LISTED SECOND here on purpose. `setdefault(...)[index]`
        meant last-write-wins, so the order Docker happens to return decided
        which object dataguard held for member 2.
        """
        found = self.discover([
            member("cache", 1, 1), member("cache", 2, 0), member("cache", 3, 0),
            sentinel("cache", 1, 1), sentinel("cache", 2, 1), sentinel("cache", 3, 1),
        ])
        component = found["cache"]
        self.assertEqual(sorted(component.services), [1, 2, 3])
        self.assertEqual(component.live_members(), [1])
        self.assertEqual(sorted(component.sentinels), [1, 2, 3])
        self.assertEqual(component.live_sentinels(), [1, 2, 3])
        for index, service in component.services.items():
            self.assertEqual(service.name, f"cache_redis-{index}")

    def test_a_member_with_no_role_label_is_still_a_member(self):
        """
        Every member service deployed before the role label existed has none.
        Requiring it would have made dataguard see zero databases until each one
        was saved again in the panel, which is the loudest possible way to do
        nothing.
        """
        stale = member("cache", 1, 1)
        del stale.attrs["Spec"]["Labels"]["dataguard.role"]
        found = self.discover([stale])
        self.assertEqual(sorted(found["cache"].services), [1])

    def test_a_viewer_is_not_a_member_either(self):
        """It has no member index, and it must not acquire one by accident."""
        found = self.discover([member("cache", 1, 1), viewer("cache")])
        self.assertEqual(sorted(found["cache"].services), [1])
        self.assertEqual(found["cache"].sentinels, {})

    def test_mongo_has_no_sentinels_and_that_is_not_an_error(self):
        found = self.discover([member("docs", 1, 1, kind="mongo"),
                               member("docs", 2, 0, kind="mongo")])
        component = found["docs"]
        self.assertEqual(component.sentinels, {})
        self.assertEqual(D.sentinels_wanted(component), 0)


class Updates:
    """Records `docker service update` instead of running it."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, name, *args):
        if self.fail:
            raise D.engines.Refused("swarm said no")
        self.calls.append((name, args))
        return True

    def replicas(self):
        return {name: args[-1] for name, args in self.calls}


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.updates = Updates()
        self._real = D._service_update
        D._service_update = self.updates

    def tearDown(self):
        D._service_update = self._real

    def component(self, live_members, live_sentinels, enabled=True):
        members = {i: member("cache", i, 1 if i in live_members else 0)
                   for i in (1, 2, 3, 4)}
        sentinels = {i: sentinel("cache", i, 1 if i in live_sentinels else 0)
                     for i in (1, 2, 3)}
        component = D.Component("cache", "redis", members, sentinels=sentinels)
        component.enabled = enabled
        return component

    def test_one_server_wants_one_sentinel(self):
        component = self.component(live_members=[1], live_sentinels=[1, 2, 3])
        self.assertEqual(D.sentinels_wanted(component), 1)
        D.reconcile_sentinels(component)
        self.assertEqual(self.updates.replicas(),
                         {"cache_sentinel-2": "0", "cache_sentinel-3": "0"})

    def test_two_servers_want_the_whole_quorum(self):
        component = self.component(live_members=[1, 2], live_sentinels=[1])
        self.assertEqual(D.sentinels_wanted(component), 3)
        D.reconcile_sentinels(component)
        self.assertEqual(self.updates.replicas(),
                         {"cache_sentinel-2": "1", "cache_sentinel-3": "1"})

    def test_a_settled_component_is_left_alone(self):
        """Level-triggered, so this runs every loop. It must be a no-op."""
        D.reconcile_sentinels(self.component(live_members=[1], live_sentinels=[1]))
        D.reconcile_sentinels(self.component(live_members=[1, 2],
                                             live_sentinels=[1, 2, 3]))
        self.assertEqual(self.updates.calls, [])

    def test_dataguard_being_off_for_a_component_means_hands_off(self):
        D.reconcile_sentinels(self.component(live_members=[1],
                                             live_sentinels=[1, 2, 3],
                                             enabled=False))
        self.assertEqual(self.updates.calls, [])

    def test_a_refused_update_stops_rather_than_carrying_on(self):
        """
        Half a quorum change is worse than none: if sentinel 2 will not start,
        starting 3 as well leaves two sentinels — an even count that agrees on
        nothing — instead of the one that was working.
        """
        D._service_update = self.updates = Updates(fail=True)
        D.reconcile_sentinels(self.component(live_members=[1, 2], live_sentinels=[1]))
        self.assertEqual(self.updates.calls, [])

    def test_the_quorum_comes_up_before_the_second_server(self):
        """
        Ordering, and it is the whole reason `want` can be passed explicitly.

        Deriving the count from what is running would start the sentinels on the
        loop AFTER the second server started — a minute in which a two-member
        set has one sentinel and a quorum of two, so a primary that died in that
        window would not be failed over.
        """
        component = self.component(live_members=[1], live_sentinels=[1])
        started = []
        real_start, D.start_member = D.start_member, (
            lambda c, i, host: started.append(
                ("member", i, list(self.updates.replicas()))))
        try:
            D.reconcile_sentinels(component, want=len(component.sentinels))
            D.start_member(component, 2, None)
        finally:
            D.start_member = real_start
        self.assertEqual(started, [("member", 2,
                                    ["cache_sentinel-2", "cache_sentinel-3"])])


class EngineTest(unittest.TestCase):
    def test_the_engine_only_talks_to_sentinels_that_are_running(self):
        """
        The list was `range(1, 4)`, hardcoded. With two of the three stopped that
        is two names that do not resolve on every single topology read, which is
        two DNS timeouts a minute for the whole time a component sits at one
        server — which is most of its life.
        """
        members = {1: member("cache", 1, 1)}
        sentinels = {i: sentinel("cache", i, 1 if i == 1 else 0) for i in (1, 2, 3)}
        component = D.Component("cache", "redis", members, sentinels=sentinels)
        captured = {}

        class FakeSentinel:
            def __init__(self, hosts, **kwargs):
                captured["hosts"] = hosts

        import redis.sentinel
        real = redis.sentinel.Sentinel
        redis.sentinel.Sentinel = FakeSentinel
        try:
            D.engine_for(component)._s()
        finally:
            redis.sentinel.Sentinel = real
        self.assertEqual(captured["hosts"], [("cache_sentinel-1", 26379)])


if __name__ == "__main__":
    unittest.main()
