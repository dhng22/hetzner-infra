"""
A deploy that lost a version race is retried, not reported as a failure.
"""
import os
import sys
import unittest

_ADMIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ADMIN)
sys.path.insert(1, os.path.dirname(_ADMIN))

CONTENDED = ("failed to update service redis-caches_redis-3: Error response "
             "from daemon: rpc error: code = Unknown desc = update out of sequence")


class DeployRetryTest(unittest.TestCase):
    """
    `docker stack deploy` reads every service's version up front and then
    applies them one at a time. A managed database's members belong to
    dataguard, which scales and promotes them on its own loop, so a deploy can
    be overtaken mid-flight — Swarm then refuses the stale write and the deploy
    stops with some services updated and the rest not.

    Seen on the live cluster: a Redis whose cache size had changed and whose
    memory reservation had not, reported as "the deploy failed", which is
    indistinguishable from a spec the cluster rejected.
    """

    def setUp(self):
        for module in [m for m in list(sys.modules)
                       if m.split(".")[0] in ("components", "swarm", "store")]:
            del sys.modules[module]
        os.environ.setdefault("INFRA_DIR", "/tmp/deploy-race-test")
        from components import base
        self.base = base
        self.real_run, self.real_sleep = base.run, base.time.sleep
        base.time.sleep = lambda _s: None
        self.calls = []

    def tearDown(self):
        self.base.run, self.base.time.sleep = self.real_run, self.real_sleep

    def deploy(self, results):
        """Run Component.deploy with `run` answering from `results`."""
        def fake_run(argv, timeout=None, stdin=None):
            self.calls.append(argv)
            return results[min(len(self.calls) - 1, len(results) - 1)]
        self.base.run = fake_run
        class _Stub(self.base.Component):
            # `stack` is a read-only property on the real class, so this
            # overrides it rather than assigning through it.
            name = "cache"
            stack = "cache"

            def write_stack(self):
                return "/tmp/stack.yml"

        return self.base.Component.deploy(object.__new__(_Stub))

    def test_contention_is_retried_and_the_retry_is_the_answer(self):
        ok, out = self.deploy([(False, CONTENDED), (True, "converged")])
        self.assertTrue(ok)
        self.assertEqual(out, "converged")
        self.assertEqual(len(self.calls), 2)

    def test_a_real_failure_is_reported_immediately(self):
        # Retrying a spec the cluster rejected just says the same thing three
        # times, more slowly.
        ok, out = self.deploy([(False, "invalid mount config")])
        self.assertFalse(ok)
        self.assertIn("invalid mount", out)
        self.assertEqual(len(self.calls), 1)

    def test_a_clean_deploy_is_not_retried(self):
        ok, _ = self.deploy([(True, "converged")])
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)

    def test_retries_are_bounded(self):
        # Something that will not stop writing is worth reporting, not looping
        # on: the panel has one worker and this runs inside a request.
        ok, out = self.deploy([(False, CONTENDED)])
        self.assertFalse(ok)
        self.assertIn("out of sequence", out)
        self.assertEqual(len(self.calls), self.base.DEPLOY_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
