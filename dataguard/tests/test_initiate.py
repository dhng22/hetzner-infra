"""
Tests for creating a replica set that has never been configured.

    python3 -m unittest discover -s dataguard/tests -v

A brand new mongo component came up completely inert and nothing said why. Every
member runs `mongod --replSet`, and a `--replSet` server that has never been
given a configuration refuses EVERY command, including the ones a driver uses to
find a primary. So the connection string resolved, TLS completed, and then every
single operation failed with `NotYetInitialized` forever — while dataguard read
the same refusal as "this component is unreachable", swallowed it, and moved on
without a line in the log.

Nothing in the system ever called `replSetInitiate`. These tests pin who does,
when, and — the part that matters most — when it must refuse to.
"""

import unittest

import engines


class FakeAdmin:
    def __init__(self, owner):
        self.owner = owner

    def command(self, name, *args, **kwargs):
        self.owner.calls.append((name, args))
        if name == "replSetGetStatus":
            if self.owner.status_error is not None:
                raise self.owner.status_error
            return {"set": "docs", "members": []}
        if name == "replSetInitiate":
            if self.owner.initiate_error is not None:
                raise self.owner.initiate_error
            self.owner.config = args[0]
            return {"ok": 1}
        raise AssertionError(f"unexpected command {name}")


class FakeClient:
    """One member, connected to directly, with no topology discovery."""

    def __init__(self, owner):
        self.admin = FakeAdmin(owner)


class FakeMongo:
    def __init__(self, status_error=None, initiate_error=None):
        self.status_error = status_error
        self.initiate_error = initiate_error
        self.calls = []
        self.config = None
        self.direct_hosts = []

    def direct(self, host):
        self.direct_hosts.append(host)
        return FakeClient(self)

    def engine(self, set_name="docs"):
        def pooled():
            raise AssertionError(
                "initiate must not use the pooled client: a set with no "
                "configuration has no primary to select")
        return engines.MongoEngine(pooled, set_name, direct_factory=self.direct)


def failure(code, message="nope"):
    exc = Exception(message)
    exc.code = code
    return exc


class InitiateTest(unittest.TestCase):

    def test_a_set_that_was_never_configured_is_created_with_one_member(self):
        fake = FakeMongo(status_error=failure(engines.NOT_YET_INITIALIZED,
                                              "no replset config"))
        self.assertTrue(fake.engine().initiate("docs_mongo-1:27017"))
        self.assertEqual(fake.config, {
            "_id": "docs", "version": 1,
            "members": [{"_id": 0, "host": "docs_mongo-1:27017"}]})

    def test_only_the_member_on_the_master_is_configured(self):
        """
        The connection string names four hosts from day one. The CONFIG must
        name one: a set configured with four members while one is running has
        no majority, so it can never elect a primary and the component is just
        as dead as it was before, with a config that now has to be repaired
        rather than created.
        """
        fake = FakeMongo(status_error=failure(engines.NOT_YET_INITIALIZED))
        fake.engine().initiate("docs_mongo-1:27017")
        self.assertEqual(len(fake.config["members"]), 1)

    def test_a_set_that_already_exists_is_left_alone(self):
        """The one that would destroy a database if it were wrong."""
        fake = FakeMongo()
        self.assertFalse(fake.engine().initiate("docs_mongo-1:27017"))
        self.assertIsNone(fake.config)
        self.assertNotIn("replSetInitiate", [c[0] for c in fake.calls])

    def test_a_member_that_is_merely_unreachable_is_not_reconfigured(self):
        """
        A network blip is not an empty database. Any error that is not the
        server's own `NotYetInitialized` means we do not know what is on the
        other end, and building a fresh set on top of a running one is the
        worst thing this process could do.
        """
        fake = FakeMongo(status_error=failure(None, "connection refused"))
        with self.assertRaises(engines.Unavailable):
            fake.engine().initiate("docs_mongo-1:27017")
        self.assertIsNone(fake.config)

    def test_an_authentication_failure_is_not_an_empty_set(self):
        fake = FakeMongo(status_error=failure(18, "Authentication failed"))
        with self.assertRaises(engines.Unavailable):
            fake.engine().initiate("docs_mongo-1:27017")
        self.assertIsNone(fake.config)

    def test_the_member_is_reached_directly(self):
        fake = FakeMongo(status_error=failure(engines.NOT_YET_INITIALIZED))
        fake.engine().initiate("docs_mongo-1:27017")
        self.assertEqual(fake.direct_hosts, ["docs_mongo-1:27017"])

    def test_a_refusal_from_the_server_is_reported_not_swallowed(self):
        fake = FakeMongo(status_error=failure(engines.NOT_YET_INITIALIZED),
                         initiate_error=failure(93, "bad config"))
        with self.assertRaises(engines.Refused):
            fake.engine().initiate("docs_mongo-1:27017")

    def test_sentinel_has_nothing_to_create(self):
        """
        A lone Redis is already a primary — there is no configuration to write,
        so the base class answers False and the loop moves on rather than
        treating every unreachable Redis as a set waiting to be built.
        """
        self.assertFalse(engines.Engine().initiate("cache_redis-1:6379"))


if __name__ == "__main__":
    unittest.main()
