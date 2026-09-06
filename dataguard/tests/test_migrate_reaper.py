"""
A finished migration must not keep holding its connection strings.

    python3 -m unittest discover -s dataguard/tests -v

The migrate job mounts TWO of them: this cluster's root URI, and a full
credential for somebody else's Atlas cluster. Nothing removed either when it
finished. Found live — four secrets from a migration that completed a day
earlier, still in Swarm, two versions of each because every run minted a new one
and kept the old.

Swarm refuses to remove a secret that any service SPEC references, even one with
no running task, which is why the job has to go for the credential to go, and
why the panel says the result is kept for an hour and no longer.
"""

import os
import sys
import time
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


def stamp(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z",
                         time.gmtime(time.time() - seconds_ago))


class FakeSecret:
    def __init__(self, name, bin):
        self.name, self.bin = name, bin

    def remove(self):
        self.bin.append(self.name)


class FakeService:
    def __init__(self, component, tasks, removed):
        self.name = f"{component}_migrate"
        self.attrs = {"Spec": {
            "Labels": {"infra.component": component, D.L_ROLE: "migrate"},
            "TaskTemplate": {"ContainerSpec": {"Secrets": [
                {"SecretName": f"{component}-migrate-here-v2"},
                {"SecretName": f"{component}-migrate-there-v2"},
            ]}}}}
        self._tasks, self._removed = tasks, removed

    def tasks(self):
        return self._tasks

    def remove(self):
        self._removed.append(self.name)


class ReaperTest(unittest.TestCase):
    def setUp(self):
        self.gone, self.removed = [], []
        self.saved = D.dkr

    def tearDown(self):
        D.dkr = self.saved

    def world(self, tasks, versions=("v1", "v2")):
        secrets = [FakeSecret(f"database-migrate-{side}-{v}", self.gone)
                   for side in ("here", "there") for v in versions]
        service = FakeService("database", tasks, self.removed)
        # A removed secret stops being listed, as Swarm's does. Without that the
        # reaper's second pass sees the ones its first pass already took and
        # this test cannot tell a double removal from a correct one.
        D.dkr = types.SimpleNamespace(
            services=types.SimpleNamespace(list=lambda filters=None: [service]),
            secrets=types.SimpleNamespace(
                list=lambda: [x for x in secrets if x.name not in self.gone]))
        return service

    def test_a_running_migration_keeps_everything_it_is_using(self):
        # It may hold those secrets for an hour. Taking them away mid-dump is
        # the one outcome worse than leaving them lying about.
        self.world([{"Status": {"State": "running", "Timestamp": stamp(10)}}])
        D._reap_finished_migrations()
        self.assertEqual(self.removed, [])
        self.assertNotIn("database-migrate-here-v2", self.gone)

    def test_a_superseded_version_goes_immediately_even_while_it_runs(self):
        # v1 became unreachable the moment the job was recreated against v2.
        # Keeping it buys nothing and it is a live credential.
        self.world([{"Status": {"State": "running", "Timestamp": stamp(10)}}])
        D._reap_finished_migrations()
        self.assertEqual(sorted(self.gone),
                         ["database-migrate-here-v1", "database-migrate-there-v1"])

    def test_a_job_still_inside_its_keep_window_is_left_alone(self):
        self.world([{"Status": {"State": "complete", "Timestamp": stamp(60)}}])
        D._reap_finished_migrations()
        self.assertEqual(self.removed, [])
        self.assertNotIn("database-migrate-there-v2", self.gone)

    def test_past_the_window_the_job_and_its_credentials_both_go(self):
        # Both, and in that order: Swarm refuses to remove a secret a service
        # spec still names, so the job has to go first or nothing goes at all.
        self.world([{"Status": {"State": "complete",
                                "Timestamp": stamp(D.MIGRATE_KEEP_SECONDS + 60)}}])
        D._reap_finished_migrations()
        self.assertEqual(self.removed, ["database_migrate"])
        self.assertEqual(len(self.gone), 4)
        self.assertIn("database-migrate-there-v2", self.gone)

    def test_a_failed_migration_is_reaped_like_a_successful_one(self):
        # A credential is no less live for the job having failed, and a failed
        # job is exactly the one somebody leaves sitting there.
        self.world([{"Status": {"State": "failed",
                                "Timestamp": stamp(D.MIGRATE_KEEP_SECONDS + 60)}}])
        D._reap_finished_migrations()
        self.assertEqual(self.removed, ["database_migrate"])

    def test_a_job_with_no_task_yet_is_not_treated_as_finished(self):
        self.world([])
        D._reap_finished_migrations()
        self.assertEqual(self.removed, [])

    def test_the_newest_task_decides_how_long_ago_it_finished(self):
        # A retry leaves an older attempt behind; reading the oldest would reap
        # a job that finished a minute ago.
        self.world([{"Status": {"State": "failed", "Timestamp": stamp(99999)}},
                    {"Status": {"State": "complete", "Timestamp": stamp(30)}}])
        D._reap_finished_migrations()
        self.assertEqual(self.removed, [])

    def test_docker_timestamps_parse(self):
        self.assertAlmostEqual(
            D._parse_stamp("2026-09-06T11:52:57.123456789Z"),
            D._parse_stamp("2026-09-06T11:52:57.123456Z"), places=3)
        self.assertIsNone(D._parse_stamp(""))
        self.assertIsNone(D._parse_stamp("not a time"))


if __name__ == "__main__":
    unittest.main()
