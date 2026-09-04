"""
A certificate authority per database, because the oplog is every write you have
ever made.

SHARED, like `signals`, and for the same reason. The panel issues this material
when a component is created — the stack references the Swarm secrets, so they
have to exist before the first deploy — and dataguard renews it on a loop. Two
copies of "what a member certificate looks like" is two things that can drift,
and the symptom of drift here is a replica set that will not form.

WHY THIS EXISTS AT ALL
----------------------
While a database is one container on the master, its traffic never leaves the
box. The moment a second member appears on another machine, replication — the
whole dataset, continuously — crosses the network between them. Docker's
`--opt encrypted` overlay wraps that in IPsec, and it is worth having, but it is
not the mechanism: it stops at the container boundary, mongod cannot see it, it
proves nothing about WHO the peer is, and a misconfigured network silently
downgrades to plaintext with nothing to notice.

TLS is the mechanism. `requireTLS` covers client connections and replica-set
internal communication in one setting — there is no separate knob for the oplog,
which is precisely why this is the right layer to fix it at. x509 cluster auth
then makes each member prove WHICH member it is, rather than proving it holds a
shared file.

WHAT IS WHERE
-------------
    /opt/infra/components/<name>/tls/ca.key       0600  never leaves the master
    /opt/infra/components/<name>/tls/ca.crt       0644  handed to clients
    /opt/infra/components/<name>/tls/member-N.pem 0600  the local copy of a leaf
    docker secret <name>-tls-ca-v<k>              0400  mounted by every member
    docker secret <name>-tls-N-v<k>               0400  mounted by one member

ONE DIRECTORY, TWO WRITERS, AND THAT IS DELIBERATE. The panel issues this
material when a component is created, because the stack references the Swarm
secrets and they must exist before the first deploy; dataguard renews it,
because it is the thing with a loop. Both write to the component's own
directory — the panel's `/opt/infra` is the same bind mount as dataguard's — so
there is one answer to "what certificate does member 2 have" rather than two
that can disagree. The local copy exists at all because Swarm never hands a
secret's DATA back, so expiry cannot be checked on the deployed one.

The CA key stays on the master and is never distributed, which is why dataguard
is manager-pinned. The certificates are Swarm secrets rather than files in
`secret.env`, because they have to land on machines the panel cannot write to,
at 0400, owned by the mongod user — and Swarm is the only thing in this cluster
that can do that. Swarm secrets are immutable, so renewal is a versioned name
and a redeploy, which is fine for something that happens once a year.

RENEWAL IS NOT OPTIONAL. An expired member certificate does not degrade the
replica set, it stops it: members refuse each other, there is no primary, and
every write fails. `days_remaining` is exported per member and alerted on, and
renewal starts a month early.
"""

import datetime
import ipaddress
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

#: Ten years. The CA is per component and never leaves the master; rotating it
#: means re-issuing every member certificate at once, which is a maintenance
#: window nobody should be asked for on a schedule.
CA_DAYS = 3650
#: One year for a leaf, renewed at 30 days remaining. Short enough that a leaked
#: member certificate is not forever, long enough that renewal is rare.
LEAF_DAYS = 365
RENEW_BEFORE_DAYS = 30

KEY_BITS = 2048


