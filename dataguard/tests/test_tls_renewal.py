"""
Keeping every member certificate correct, and what "correct" means.

    python3 -m unittest discover -s dataguard/tests -v

TWO REASONS TO REISSUE, and only the first one used to be here.

Expiry is the obvious one. The other is the SAN, and it is the one that failed
silently. A member is dialled by its service name, by its per-task name, by the
alias that makes the connection string permanent, and — by a client outside the
cluster — on loopback, because the tunnel helper listens there and TLS is
forwarded rather than terminated. A certificate that does not carry the name a
client dialled fails the handshake, while the set replicates perfectly and every
internal check goes on passing. Nothing anywhere says why the database stopped
answering its own connection string.

The bug had both halves. The renewal here rebuilt the name list from the service
name alone, so at thirty days out the alias was DROPPED; and nothing reissued
when a name was ADDED, so a certificate that had fallen behind stayed behind
until it expired. `pki.member_names` is now the one definition, and this loop
reconciles against it rather than against the calendar.
"""

import datetime
import os
import sys
import tempfile
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
import pki  # noqa: E402


class TLSCase(unittest.TestCase):
    """A component with one member certificate on disk, and no docker."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        D.INFRA_DIR = self.root
        self.component = types.SimpleNamespace(name="docs", kind="mongo", pool=1)
        self.dir = D.tls_dir(self.component)
        os.makedirs(self.dir, exist_ok=True)
        self.key, self.crt = pki.ensure_ca(self.dir, "testcluster", "docs")
        self.written = {}
        D._write_local = lambda path, payload: self.written.__setitem__(path, payload)
        D._ensure_secret = lambda base, payload: f"{base}-v2"
        D._swap_secret = lambda *a, **kw: True

    def issue(self, names, age_days=0):
        """Write member-1.pem with `names`, as if issued `age_days` ago."""
        now = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=age_days))
        pem = pki.issue_member(self.key, self.crt, "testcluster", "docs",
                               names, now=now)
        with open(os.path.join(self.dir, "member-1.pem"), "wb") as fh:
            fh.write(pem)
        return pem

    def reissued(self):
        """The certificate `ensure_tls` wrote, or None if it wrote nothing."""
        self.assertTrue(D.ensure_tls(self.component))
        if not self.written:
            return None
        return next(iter(self.written.values()))


class ExpiryTest(TLSCase):
    def test_a_certificate_with_time_left_is_left_alone(self):
        self.issue(pki.member_names("docs_mongo-1", "docs-mongo"))
        self.assertIsNone(self.reissued())

    def test_renewal_keeps_every_name_the_panel_put_in(self):
        """
        The half that used to SHORTEN the certificate. Nothing about a renewal
        may change what the member is called.
        """
        names = pki.member_names("docs_mongo-1", "docs-mongo")
        self.issue(names, age_days=pki.LEAF_DAYS - 5)
        renewed = self.reissued()
        self.assertIsNotNone(renewed, "a certificate five days out was not renewed")
        self.assertTrue(pki.covers(renewed, names))
        self.assertFalse(pki.needs_renewal(renewed))
        # Named individually, so a failure says which one went.
        self.assertTrue(pki.covers(renewed, ["docs-mongo"]),
                        "the seed alias left the certificate")
        self.assertTrue(pki.covers(renewed, ["127.0.0.1"]),
                        "the loopback name an external client dials left the "
                        "certificate")


class SanReconcileTest(TLSCase):
    """
    The half that had no mechanism at all: a name being MISSING.

    Nothing here is close to expiring. The certificate is simply short of a name
    the member is dialled by, and the loop is what notices — rather than the
    name staying missing until the thing expires a year later.
    """

    def test_a_certificate_short_of_a_name_is_reissued_at_once(self):
        # What the old renewal used to leave behind: the service name and the
        # loopback pair, with the alias missing.
        self.issue(["docs_mongo-1", "tasks.docs_mongo-1", "localhost", "127.0.0.1"])
        renewed = self.reissued()
        self.assertIsNotNone(renewed, "a short certificate was never repaired")
        self.assertTrue(pki.covers(renewed, ["docs-mongo"]))
        # ...and it did not have to wait a year to do it.
        self.assertGreater(pki.days_remaining(renewed), pki.LEAF_DAYS - 1)

    def test_a_complete_certificate_is_left_alone(self):
        self.issue(pki.member_names("docs_mongo-1", "docs-mongo"))
        self.assertIsNone(self.reissued())


class QuietWhenThereIsNothingToDoTest(TLSCase):
    def test_a_component_with_no_tls_directory_is_passed_over_in_silence(self):
        """
        Redis holds no certificates. It used to be told, once per process, that
        its certificate authority was missing and that it should be redeployed —
        advice about a component that was working perfectly.
        """
        import shutil
        shutil.rmtree(self.dir)
        said = []
        original, D.say_once = D.say_once, lambda key, msg, *a: said.append(key)
        try:
            self.assertTrue(D.ensure_tls(self.component))
        finally:
            D.say_once = original
        self.assertEqual(said, [])
        self.assertEqual(self.written, {})

    def test_a_missing_authority_is_still_reported(self):
        """The narrower case, and it is a real one: the panel never finished."""
        os.remove(os.path.join(self.dir, "ca.key"))
        said = []
        original, D.say_once = D.say_once, lambda key, msg, *a: said.append(key)
        try:
            self.assertFalse(D.ensure_tls(self.component))
        finally:
            D.say_once = original
        self.assertEqual(said, [("docs", "notls")])


if __name__ == "__main__":
    unittest.main()
