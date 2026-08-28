"""
Tests for the per-component certificate authority.

    python3 -m unittest discover -s dataguard/tests -v

The properties asserted here are the ones that fail SILENTLY if they regress: a
server-only certificate produces a replica set that accepts client connections
and cannot form, and a CA that is regenerated on the second call invalidates
every member at once. Neither looks like a certificate problem from the outside.
"""

import datetime
import os
import stat
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import pki  # noqa: E402

from cryptography import x509  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402


class CATest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_second_call_returns_the_same_authority(self):
        """
        Idempotent, because this runs every loop. Regenerating would invalidate
        every member certificate at once and take the set down — and it would do
        it a minute after everything looked fine.
        """
        first = pki.ensure_ca(self.dir, "cluster", "docs")
        second = pki.ensure_ca(self.dir, "cluster", "docs")
        self.assertEqual(first, second)

    def test_the_private_key_is_not_readable_by_anyone_else(self):
        pki.ensure_ca(self.dir, "cluster", "docs")
        path = os.path.join(self.dir, "ca.key")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_the_authority_lands_where_it_was_asked_to(self):
        """
        `directory` is where the material lives, not a root to build a path
        under. Two callers with two ideas of the layout is how the panel came to
        write an authority the renewer could not find.
        """
        pki.ensure_ca(self.dir, "cluster", "docs")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "ca.crt")))

    def test_two_components_get_two_different_authorities(self):
        """
        x509 cluster auth accepts a peer whose O and OU match. Sharing one CA
        across components would let a member of one replica set authenticate to
        another, which is exactly the property this is here to deny.
        """
        a = pki.ensure_ca(os.path.join(self.dir, "docs"), "cluster", "docs")
        b = pki.ensure_ca(os.path.join(self.dir, "sessions"), "cluster", "sessions")
        self.assertNotEqual(a[1], b[1])

    def test_the_subject_carries_the_component_so_sets_cannot_cross_authenticate(self):
        _key, crt = pki.ensure_ca(self.dir, "cluster", "docs")
        cert = x509.load_pem_x509_certificate(crt)
        ou = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
        self.assertEqual(ou[0].value, "docs")


class MemberTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.key, self.crt = pki.ensure_ca(self.dir, "cluster", "docs")
        self.pem = pki.issue_member(
            self.key, self.crt, "cluster", "docs",
            ["docs_mongo-2", "tasks.docs_mongo-2", "localhost", "127.0.0.1"])

    def cert(self):
        return x509.load_pem_x509_certificate(self.pem)

    def test_the_file_contains_the_key_first_then_the_certificate(self):
        """
        `--tlsCertificateKeyFile` takes ONE file with both, key first. A
        certificate-only file fails with "cannot read PEM key file", which reads
        like corruption rather than like a missing half.
        """
        self.assertLess(self.pem.index(b"-----BEGIN PRIVATE KEY-----"),
                        self.pem.index(b"-----BEGIN CERTIFICATE-----"))

    def test_it_is_both_a_server_and_a_client_certificate(self):
        """
        A replica-set member is a server to its clients and a CLIENT to its
        peers — x509 cluster auth IS the member presenting this certificate as a
        client. Server-only certificates give a set that accepts connections and
        never forms.
        """
        usage = self.cert().extensions.get_extension_for_class(
            x509.ExtendedKeyUsage).value
        self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, usage)
        self.assertIn(ExtendedKeyUsageOID.CLIENT_AUTH, usage)

    def test_every_name_the_member_is_dialled_by_is_in_the_san(self):
        names = self.cert().extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        self.assertIn("docs_mongo-2", names.get_values_for_type(x509.DNSName))
        self.assertIn("localhost", names.get_values_for_type(x509.DNSName))

    def test_it_is_signed_by_this_component_s_authority(self):
        ca = x509.load_pem_x509_certificate(self.crt)
        self.assertEqual(self.cert().issuer, ca.subject)

    def test_renewal_starts_before_expiry_not_after(self):
        """
        An expired member certificate does not degrade the set, it stops it. The
        window has to open early enough that a restart of one member per loop
        gets through the whole set with time to spare.
        """
        self.assertFalse(pki.needs_renewal(self.pem))
        soon = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=pki.LEAF_DAYS - 5))
        self.assertTrue(pki.needs_renewal(self.pem, now=soon))

    def test_an_unreadable_certificate_is_unknown_rather_than_expired(self):
        """
        None, not zero. "Unreadable" and "expired" call for different actions,
        and treating the first as the second would overwrite something that was
        fine.
        """
        self.assertIsNone(pki.days_remaining(b"not a certificate"))
        self.assertFalse(pki.needs_renewal(b"not a certificate"))


if __name__ == "__main__":
    unittest.main()