def _write(path, data, mode):
    """
    Write via a temp file in the same directory, then rename.

    chmod happens BEFORE the rename, so the final path never exists with default
    permissions — not even for the microsecond between create and chmod. One of
    these files is a certificate authority's private key.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _name(cluster, component, common_name):
    """
    The subject every certificate in one component shares, bar the CN.

    O and OU are load-bearing rather than decorative: mongod's x509 cluster
    authentication accepts a peer only when its O, OU and DC match the local
    member's. Two components therefore cannot authenticate to each other even
    though the same process issued both, which is the property worth having.
    """
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, cluster),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, component),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])


def ensure_ca(directory, cluster, component, now=None):
    """
    (key_pem, cert_pem) for this component's CA, creating it on first use.

    `directory` is where the material lives — the component's own `tls/` — not a
    root to build a path under. Two callers with two ideas of the layout is how
    the panel came to write an authority the renewer could not find.

    Idempotent, so it is safe to call every loop: an existing CA is read back
    rather than replaced. Replacing one would invalidate every member
    certificate at once and take the set down.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    key_path = os.path.join(directory, "ca.key")
    crt_path = os.path.join(directory, "ca.crt")
    if os.path.exists(key_path) and os.path.exists(crt_path):
        with open(key_path, "rb") as fh:
            key_pem = fh.read()
        with open(crt_path, "rb") as fh:
            crt_pem = fh.read()
        return key_pem, crt_pem

    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    subject = _name(cluster, component, f"{component} CA")
    cert = (x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    crt_pem = cert.public_bytes(serialization.Encoding.PEM)
    _write(key_path, key_pem, 0o600)
    _write(crt_path, crt_pem, 0o644)
    return key_pem, crt_pem


def issue_member(ca_key_pem, ca_crt_pem, cluster, component, hostnames, now=None):
    """
    A member's combined key+certificate PEM, in the order mongod expects.

    `--tlsCertificateKeyFile` takes ONE file containing both, key first. Handing
    it a certificate-only file produces "cannot read PEM key file", which reads
    like the file is corrupt rather than like it is missing half of itself.

    Every name the member may be reached by goes in the SAN, because a mongod
    validating a peer checks the certificate against the host it dialled — and
    the replica set config names members by their Swarm service DNS name while a
    local health check dials localhost.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    ca_crt = x509.load_pem_x509_certificate(ca_crt_pem)

    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    alt = []
    for host in hostnames:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            alt.append(x509.DNSName(host))

    cert = (x509.CertificateBuilder()
            .subject_name(_name(cluster, component, hostnames[0]))
            .issuer_name(ca_crt.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=LEAF_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            # BOTH usages on one certificate, on purpose. A replica-set member is
            # a server to its clients and a CLIENT to its peers, and x509 cluster
            # auth is the member presenting this same certificate as a client.
            # Issuing server-only certificates is the classic way to get a set
            # that accepts connections and cannot form.
            .add_extension(x509.ExtendedKeyUsage([
                x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))

    return (key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
        + cert.public_bytes(serialization.Encoding.PEM))


def days_remaining(pem, now=None):
    """
    How long this certificate has left, or None if it cannot be read.

    None is deliberately not zero: "unreadable" and "expired" call for different
    actions, and an unreadable file must not trigger a renewal that overwrites
    something that was fine.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        cert = x509.load_pem_x509_certificate(
            pem if isinstance(pem, bytes) else pem.encode())
    except Exception:                                            # noqa: BLE001
        return None
    expiry = cert.not_valid_after_utc
    return (expiry - now).total_seconds() / 86400.0


def needs_renewal(pem, now=None):
    left = days_remaining(pem, now)
    return left is not None and left <= RENEW_BEFORE_DAYS


def covers(pem, hostnames):
    """
    Whether this certificate's SAN already names every one of `hostnames`.

    Expiry is not the only reason to reissue. A member reached by a name its
    certificate does not carry fails the handshake with a hostname mismatch, and
    the set keeps working internally while its own connection string stops
    connecting — so "the SAN list changed" has to be a renewal trigger too.

    Unreadable input answers False. The caller reissues, which is the safe way
    to be wrong about a certificate.
    """
    try:
        cert = x509.load_pem_x509_certificate(
            pem if isinstance(pem, bytes) else pem.encode())
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
    except Exception:  # noqa: BLE001 — any unreadable certificate is reissued
        return False
    present = {str(v) for v in san.get_values_for_type(x509.DNSName)}
    present |= {str(v) for v in san.get_values_for_type(x509.IPAddress)}
    return all(str(name) in present for name in hostnames)
