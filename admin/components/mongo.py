"""
A MongoDB replica set that can start on one box and end up on four, without the
connection string ever changing.

THE ADDRESSING TRICK, WHICH EVERYTHING ELSE HANGS OFF
-----------------------------------------------------
A component is created with a pool of n+1 members and the connection string
names ALL of them from the first day — while only member 1 exists, on the
master. A driver tolerates seeds that do not resolve: it discovers the real
topology from whichever seed answers, so `docs_mongo-4` can be a name that means
nothing for six months and then suddenly mean a machine in Helsinki, with no
redeploy of anything that talks to it.

Putting all n+1 in `rs.config()` up front would be the exact opposite: a set with
no majority, therefore no primary, therefore no writes. So the SEED LIST is the
final shape and the REPLICA SET CONFIG grows underneath it. Dataguard owns that
growth; this file only has to make every member addressable before it exists.

Every member is its own Swarm service with its own volume, because a volume
lives on one machine and one service with three replicas would be three mongods
fighting over one data directory. Members 2..n+1 are rendered at `replicas: 0`
and dataguard moves them between 0 and 1 — the live count and the live node
constraint are read back here, the same way an application's image and replica
count are, because something other than this file owns them at runtime.

TLS IS NOT OPTIONAL AND NOT AN OVERLAY
--------------------------------------
The moment a second member exists, replication crosses the network — and
replication is every write you have ever made. Docker's encrypted overlay is
worth having and is not the mechanism: it stops at the container boundary,
mongod cannot see it, and it proves nothing about who the peer is. `requireTLS`
covers client connections AND replica-set internal traffic in one setting, and
`clusterAuthMode=x509` makes each member prove which member it is.

x509 from the first day rather than a keyFile, deliberately. Moving an existing
set from keyFile to x509 is a four-stage rolling change
(keyFile -> sendKeyFile -> sendX509 -> x509); starting there costs nothing and
skips the whole dance.

WHY THE FIRST START IS A WRAPPER
--------------------------------
The official image creates the root user by starting a temporary mongod and
connecting to it with mongosh — which cannot work under `requireTLS`, and fails
in a way that reads like a broken image rather than a policy conflict. So the
initial user is created here, against a temporary server bound to 127.0.0.1 with
TLS off, inside the container, before the real one starts. Nothing off the
container can reach that; the alternative is running the real server with a
weaker TLS mode for its first minutes, which is a hole nobody would remember to
close.
"""

import os
import struct
from urllib.parse import quote

from . import base, store
from .base import Component, Field, Secret

EXPORTER_IMAGE = "percona/mongodb_exporter:0.43.1"
PBM_IMAGE = "percona/percona-backup-mongodb:2.8.0"
VIEWER_IMAGE = "mongo-express:1.0.2-20"
#: The migration job's image. NOT `PBM_IMAGE`: Percona's backup image carries no
#: `mongodump` at all (checked — `command -v mongodump` finds nothing), and the
#: official `mongo` images stopped shipping the database tools at 4.4. This one
#: has `mongodump`, `mongorestore` and `mongosh`, which is every binary the job
#: needs and nothing that has to be installed at run time.
MIGRATE_IMAGE = "percona/percona-server-mongodb:7.0"

#: The uid the official mongo image runs as. Swarm secrets are mounted with an
#: explicit owner because mongod refuses a certificate or key file it does not
#: own, and the error names the file rather than the ownership.
MONGO_UID = "999"

#: The uid `MIGRATE_IMAGE` runs as — Percona's entrypoint drops to `mongodb`,
#: which is 1001 there and NOT the 999 above. Same reason as `MONGO_UID` and one
#: the migration job originally missed: a Swarm secret belongs to root unless it
#: is told otherwise, so mounting a connection string 0400 without this makes it
#: unreadable by the only process that needs it. What that looks like is not a
#: permission error — the job reports "cannot read the source" and mongosh, given
#: an empty URI, reports `ECONNREFUSED 127.0.0.1:27017`, which reads like a wrong
#: Atlas password or a firewall. The mode stays 0400: it is what stops anything
#: else in the container from reading somebody else's cluster credential.
MIGRATE_UID = "1001"


def _op_query(collection, document):
    """
    One OP_QUERY message, as hex, for the gateway's health check.

    OP_QUERY is removed from MongoDB for everything EXCEPT this: a driver must
    open a connection with `isMaster`/`hello` in this shape before the wire
    protocol has been negotiated and anything newer can be spoken. That makes it
    the one message a health check can rely on across every version this
    component offers, and the one that needs no authentication.

    Assembled rather than pasted as a hex literal, because a hex literal is a
    thing nobody can check and nobody dares change.
    """
    body = (struct.pack("<i", 0)                     # flags
            + collection.encode() + b"\x00"          # fullCollectionName
            + struct.pack("<i", 0)                   # numberToSkip
            + struct.pack("<i", 1)                   # numberToReturn
            + document)
    header = struct.pack("<iiii", 16 + len(body), 1, 0, 2004)   # 2004 = OP_QUERY
    return (header + body).hex()


def _bson_int32(name, value):
    return b"\x10" + name.encode() + b"\x00" + struct.pack("<i", value)


def _bson_document(*elements):
    body = b"".join(elements) + b"\x00"
    return struct.pack("<i", 4 + len(body)) + body


#: `{isMaster: 1}` against `admin.$cmd`, and the answer that means "primary".
#:
#: The legacy command is asked for by name on purpose. `hello` replies with
#: `isWritablePrimary`, `isMaster` replies with `ismaster`, and only the second
#: is answered by every version on the form — so this is the field to match.
_ISMASTER_QUERY = _op_query("admin.$cmd", _bson_document(_bson_int32("isMaster", 1)))
_ISMASTER_TRUE = (b"\x08" + b"ismaster" + b"\x00" + b"\x01").hex()


def _read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


class MongoComponent(Component):
    TYPE = "mongo"
    LABEL = "MongoDB"
    BLURB = "A MongoDB replica set that grows without changing its address."
    CATEGORY = "Data"
    GROUP = "Database"
    KEEPS_VOLUME = True
    MANAGER_FIELD = "dataguard"
    GROUPS = (
        ("observability", "Observability", None,
         "Two containers you can have alongside the database. The exporter is "
         "what Grafana and every database alert read it through. The visualiser "
         "is a console with full access to your data and no password of its own, "
         "so it is never published — the View button proxies it through the "
         "panel session you are already signed in to, and it is stopped again "
         "once nobody has looked at it."),
        ("dataguard", "Dataguard", "dataguard",
         "With this off the topology is yours: one server, one volume, no "
         "failover and no backups. Dataguard still measures this component "
         "either way — it simply stops changing its shape."),
    )

    @classmethod
    def fields(cls):
        return [
            Field("placement_mode", "Placement", "choice", "auto",
                  choices=("auto", "master", "any"),
                  help="`auto` lets dataguard decide which machines the members "
                       "run on, which is the only way it can grow this off the "
                       "master. `master` keeps every member on the node that will "
                       "not be deleted, and turns dataguard off. `any` lets Swarm "
                       "place them and is only useful if you are moving the data "
                       "yourself."),
            Field("placement_extra", "Extra constraints", "text", "",
                  placeholder="node.labels.disk == ssd",
                  help="Comma separated, added to whatever the mode implies."),
            Field("version", "Version", "choice", "7.0",
                  choices=("7.0", "6.0", "5.0"),
                  help="Changing this restarts the members one at a time. The "
                       "volumes survive, but Mongo only upgrades one major version "
                       "at a time — go 5 to 6 to 7, never 5 to 7."),
            Field("username", "Root username", "text", "root", required=True,
                  help="Created on the FIRST start only, from an empty volume. "
                       "Changing it later does nothing until the data directory is "
                       "empty again."),
            Field("cache_mb", "WiredTiger cache (MB)", "memory", 256, required=True,
                  minimum=64, maximum=131072,
                  help="Mongo's own data cache. Left unset it is sized from the "
                       "HOST's RAM and ignores this container's limit entirely, "
                       "which is how a small Mongo gets OOM-killed on a large "
                       "machine. Applies to every member."),
            Field("external_hostname", "Tunnel hostname", "text", "",
                  placeholder="docs.example.com",
                  help="A DNS name on a domain in your Cloudflare account, which you invent — `docs.example.com`. Setting it here does not create it: the Credentials tab then gives you four steps, and until you have done them nothing outside the cluster can reach this database. Leave it empty to keep it in-cluster only."),
            Field("cpu_reservation", "CPU reserved", "cpu", 0.3, required=True,
                  minimum=0.01, maximum=32),
            # 512, not 768. Must stay above the WiredTiger cache — the rule is
            # enforced in validate() — and the cache defaults to 256, so this is
            # twice it rather than three times. Mongo genuinely needs room above
            # its cache for connections, sorts and index builds; measured here
            # under production traffic it peaked at 285MB against a 256MB cache,
            # so 512 keeps a real margin while returning 256MB to the cluster
            # that the old default was holding for nothing.
            Field("memory_reservation_mb", "Memory reserved (MB)", "memory", 512,
                  required=True, minimum=64, maximum=131072,
                  help="Must exceed the WiredTiger cache with room for connections, "
                       "indexes being built and the server itself."),

            Field("exporter", "Metrics exporter", "bool", True,
                  group="observability", switch=True,
                  help="Runs mongodb_exporter so Grafana and the replication "
                       "alerts have connection counts, operation counters and "
                       "replication lag."),
            Field("visualizer", "Data visualizer", "bool", False,
                  group="observability", switch=True,
                  help="A browser console over this database. It is FULL access "
                       "with no password of its own, so it is never published: the "
                       "View button on this page proxies it through your panel "
                       "session, and dataguard stops it again once nobody has "
                       "looked at it for fifteen minutes."),

            Field("dataguard", "Dataguard", "bool", True, group="dataguard",
                  help="Let dataguard own this set's shape: how many members it "
                       "has, which machines they are on, and which one is primary."),
            Field("secondary_reads", "Reads may go to secondaries", "bool", True,
                  group="dataguard",
                  help="ON, because read scaling is the point of a replica set — "
                       "and it is a CONTRACT WITH YOUR APPLICATION, not a setting. "
                       "A secondary can be behind the write that produced the data "
                       "you are reading. Your code must use a causally consistent "
                       "session and carry its operationTime between requests; the "
                       "Credentials tab has the pattern. Without that, a user can "
                       "save a row and not see it on the next page load, with no "
                       "error anywhere. Turn this OFF and every read goes to the "
                       "primary — correct by construction, and then only a bigger "
                       "machine can help read latency."),
            Field("lag_budget_seconds", "Lag budget (s)", "number", 10,
                  minimum=1, maximum=3600, group="dataguard",
                  help="A secondary further behind than this is taken out of read "
                       "rotation entirely — hidden, so no client can be handed it — "
                       "and put back when it catches up. Much tighter than the "
                       "driver's own maxStalenessSeconds, which cannot go below 90."),
            Field("max_replica_lag_alert", "Alert above lag (s)", "number", 60,
                  minimum=1, maximum=86400, group="dataguard",
                  help="Sustained lag above this raises ReplicaLagOverBudget."),
            Field("backup_target", "Backup to", "text", "", group="dataguard",
                  placeholder="name from the Storage tab",
                  help="A target from the Storage tab. Blank means NO BACKUPS — and "
                       "dataguard will then also refuse to change this set's shape, "
                       "because a topology change can lose data and doing one with "
                       "nothing to restore from is betting the database on it going "
                       "well. The set still runs, fails over and reports; it just "
                       "will not grow."),
            Field("backup_interval_hours", "Backup every (hours)", "number", 24,
                  minimum=1, maximum=720, group="dataguard",
                  help="Full snapshots. Between them PITR keeps a continuous oplog, "
                       "so you can restore to any second, not just to a snapshot."),
            Field("max_snapshots", "Keep at most (snapshots)", "number", 7,
                  minimum=1, maximum=365, group="dataguard",
                  help="Once there are more full snapshots than this, the oldest are "
                       "deleted after each new one completes — oldest first, and only "
                       "ever after the new one has succeeded, so a failed backup "
                       "cannot leave you with fewer than you had. Storage is billed "
                       "by the gigabyte-month and a snapshot every day forever is a "
                       "bill that only goes one way. Note this bounds the SNAPSHOTS, "
                       "not the PITR oplog between them: restoring to an arbitrary "
                       "second only works back as far as the oldest snapshot you "
                       "still have, so this is also how far back you can go."),
        ]

    # --- credentials --------------------------------------------------------

    SECRETS = (
        Secret("MONGO_PASSWORD", "Password",
               help="Leave blank to generate a strong one. Set it yourself if you "
                    "are moving an existing database and its clients already know "
                    "the password."),
        # NOT on the Credentials tab. That tab answers "how do I connect to this
        # component", and this is a credential for a cluster that is not this
        # one — it belongs beside the operation that uses it. Storage is
        # unchanged either way: `secret.env` at 0600, never `component.json`,
        # because it carries a password for somebody else's database.
        Secret("ATLAS_URI", "MongoDB Atlas connection string", generated=False,
               maximum=512, tab="migrate",
               help="Paste it exactly as Atlas gives it to you, including the "
                    "password and the database name. It is stored for the next "
                    "migration and shown as set rather than read back."),
    )

    def password(self):
        return self.secret("MONGO_PASSWORD")

    def ensure_password(self):
        """Generate one if there is none. Idempotent, so a redeploy is safe."""
        if self.password():
            return False
        self.apply_secrets({})
        return True

    def rotate_password(self):
        """
        New password, changed in the RUNNING server, then saved and redeployed.

        `MONGO_INITDB_ROOT_PASSWORD` is read on the first start only, off an
        empty data directory. Redeploying with a new value changes nothing for an
        existing volume, so a naive copy of the Redis button would report success
        while every client kept working on the old password.
        """
        new = self.SECRETS[0].generate()
        ok, out = self._change_password(new)
        if not ok:
            return False, out
        self.apply_secrets({"MONGO_PASSWORD": new})
        ok, out = self.deploy()
        if not ok:
            return False, (f"The password was changed in the server and saved, but "
                           f"the redeploy failed: {out}")
        return True, ("Password rotated in the running set and saved. Anything still "
                      "using the old one can no longer authenticate.")

    def _change_password(self, new):
        container = self._local_container()
        if not container:
            return False, (f"No running member of {self.name} on this node. The panel "
                           f"can only reach containers on the master, and a Mongo "
                           f"password has to be changed in the server itself — the "
                           f"environment variable is read on first start only.")
        # Both passwords travel in the container's environment, never in argv:
        # the master's process table is readable by anything else on the box.
        script = ('db.getSiblingDB("admin").changeUserPassword('
                  'process.env.MONGO_INITDB_ROOT_USERNAME, process.env.NEW_PASSWORD)')
        ok, out = base.run([
            "docker", "exec", "-e", f"NEW_PASSWORD={new}", container,
            "sh", "-c",
            'exec mongosh --quiet --tls --tlsCAFile /run/secrets/tls-ca.crt '
            '-u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
            f'--authenticationDatabase admin --eval {base.quote(script)}'], timeout=120)
        if not ok:
            return False, f"Could not change the password in the server: {out}"
        return True, ""

    def _local_container(self):
        for index in range(1, self.slots + 1):
            out = base.docker_out([
                "ps", "--filter",
                f"label=com.docker.swarm.service.name={self.member_service(index)}",
                "--filter", "status=running", "--format", "{{.ID}}"])
            lines = [line.strip() for line in out.splitlines() if line.strip()]
            if lines:
                return lines[0]
        return ""

    # --- identity -----------------------------------------------------------

    @property
    def slots(self):
        """
        How many member slots exist. A constant — see `base.MEMBER_SLOTS`.

        SLOTS, not members: slot 1 is the copy on the master and is never handed
        out for growth, so the grown set is one smaller than this.

        It was `replica_pool + 1` and frozen at creation for a reason that WAS
        real here, unlike on Redis: the seed list named every slot, so growing
        the ceiling put a host in the string nobody had been given. `seed_hosts`
        names one alias now, so the string no longer knows or cares how many
        there are, and the ceiling stopped being anybody's decision.
        """
        return base.MEMBER_SLOTS

    def member_indexes(self):
        """Every slot this component renders. One when nothing manages it."""
        return range(1, (self.slots if self.managed else 1) + 1)

    def member_key(self, index):
        """
        The service key for a member. UNSUFFIXED when nothing manages this one.

        The same rule, and the same reason, as RedisComponent.member_key: the
        Dataguard panel says "with this off the topology is yours: one server,
        one volume", and the renderer emitted a whole replica set regardless.
        Components deploy with `--prune`, so a component created before the
        switch existed would have had `<name>_mongo` DELETED on its next
        redeploy and four members rendered in its place, with a connection
        string nothing was using.

        §6.1 of the design says this migration "makes this an explicit confirmed
        action showing the old and new strings side by side, never a silent
        consequence of saving a form". This is what makes it possible to keep
        that promise: unmanaged is the old single service, and the switch is the
        migration.
        """
        if not self.managed:
            return self.TYPE
        return f"{self.TYPE}-{index}"

    def member_service(self, index):
        return f"{self.stack}_{self.member_key(index)}"

    def service_key(self):
        """Member 1 is the component's main service — the one on the master."""
        return self.member_key(1)

    def services(self):
        names = [self.member_service(i) for i in self.member_indexes()]
        if self.external_hostname():
            names.append(f"{self.stack}_gateway")
        if self.spec.get("exporter"):
            names.append(f"{self.stack}_mongo-exporter")
        if self.managed:
            names.append(f"{self.stack}_pbm-ctl")
        if self.spec.get("visualizer"):
            names.append(f"{self.stack}_viewer")
        return names

    @property
    def managed(self):
        return bool(self.spec.get("dataguard"))

    # --- addressing ---------------------------------------------------------

    def seed_alias(self):
        """
        One name that resolves to every member that is currently running.

        Every member service joins the edge network under this alias, and Swarm's
        DNS answers it with the VIP of each service that has it — verified on the
        cluster: two members answered with two addresses, a third at `replicas: 0`
        did not appear at all, and scaling it up put it in the answer within
        seconds. So a stopped slot is not a dead address in the seed list; it is
        simply not in the list.

        Underscore-free, because it goes in a certificate SAN and in a URL where
        a hostname is expected. `<name>-mongo` cannot collide with the service
        names, which are `<stack>_mongo-<n>`.
        """
        return f"{self.name}-mongo"

    def seed_hosts(self, port=27017):
        # ONE name, and this is the change that made the connection string
        # permanent. It used to name every slot — `docs_mongo-1` through
        # `docs_mongo-4` — which is why the slot count had to be chosen up front
        # and could never be raised: raising it added a host to a string that
        # applications were already holding. An alias has no count in it.
        #
        # An unmanaged component is one server and has no alias, so it names
        # that server directly.
        if not self.managed:
            return [f"{self.member_service(1)}:{port}"]
        return [f"{self.seed_alias()}:{port}"]

    def connection_url(self, host=None, port=None):
        """
        The string, written once, that never changes again.

        Every option in it is load-bearing:

          replicaSet          makes the driver DISCOVER the topology instead of
                              treating the seeds as a list of separate servers.
                              In-cluster only — see the `host` branch, where it
                              would send an outside client to addresses that
                              exist on the overlay network and nowhere else.
          tls                 true from the first day, while the set is still one
                              member on the master — so it is already correct on
                              the day a second machine appears.
          w=majority          a write is acknowledged only once a majority holds
                              it, which is what makes it survive a failover.
          readConcernLevel    majority: never read something that could still be
                              rolled back.
          retryWrites         the driver retries a write interrupted by an
                              election. Without it, every stepdown is an error
                              your users see.
          readPreference      only present when secondary reads are ON, because
                              it is the option that changes what your application
                              is allowed to assume.
        """
        user = quote(self.spec.get("username") or "root", safe="")
        secret = quote(self.password(), safe="")
        common = ["authSource=admin", "tls=true", "retryWrites=true",
                  "w=majority", "readConcernLevel=majority"]

        if host:
            # ONE SERVER, AND NO `replicaSet`. This is the option that used to be
            # here and could not work: it makes the driver ask the seed for the
            # set's real members and then THROW THE SEED AWAY, and the answer is
            # `<stack>_mongo-2:27017` — Swarm service DNS, which resolves on the
            # overlay network and nowhere else. So the string connected while
            # the set was one member and stopped the day it became two, with a
            # server-selection timeout naming hosts nobody outside had heard of.
            #
            # `directConnection=true` is what stops that discovery: the driver
            # treats this address as a single server and speaks to it. It has to
            # be explicit, because a modern driver given one seed and no
            # replicaSet still discovers by default. Retryable writes and causal
            # sessions survive it — the server it reaches is a replica-set
            # primary, not a standalone.
            #
            # What makes that address stay correct is the gateway: it is the
            # primary's forwarder, not a member, so it does not step down, does
            # not move and is not removed. See `_gateway`.
            #
            # No `tlsCAFile` either. A client outside the cluster will never have
            # `/run/secrets`, and handing it that path produces a string whose
            # only possible outcome is a file-not-found that reads like a broken
            # database. It downloads the authority from the Credentials tab and
            # points at wherever it saved it.
            #
            # No `readPreference` either: this address is the primary by
            # construction, and asking it for a secondary read would be asking
            # for something the proxy has no way to offer.
            # NO `tls=true`, AND NO `tlsCAFile`. The gateway holds the TLS
            # session to the member; what reaches this address has already been
            # encrypted by Cloudflare for the whole of its journey across the
            # internet. Requiring it here would buy one more encrypted hop and
            # cost every consumer a certificate file to obtain and install.
            return (f"mongodb://{user}:{secret}@{host}:{port or 27017}/"
                    f"?directConnection=true&{'&'.join(o for o in common if o != 'tls=true')}")

        # The authority is this component's own, so nothing can verify it from
        # the public root store — which is why every consumer needed a
        # workaround of its own and an application handed this string got
        # nothing but "self-signed certificate in certificate chain". Naming the
        # file IN the string is what makes it work by construction: `tlsCAFile`
        # is a driver option, so pymongo, the Node driver and mongosh all honour
        # it, and the panel mounts the authority at this exact path in every
        # container that needs it.
        options = [f"replicaSet={self.stack}", *common]
        options.insert(3, f"tlsCAFile={base.ca_file_for(self.name)}")
        if self.spec.get("secondary_reads"):
            # maxStalenessSeconds is the DRIVER's own guard and cannot go below
            # 90 by protocol. Dataguard's lag budget is the tight one — it hides
            # a lagging member outright — and this is the belt underneath it, for
            # the seconds before dataguard notices.
            options += ["readPreference=secondaryPreferred", "maxStalenessSeconds=90"]
        return (f"mongodb://{user}:{secret}@{','.join(self.seed_hosts())}/"
                f"?{'&'.join(options)}")

    def external_hostname(self):
        return base.clean_hostname(self.spec.get("external_hostname"))

    def credentials(self):
        hostname = self.external_hostname()
        return {
            "password": self.password(),
            # Derived from the SAME list the URL is built from, so the two can
            # never disagree. It used to name only the first member, while the URL beside it and
            # the "How to reach it" panel both named all of them — one component
            # described three different ways on one page.
            "internal_host": ", ".join(h.rsplit(":", 1)[0]
                                       for h in self.seed_hosts()),
            "internal_port": "27017",
            "internal_url": self.connection_url(),
            "external_hostname": hostname,
            "external_target": f"tcp://{self.stack}_gateway:27017",
            "external_command": base.access_command(hostname, 27017) if hostname else "",
            "external_url": self.connection_url("127.0.0.1", 27017) if hostname else "",
            "external_steps": self._external_steps(hostname) if hostname else [],
            "external_notes": self._external_notes(),
            "legacy_port": self.spec.get("external_port"),
            "notes": self._credential_notes(),
        }

    def _external_steps(self, hostname):
        """
        What to DO, in order, each note saying what happens if you skip it.

        Built here rather than in the template so an engine that needs a
        different step can have one without the template learning its name. The
        panel numbers whatever comes back.
        """
        return [
            {"title": "Route this hostname to your tunnel",
             "code": f"tcp://{self.stack}_gateway:27017",
             "note": f"In the Cloudflare Zero Trust dashboard, add {hostname} to "
                     "this cluster's tunnel with that target. PUT AN ACCESS POLICY "
                     "ON IT: without one, anyone who knows the name reaches this "
                     "database, with only the password in the way."},
            {"title": "Install cloudflared where your app runs, and keep this running",
             "code": base.access_command(hostname, 27017),
             "note": "It holds the tunnel open and listens on 127.0.0.1:27017. "
                     "Nothing can connect while it is stopped."},
            {"title": "Point your application at this",
             "code": self.connection_url("127.0.0.1", 27017),
             "note": "Your application talks to the helper from step 2, which is "
                     "running beside it and listening on this port; the helper "
                     "carries the traffic through the tunnel. That is why this is "
                     "a local address and not the hostname."},
        ]

    def _external_notes(self):
        return [
            "Whatever it reaches is the current primary, so an external read "
            "never comes from a secondary however that switch is set.",
            "Keep `directConnection=true`. Without it the driver goes looking for "
            "the other members, at names that only resolve inside this cluster.",
        ]

    def ca_certificate(self):
        """
        The public half of this component's authority, for a client outside the
        cluster. The KEY is never exposed by any route.

        Empty until the component has been rendered at least once, because that
        is when the authority is issued — the panel does it there rather than at
        create time so that a component created and never deployed leaves no
        private key on disk.
        """
        path = store.path_for(self.name, "tls/ca.crt")
        try:
            with open(path) as fh:
                return fh.read()
        except OSError:
            return ""

    def _credential_notes(self):
        notes = [
            "Every member is named in the string above, including the ones that do "
            "not exist yet. That is deliberate: the driver ignores a seed it cannot "
            "resolve and discovers the real set from the ones it can, which is what "
            "lets this database move onto its own machines without you changing "
            "anything.",
            "TLS is required, and the string above already names the authority "
            "file, so an application on the edge network needs nothing added to "
            "it. That file is mounted into every application component in this "
            "cluster — but only as of its next deploy, so an app that was "
            "running before this database existed has to be redeployed once "
            "before it can connect.",
            "ALL OF THIS IS ABOUT THE STRING ABOVE, which is the in-cluster one. "
            "A client outside the cluster needs none of it: the External panel's "
            "URL carries no TLS options at all, because the gateway holds that "
            "session on its behalf.",
            "THE JAVA DRIVER IS THE EXCEPTION, and it fails quietly. It does not "
            "implement `tlsCAFile` — it logs \"Connection string contains "
            "unsupported option 'tlscafile'\" at WARN, uses the JVM's own trust "
            "store instead, and every operation then fails with \"PKIX path "
            "building failed\". Java's equivalent is an SSLContext on the client: "
            "load this CA into a KeyStore, hand it to MongoClientSettings via "
            "applyToSslSettings, and do NOT set -Djavax.net.ssl.trustStore, which "
            "would replace trust for the whole process.",
            "A JAVA CLIENT ALSO HAS TO ALLOW THE HOST NAME. Swarm names every "
            "service `<stack>_<service>`, and the JDK rejects a host name "
            "containing an underscore before it reads the certificate at all "
            f"(\"Illegal given domain name: {self.member_service(1)}\"), even "
            "though that exact name is in the certificate. Set "
            "invalidHostNameAllowed on the same SSL settings. It gives up less "
            "than it sounds: this authority signs nothing but this component's "
            "own members, so a chain that validates already proves the peer is "
            "one of them.",
        ]
        if self.spec.get("secondary_reads"):
            notes.append(
                "READS MAY GO TO SECONDARIES, and a secondary can be behind the "
                "write that produced what you are reading. To keep read-your-own-"
                "writes, use one causally consistent session per request chain and "
                "carry its operationTime forward:\n"
                "    with client.start_session(causal_consistency=True) as s:\n"
                "        coll.insert_one(doc, session=s)\n"
                "        token = s.operation_time          # send this to the client\n"
                "    # on the next request, before reading:\n"
                "    s.advance_operation_time(token)\n"
                "Without that, a user can save a row and not see it on the next "
                "page load, and nothing anywhere reports an error.")
        else:
            notes.append(
                "Every read goes to the primary, so read-your-own-writes holds "
                "without your application doing anything. The cost is that adding "
                "replicas cannot help read latency — dataguard knows this and will "
                "ask for a bigger machine instead of pretending otherwise.")
        return notes

    # --- validation ---------------------------------------------------------

    def validate(self):
        problems = super().validate()
        hostname = self.spec.get("external_hostname")
        if hostname and not self.external_hostname():
            problems.append(
                f"{hostname!r} is not a tunnel hostname. It is the DNS name you "
                "added to your Cloudflare tunnel — `db.example.com` — not an "
                "address and not a port. Left as it is, this database would "
                "quietly stay reachable from inside the cluster only.")
        cache = self.spec.get("cache_mb") or 0
        reserved = self.spec.get("memory_reservation_mb") or 0
        if cache and reserved and cache >= reserved:
            problems.append(
                f"The WiredTiger cache ({cache} MB) must be below the memory "
                f"reservation ({reserved} MB) — Mongo needs room above its cache "
                "for connections, sorts and index builds.")
        user = (self.spec.get("username") or "").strip()
        if user and not user.replace("_", "").replace("-", "").isalnum():
            problems.append("Root username may only contain letters, digits, - and _.")
        # Deliberately NOT validated here: an even member count, and a managed
        # set with no backup target. Neither is invalid — an even set is legal
        # because dataguard keeps the VOTING members odd underneath it, using
        # non-voting members for the rest, and a set with no backup target runs
        # perfectly well; it just will not be allowed to change shape. Refusing
        # to create either would be the form claiming an authority it does not
        # have. Both are said in the field help and reported at runtime.
        return problems

    # --- TLS material -------------------------------------------------------

    def tls_dir(self):
        return store.path_for(self.name, "tls")

    def ensure_tls(self):
        """
        The authority and one certificate per member, as versioned Swarm secrets.

        Done HERE, at deploy time, rather than by dataguard, for one blunt
        reason: the stack REFERENCES these secrets, so they have to exist before
        the first `docker stack deploy` or it fails outright. Dataguard renews
        them afterwards — it is the thing with a loop — and this file reads the
        live secret names back, so a renewal is not undone by the next save.

        Idempotent. An existing authority is opened, never replaced: replacing it
        would invalidate every member certificate at once, and a replica set with
        mismatched certificates does not degrade, it stops.
        """
        import pki

        key_pem, crt_pem = pki.ensure_ca(self.tls_dir(), self.cluster_name(),
                                         self.name)
        self._ensure_secret("tls-ca", crt_pem)
        for index in range(1, self.slots + 1):
            path = store.path_for(self.name, f"tls/member-{index}.pem")
            existing = _read_bytes(path)
            # Expiry is not the only reason to reissue. A client dialling the
            # seed alias checks the certificate against the name it dialled, so a
            # member whose certificate predates the alias would fail the TLS
            # handshake with "hostname mismatch" — the component would keep
            # working internally and stop being reachable by its own connection
            # string. Asking what the certificate covers is what makes that
            # migration happen by itself.
            if existing is not None and not pki.needs_renewal(existing) \
                    and pki.covers(existing, self._member_names(index)):
                self._ensure_secret(f"tls-{index}", existing)
                continue
            host = self.member_service(index)
            pem = pki.issue_member(key_pem, crt_pem, self.cluster_name(), self.name,
                                   self._member_names(index))
            store._write_atomic(path, pem.decode(), 0o600)
            self._ensure_secret(f"tls-{index}", pem, replace=existing is not None)

    def _member_names(self, index):
        """
        Every name this member may be dialled by, for its certificate SAN.

        Nothing here depends on where a client is: the gateway holds the TLS
        session for anything outside, so these are the names members are dialled
        by from inside the cluster and nothing else.

        The list itself is `pki.member_names`, which dataguard renews from as
        well — one definition, so a renewal cannot quietly issue a shorter one,
        and a name added here is reissued rather than waiting for expiry.
        """
        import pki

        return pki.member_names(self.member_service(index), self.seed_alias())

    @staticmethod
    def cluster_name():
        return os.environ.get("APP_NAME", "cluster")

    def _ensure_secret(self, suffix, payload, replace=False):
        """
        Create the next version of a secret if it is missing or being renewed.

        Swarm secrets are immutable, so an update is a new NAME and a redeploy —
        the versioned dance `admin/settings_def.py` documents for the cluster's
        own secrets. The old version is left alone: removing one a running task
        still references fails, and a stale secret costs nothing.
        """
        versions = self.secret_versions(suffix)
        if versions and not replace:
            return versions[-1][1]
        version = (versions[-1][0] + 1) if versions else 1
        name = f"{self.name}-{suffix}-v{version}"
        if isinstance(payload, bytes):
            payload = payload.decode()
        # Through stdin, never a file path and never argv: a private key on a
        # command line is a private key in the master's process table.
        ok, out = base.run(["docker", "secret", "create",
                            "--label", f"infra.component={self.name}",
                            "--label", "infra.managed_by=dataguard",
                            name, "-"], stdin=payload)
        if not ok and "already exists" not in out:
            raise store.ComponentError(f"could not create the secret {name}: {out}")
        return name

    # --- rendering ----------------------------------------------------------

    def _placement(self, index):
        """
        Where member `index` may run.

        Member 1 is the master's copy and is pinned there by construction —
        every state in dataguard's ladder is described relative to "the one on
        the master", so it is not a thing that moves. Members 2 and up carry
        whatever node constraint dataguard last set, READ BACK off the live
        service: writing our idea of it would slam a member onto a different
        machine on the next unrelated save, and a mongod that changes machines
        loses its data directory.
        """
        mode = (self.spec.get("placement_mode") or "auto").strip()
        constraints = []
        if index == 1 or mode == "master":
            constraints.append("node.role == manager")
        else:
            constraints.extend(self._live_node_constraints(self.member_service(index)))
        for extra in (self.spec.get("placement_extra") or "").split(","):
            extra = extra.strip()
            if extra:
                constraints.append(extra)
        return {"constraints": constraints} if constraints else {}

    def _live_node_constraints(self, service):
        out = base.docker_out([
            "service", "inspect", service, "--format",
            "{{range .Spec.TaskTemplate.Placement.Constraints}}{{println .}}{{end}}"])
        return [line.strip() for line in out.splitlines()
                if line.strip().startswith("node.hostname")]

    def _member_replicas(self, index):
        """
        Live count wins, and member 1 is the floor.

        Dataguard owns whether members 2..n are running. Applying this file's
        idea of that would stop a member somebody's database is currently
        primary on, at whatever moment an unrelated setting was saved.
        """
        live = self.live_replicas(self.member_service(index))
        if live is not None:
            return live
        return 1 if index == 1 else 0

    def _command(self, index):
        """
        Start the member, creating the root user first if the volume is empty.

        THE FIRST START IS THE AWKWARD ONE. The official image creates the root
        user by starting a temporary mongod and connecting to it with mongosh,
        which cannot work under `requireTLS` — it fails in a way that reads like
        a broken image rather than a policy conflict. So the user is created here
        against a temporary server bound to 127.0.0.1 INSIDE THE CONTAINER with
        TLS off, and the real server then starts with TLS required from its first
        packet. The alternative — running the real one in a weaker mode for its
        first few minutes — is a hole nobody would remember to close.

        `$$` survives compose interpolation as a literal `$`; the shell is what
        expands it. Without the shell, mongod would be handed the eleven
        characters `$MONGO_INITDB_ROOT_PASSWORD`.
        """
        cache_gb = max(round((self.spec.get("cache_mb") or 256) / 1024, 3), 0.25)
        init = (
            'if [ ! -f /data/db/.infra-initialised ]; then\n'
            '  mongod --dbpath /data/db --bind_ip 127.0.0.1 --port 27018 '
            '--tlsMode disabled --fork --logpath /tmp/init.log\n'
            '  mongosh --quiet --port 27018 --eval '
            '\'db.getSiblingDB("admin").createUser({user: process.env.MONGO_INITDB_ROOT_USERNAME,'
            ' pwd: process.env.MONGO_INITDB_ROOT_PASSWORD,'
            ' roles: [{role: "root", db: "admin"}]})\' || true\n'
            '  mongod --dbpath /data/db --port 27018 --shutdown\n'
            '  touch /data/db/.infra-initialised\n'
            'fi\n')
        server = " ".join([
            "exec", "mongod",
            "--replSet", self.stack,
            "--bind_ip_all",
            "--wiredTigerCacheSizeGB", str(cache_gb),
            # requireTLS, not preferTLS. A mode that ACCEPTS plaintext will carry
            # plaintext the first time a client gets its options wrong, and
            # nothing will say so.
            "--tlsMode", "requireTLS",
            "--tlsCertificateKeyFile", "/run/secrets/tls-member.pem",
            "--tlsCAFile", "/run/secrets/tls-ca.crt",
            # Ordinary clients authenticate with SCRAM and present no
            # certificate; only the MEMBERS use x509, and they present one.
            "--tlsAllowConnectionsWithoutCertificates",
            # x509 from the first day rather than a keyFile. Moving an existing
            # set from keyFile to x509 is a four-stage rolling change; starting
            # here costs nothing and skips it.
            "--clusterAuthMode", "x509",
        ])
        return ["sh", "-c", init + server]

    def _member(self, index):
        volume = f"{self.name}-{index}-data"
        service = {
            "image": f"mongo:{self.spec['version']}",
            "command": self._command(index),
            "environment": {
                "MONGO_INITDB_ROOT_USERNAME": self.spec.get("username") or "root",
                "MONGO_INITDB_ROOT_PASSWORD": self.password(),
            },
            "volumes": [f"{volume}:/data/db"],
            # The alias is what makes the connection string permanent — see
            # `seed_alias`. Every member carries it; Swarm answers it with the
            # ones that are actually running.
            "networks": {base.EDGE_NETWORK: {"aliases": [self.seed_alias()]}},
            "logging": self.loki_logging(),
            "secrets": [
                {"source": self.secret_name("tls-ca"), "target": "tls-ca.crt",
                 "uid": MONGO_UID, "mode": 0o400},
                {"source": self.secret_name(f"tls-{index}"), "target": "tls-member.pem",
                 "uid": MONGO_UID, "mode": 0o400},
            ],
            # Long, because a mongod that is killed mid-checkpoint replays its
            # journal on the next start, and a member that takes ten minutes to
            # come back is a member the set has to elect around.
            "stop_grace_period": "120s",
            "deploy": {
                "replicas": self._member_replicas(index),
                "labels": dict(self.base_labels(), **self.dataguard_labels(index)),
                "placement": self._placement(index),
                "restart_policy": {"condition": "any", "delay": "10s"},
                "resources": self.resources(),
            },
        }
        return service

    def _gateway(self):
        """
        The external endpoint: one address that is always the current primary.

        A replica set is DISCOVERED, and a client outside the cluster cannot do
        that: the set answers with `docs_mongo-2:27017`, Swarm service DNS that
        resolves on the overlay network and nowhere else. So the proxy discovers
        from inside — every member is asked the handshake question and only the
        primary answers `ismaster: true` — and the client is told
        `directConnection=true` and treats this one address as a server.

        THE CHECK IS AN OP_QUERY, and that is not legacy sloppiness. Every driver
        must open with `isMaster`/`hello` over OP_QUERY — it is how the wire
        protocol is negotiated before anything else can be spoken — so it is the
        one message shape mongod is required to keep answering, and it needs no
        authentication. It is built from bytes rather than typed as a hex
        literal so it can be read and tested rather than trusted.

        TLS STARTS HERE RATHER THAN AT THE CLIENT, which is the trade that lets
        an external consumer install nothing. It dials `127.0.0.1`, so an
        end-to-end session would be verified against this component's private
        authority — every consumer, in every language, obtaining that file and
        naming it in a URL, for a hop Cloudflare has already encrypted across
        the internet and the IPsec overlay encrypts inside the cluster. So this
        speaks TLS to the member (`verify required` against the component's own
        authority, `verifyhost` against the alias every member carries) and
        takes the client's connection in the clear. mongod never sees plaintext;
        `requireTLS` is untouched, and an application INSIDE the cluster keeps
        its end-to-end session.
        """
        return base.gateway_service(
            name="mongo",
            port=27017,
            backends=[(self.member_key(i), f"{self.member_service(i)}:27017")
                      for i in self.member_indexes()],
            check=[
                f"tcp-check send-binary {_ISMASTER_QUERY}",
                # `\x08ismaster\x00\x01` — the BSON for `ismaster: true`. A
                # secondary sends the same element with a trailing 0 and does not
                # match, which is the entire decision.
                f"tcp-check expect binary {_ISMASTER_TRUE}",
            ],
            server_options=("ssl verify required "
                            f"ca-file {base.ca_file_for(self.name)} "
                            # The alias, not the member: every member carries it,
                            # so one line covers every slot and a new one needs no
                            # change here.
                            f"verifyhost {self.seed_alias()}"),
            secrets=[{"source": self.secret_name("tls-ca"),
                      "target": f"{self.name}-ca.crt", "mode": 0o444}],
            # Slower than Redis on purpose: each check is a TLS handshake and a
            # connection, and mongod writes a log line at each end of one. Three
            # seconds with `fall 2` takes a demoted primary out well inside the
            # ten-second election timeout that produced it.
            inter="3s",
            labels=self.base_labels(),
            logging=self.loki_logging())

    def dataguard_labels(self, index):
        """
        This member's whole contract with dataguard, carried on the service.

        Nothing about this component exists in dataguard's configuration — it
        discovers by label and reads policy from here, exactly as the autoscaler
        does for an application. A second database is a create form, not an edit
        of another file.
        """
        s = self.spec
        labels = {
            "infra.managed_by": "dataguard",
            # ROLE FIRST, because `dataguard.member` alone is ambiguous.
            # Redis sentinels carry the same `infra.component`, `infra.type` and
            # `dataguard.member` as the data members do, so dataguard's discovery
            # key matched both and whichever service Docker listed second won —
            # it read three running sentinels as three running data members and
            # believed a single-server component was already a three-member set.
            # The role is what separates them; anything without one is a member,
            # so a component deployed before this label existed still reads
            # correctly.
            "dataguard.role": "member",
            "dataguard.member": str(index),
            "dataguard.pool": str(self.slots),
            "dataguard.set": self.stack,
            "dataguard.enabled": "true" if self.managed else "false",
            # `pool - 1`, not `pool`, and no longer a setting: slot 1 is the
            # copy on the master and a grown set has left it, so the reachable
            # ceiling is the slots beyond it. Emitting `pool` handed dataguard a
            # limit one above anything `free_index` could satisfy.
            #
            # This is a bound, not a budget. It used to be a form field the user
            # had to keep in step with a second form field; what actually decides
            # how big this set gets is pressure and what the overseer will buy,
            # exactly as it is for the autoscaler.
            "dataguard.max_members": str(self.slots - 1),
            "dataguard.lag_budget_seconds": str(s.get("lag_budget_seconds") or 10),
            "dataguard.secondary_reads": "true" if s.get("secondary_reads") else "false",
            "dataguard.backup_target": str(s.get("backup_target") or ""),
            "dataguard.max_snapshots": str(s.get("max_snapshots") or 7),
            # This field existed on the form and was emitted nowhere, so nothing
            # in the cluster could act on it — "Backup every 24 hours" was a
            # number you could change with no effect at all. Dataguard reads it
            # to decide when the next snapshot is due.
            "dataguard.backup_interval_hours": str(s.get("backup_interval_hours") or 24),
            "dataguard.viewer": "true" if s.get("visualizer") else "false",
        }
        return labels

    def render(self):
        s = self.spec
        self.ensure_password()
        self.ensure_tls()

        members = self.member_indexes()
        services = {self.member_key(i): self._member(i) for i in members}
        secrets = {self.secret_name("tls-ca"): {"external": True}}
        for i in members:
            secrets[self.secret_name(f"tls-{i}")] = {"external": True}
        volumes = {f"{self.name}-{i}-data": {} for i in members}
        networks = [base.EDGE_NETWORK]

        if self.external_hostname():
            services["gateway"] = self._gateway()

        if s.get("exporter"):
            networks.append(base.MONITORING_NETWORK)
            services["mongo-exporter"] = {
                "image": EXPORTER_IMAGE,
                "command": ["--collect-all", "--compatible-mode"],
                # The exporter connects through the SAME seed list, so it follows
                # the primary through a failover instead of scraping whichever
                # member it was pointed at when it started.
                "environment": {"MONGODB_URI": self.connection_url()},
                "secrets": [{"source": self.secret_name("tls-ca"),
                             "target": f"{self.name}-ca.crt", "mode": 0o444}],
                "networks": [base.EDGE_NETWORK, base.MONITORING_NETWORK],
                "logging": self.loki_logging(),
                "deploy": {
                    "replicas": 1,
                    "placement": {"constraints": ["node.role == manager"]},
                    "labels": dict(self.base_labels(), **{
                        "prometheus.scrape": "true",
                        "prometheus.port": "9216",
                        "prometheus.path": "/metrics",
                    }),
                    "restart_policy": {"condition": "any", "delay": "10s"},
                    "resources": {"reservations": {"cpus": "0.05", "memory": "64M"}},
                },
            }

        if self.managed and s.get("backup_target"):
            services.update(self._backup_services(secrets))

        if s.get("visualizer"):
            services["viewer"] = self._viewer()
            if base.MONITORING_NETWORK not in networks:
                networks.append(base.MONITORING_NETWORK)

        return {
            "version": "3.8",
            "services": services,
            "networks": base.compose_networks(*networks),
            "volumes": volumes,
            "secrets": secrets,
        }

    def _storage_secret(self):
        """
        The Swarm secret holding this component's backup target's S3 keys.

        The Storage tab has always written it — `storage-<target>-v<N>`, keys as
        JSON, created through stdin — and nothing has ever mounted it. So the
        agents came up, the controller came up, and PBM had no storage
        configured and could not write a single byte anywhere. The tab looked
        finished; the wire stopped one connector short.
        """
        target = (self.spec.get("backup_target") or "").strip()
        if not target:
            return "", None
        import storage as storage_store
        return storage_store.secret_name(target), storage_store.by_name(target)

    def pbm_storage_config(self):
        """
        PBM's `storage` document for this component's target, or None.

        The keys are NOT in here. They live in the mounted secret, and the
        config that references them is written inside the controller at the
        moment `pbm config` runs — see `configure_backups`.
        """
        _, target = self._storage_secret()
        if not target:
            return None
        return {
            "storage": {
                "type": "s3",
                "s3": {k: v for k, v in {
                    "region": target.get("region") or "us-east-1",
                    "bucket": target.get("bucket") or "",
                    "prefix": (target.get("prefix") or "").strip("/") or self.name,
                    "endpointUrl": target.get("endpoint") or "",
                    "forcePathStyle": bool(target.get("path_style")),
                    "serverSideEncryption": ({"sseAlgorithm": "AES256"}
                                             if target.get("sse") else None),
                }.items() if v not in ("", None, False)},
            },
            "pitr": {"enabled": True},
        }

    def configure_backups(self):
        """
        Hand PBM its storage. Idempotent, and safe to run after every deploy.

        `pbm config` had no call site anywhere in this repo. Everything around it
        existed — the target, its credentials in a Swarm secret, an agent beside
        every member, a controller to drive them — and the one command that tells
        PBM where to put a backup was never run. So every snapshot had nowhere to
        go, and the Storage tab was a finished design with the last connector
        unattached.

        The YAML is assembled INSIDE the controller, by a shell that reads the
        two keys out of the mounted secret and expands them into a heredoc. They
        are therefore never an argument to anything: `docker exec ... sh -c` puts
        its script in the master's process table, and `ps` is readable by
        anything else on the box.
        """
        config = self.pbm_storage_config()
        if config is None:
            return True, ""
        container = self._controller()
        if not container:
            return False, "No backup controller is running on this node."

        # Escaped for the unquoted heredoc below, so a bucket or endpoint
        # containing a shell metacharacter is data rather than code.
        def shell_safe(text):
            return (str(text).replace("\\", "\\\\").replace("$", "\\$")
                    .replace("`", "\\`"))

        s3 = config["storage"]["s3"]
        body = ["storage:", "  type: s3", "  s3:"]
        for key, value in s3.items():
            if key == "serverSideEncryption":
                body.append("    serverSideEncryption:")
                body.append(f"      sseAlgorithm: {value['sseAlgorithm']}")
            elif isinstance(value, bool):
                body.append(f"    {key}: {'true' if value else 'false'}")
            else:
                body.append(f"    {key}: {shell_safe(value)}")
        # Under `s3`, where PBM looks for it, and written by the same heredoc
        # rather than appended afterwards — appending would land it after
        # whatever came next and quietly configure nothing.
        body += ["    credentials:",
                 "      access-key-id: $ACCESS",
                 "      secret-access-key: $SECRET",
                 "pitr:", "  enabled: true"]

        script = "\n".join([
            "set -eu",
            "umask 077",
            "CREDS=/run/secrets/backup-storage.json",
            "ACCESS=$(sed -n 's/.*\"access_key\"[^\"]*\"\\([^\"]*\\)\".*/\\1/p' \"$CREDS\")",
            "SECRET=$(sed -n 's/.*\"secret_key\"[^\"]*\"\\([^\"]*\\)\".*/\\1/p' \"$CREDS\")",
            '[ -n "$ACCESS" ] && [ -n "$SECRET" ] || { echo "storage credentials are unreadable"; exit 1; }',
            "cat > /tmp/pbm.yaml <<YAML",
            "\n".join(body),
            "YAML",
            "pbm config --file /tmp/pbm.yaml",
        ])
        ok, out = base.run(["docker", "exec", container, "sh", "-c", script],
                           timeout=120)
        return ok, out or "Backup storage configured."

    def _backup_services(self, secrets):
        """
        Percona Backup for MongoDB: an agent beside every member, one controller.

        PBM is used rather than a mongodump loop because PITR — a continuous
        oplog between full snapshots, so you can restore to a second rather than
        to a snapshot — is genuinely hard to get right, and a backup that
        restores WRONG is worse than none at all.

        The controller is pinned to the master so the panel and dataguard can
        both `docker exec` into it. It is the pbm CLI and nothing else; the CLI
        writes commands into the database and the agents pick them up, so nothing
        here reimplements PBM's control protocol.
        """
        out = {}
        uri = self.connection_url()
        # The bucket's keys, mounted where `configure_backups` reads them. The
        # Storage tab has always written this secret and nothing ever mounted
        # it, which is half of why no backup could reach S3.
        storage_secret, _ = self._storage_secret()
        storage_mount = ([{"source": storage_secret,
                           "target": "backup-storage.json", "mode": 0o400}]
                         if storage_secret else [])
        for index in range(1, self.slots + 1):
            out[f"pbm-agent-{index}"] = {
                "image": PBM_IMAGE,
                "command": ["pbm-agent"],
                "environment": {"PBM_MONGODB_URI": uri},
                "volumes": [f"{self.name}-{index}-data:/data/db"],
                "secrets": [{"source": self.secret_name("tls-ca"),
                             "target": f"{self.name}-ca.crt", "mode": 0o444}]
                           + storage_mount,
                "networks": [base.EDGE_NETWORK],
                "logging": self.loki_logging(),
                "deploy": {
                    # An agent follows its member exactly: it reads that member's
                    # data directory for a physical backup, so it is not a thing
                    # that can live on another machine.
                    "replicas": self._member_replicas(index),
                    "placement": self._placement(index),
                    "labels": dict(self.base_labels(),
                                   **{"infra.managed_by": "dataguard",
                                      "dataguard.role": "pbm-agent"}),
                    "restart_policy": {"condition": "any", "delay": "10s"},
                    "resources": {"reservations": {"cpus": "0.02", "memory": "64M"}},
                },
            }
        out["pbm-ctl"] = {
            "image": PBM_IMAGE,
            # It exists to be exec'd into. Sleeping is the honest way to say so.
            "command": ["sh", "-c", "exec sleep infinity"],
            "environment": {"PBM_MONGODB_URI": uri},
            "secrets": [{"source": self.secret_name("tls-ca"),
                         "target": f"{self.name}-ca.crt", "mode": 0o444}]
                       + storage_mount,
            "networks": [base.EDGE_NETWORK],
            "logging": self.loki_logging(),
            "deploy": {
                "replicas": 1,
                "placement": {"constraints": ["node.role == manager"]},
                "labels": dict(self.base_labels(),
                               **{"infra.managed_by": "dataguard",
                                  "dataguard.role": "pbm-ctl"}),
                "restart_policy": {"condition": "any", "delay": "30s"},
                "resources": {"reservations": {"cpus": "0.02", "memory": "48M"}},
            },
        }
        return out

    def _viewer(self):
        """
        A browser console over the database, off by default and stopped when idle.

        NO PORTS, and that is the whole security model. It has full access with
        no password of its own, so it is never published and never on the tunnel:
        the panel proxies it behind the session you are already signed in to, and
        dataguard scales it back to zero once nobody has looked at it. Starting
        at zero replicas means it does not exist until somebody asks for it.
        """
        return {
            "image": VIEWER_IMAGE,
            "environment": {
                "ME_CONFIG_MONGODB_URL": self.connection_url(),
                "ME_CONFIG_MONGODB_ENABLE_ADMIN": "true",
                # The URL says `tls=true` and mongo-express ALSO hands the
                # driver an `ssl` option of its own. The driver treats the two
                # as one setting and refuses to start when they disagree —
                # `All values of tls/ssl must be the same` — so leaving this
                # unset is a crash loop, not a default.
                "ME_CONFIG_MONGODB_SSL": "true",
                # And the certificate the members present is signed by this
                # component's own authority, which is in no public root store.
                # mongo-express only exposes the driver's `sslCA` option, which
                # the driver stopped reading two major versions ago, so the CA
                # is given to Node itself instead — the one place both the
                # console and its driver are guaranteed to look.
                "NODE_EXTRA_CA_CERTS": base.ca_file_for(self.name),
                # Its own auth is off ON PURPOSE. Two passwords for one door is
                # one password nobody rotates; the door is the panel's session,
                # and the service has no published port for anything else to
                # knock on. Nor does this widen `edge`: every application
                # container there already holds the root connection string, so
                # a console reachable from it grants nothing it did not have.
                #
                # `ME_CONFIG_BASICAUTH` and nothing else. `…BASICAUTHENABLED`
                # was the name in mongo-express 0.x, 1.0 renamed it, and the
                # image ships `ME_CONFIG_BASICAUTH=true` in its own Dockerfile —
                # so setting the old name read as "off" here while the console
                # actually sat behind the shipped defaults, `admin:pass`. A
                # setting that is ignored is worse than one that is wrong: it
                # says the thing it did not do.
                "ME_CONFIG_BASICAUTH": "false",
                "ME_CONFIG_SITE_BASEURL": f"/components/{self.name}/viewer/",
            },
            "secrets": [{"source": self.secret_name("tls-ca"),
                         "target": f"{self.name}-ca.crt", "mode": 0o444}],
            # BOTH networks, and `monitoring` is the one that makes View work.
            # `edge` is where the database is; `monitoring` is where the PANEL
            # is, and the panel proxying to a name it cannot resolve is a 502
            # with nothing in any log to say why. The visualiser is infra, not
            # an application, so it belongs on the infra network beside the
            # exporter rather than the panel being moved onto `edge` — putting a
            # root console on the network every application container shares
            # would buy the same feature at a much worse price.
            "networks": [base.EDGE_NETWORK, base.MONITORING_NETWORK],
            "logging": self.loki_logging(),
            "deploy": {
                "replicas": self.live_replicas(f"{self.stack}_viewer") or 0,
                # On the manager because that is where the panel is, and the
                # panel is the only thing that may reach it.
                "placement": {"constraints": ["node.role == manager"]},
                "labels": dict(self.base_labels(),
                               **{"infra.managed_by": "dataguard",
                                  "dataguard.role": "viewer"}),
                "restart_policy": {"condition": "any", "delay": "10s"},
                "resources": {"reservations": {"cpus": "0.02", "memory": "96M"}},
            },
        }

    # --- panel surface ------------------------------------------------------

    def tabs(self):
        tabs = [("overview", "Overview"), ("credentials", "Credentials"),
                ("map", "Map")]
        if self.managed:
            tabs.append(("backups", "Backups"))
        return tabs + [("settings", "Settings"), ("logs", "Logs")]

    def actions(self):
        actions = super().actions()
        actions.pop("rollback", None)   # a database is not a thing to roll back casually
        actions["rotate"] = base.action(
            self.rotate_password, "Rotate password",
            "Generate a new password, change it in the running set and restart with "
            "it? Anything still using the old one will stop being able to "
            "authenticate.",
            tone="danger", when="running")
        if self.managed and self.spec.get("backup_target"):
            actions["backup"] = base.action(
                self.backup_now, "Back up now", when="running")
            # Rendered by the Backups tab next to the snapshot it needs, so the
            # button row skips it — the same arrangement as deploy-image.
            actions["recover"] = base.action(None, "Restore a snapshot", tone="danger")
        return actions

    def backup_now(self):
        container = self._controller()
        if not container:
            return False, ("No backup controller is running on this node. It is "
                           "pinned to the master; check `docker service ps "
                           f"{self.stack}_pbm-ctl`.")
        # PBM keeps its storage config in the database, so this is a one-off in
        # practice — but it is idempotent and it is the difference between a
        # backup and an error about having nowhere to write.
        ok, out = self.configure_backups()
        if not ok:
            return False, f"Could not configure backup storage: {out}"
        ok, out = base.run(["docker", "exec", container, "pbm", "backup", "--wait"],
                           timeout=3600)
        return ok, out or "Backup complete."

    def snapshots(self):
        """[(name, when, size)] plus the PITR ranges, or [] if PBM cannot answer."""
        container = self._controller()
        if not container:
            return []
        ok, out = base.run(["docker", "exec", container, "pbm", "list", "--out=json"],
                           timeout=60)
        if not ok:
            return []
        import json
        try:
            data = json.loads(out)
        except ValueError:
            return []
        return data.get("snapshots", []), data.get("pitr", {})

    def restore(self, snapshot=None, point_in_time=None):
        """
        Put the database back to a moment in the past. Destructive, and says so.

        Everything written after the target is gone — that is what restoring
        means, and the confirmation the panel shows names the timestamp so it
        cannot be misread as "merge in a backup".
        """
        container = self._controller()
        if not container:
            return False, "No backup controller is running on this node."
        if point_in_time:
            argv = ["pbm", "restore", "--time", point_in_time, "--wait"]
            what = f"the state at {point_in_time}"
        elif snapshot:
            argv = ["pbm", "restore", snapshot, "--wait"]
            what = f"snapshot {snapshot}"
        else:
            return False, "Nothing to restore to."
        ok, out = base.run(["docker", "exec", container] + argv, timeout=7200)
        if not ok:
            return False, f"Restore failed: {out}"
        return True, (f"Restored to {what}. Everything written after it is gone. "
                      "Check that the set has a primary before sending traffic.")

    # --- migrate to and from Atlas ------------------------------------------

    #: The service that runs one migration. Named like a stack service so it
    #: reads correctly in `docker service ls`, but created with
    #: `docker service create` and NOT carrying the stack's namespace label —
    #: which is what keeps `docker stack deploy --prune` from deleting a
    #: migration that is halfway through because somebody saved an unrelated
    #: setting on another tab.
    MIGRATE_ROLE = "migrate"

    def migrate_service(self):
        return f"{self.stack}_migrate"

    #: The job, as a script. It runs inside the container, so the only things on
    #: any command line are file paths — the two connection strings are written
    #: to files and passed with `--config`, which the tools read a `uri:` out of.
    #: A URI in argv is a URI in the master's process table, and `ps` is readable
    #: by anything on the box.
    _MIGRATE_SCRIPT = r"""set -eu
umask 077
say() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# Both connection strings arrive as mounted secrets and are written into files
# the tools read. NOTHING puts a URI on a command line: container arguments show
# up in the master's own process table, and one of these two is a full
# credential for somebody else's cluster.
# The readability check is not paranoia: a secret mounted 0400 without an
# explicit owner belongs to root, this image does not run as root, and every
# symptom of that appears LATER and somewhere else — an empty `uri:` makes
# mongosh fall back to localhost and report ECONNREFUSED, which reads like a
# wrong password. Say what is actually wrong, at the point it is knowable.
cfg() {
  [ -r "$1" ] || { say "cannot read $1 — the secret is mounted for another user"
                   exit 1; }
  printf 'uri: %s\n' "$(cat "$1")" > "$2"
}
cfg /run/secrets/migrate-here.uri  /tmp/here.yaml
cfg /run/secrets/migrate-there.uri /tmp/there.yaml

# mongosh has no --config, so its URI goes into the SCRIPT it runs, which is
# also a file. A URI cannot contain a newline, so escaping a backslash and a
# double quote is the whole job of turning it into a JS string literal.
counter() {
  { printf 'const c = Mongo("'
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' "$1" | tr -d '\n'
    printf '");\n'
    cat <<'JS'
const out = [];
for (const name of c.getDBNames()) {
  if (["admin", "local", "config"].includes(name)) continue;
  const d = c.getDB(name);
  for (const coll of d.getCollectionNames()) {
    out.push(name + "." + coll + "=" + d.getCollection(coll).countDocuments({}));
  }
}
print(out.sort().join("\n"));
JS
  } > "$2"
}
counter /run/secrets/migrate-here.uri  /tmp/count-here.js
counter /run/secrets/migrate-there.uri /tmp/count-there.js
count() { mongosh --quiet --nodb --file "$1"; }

case "$DIRECTION" in
  export) FROM=/tmp/here.yaml;  TO=/tmp/there.yaml
          FROM_JS=/tmp/count-here.js; TO_JS=/tmp/count-there.js
          say "self-host -> Atlas" ;;
  import) FROM=/tmp/there.yaml; TO=/tmp/here.yaml
          FROM_JS=/tmp/count-there.js; TO_JS=/tmp/count-here.js
          say "Atlas -> self-host" ;;
  *) say "unknown direction $DIRECTION"; exit 2 ;;
esac

say "preflight: reading both ends"
BEFORE="$(count "$FROM_JS")" || { say "cannot read the source"; exit 1; }
TARGET="$(count "$TO_JS")"   || { say "cannot read the destination"; exit 1; }
say "source holds:"; printf '%s\n' "$BEFORE"

# Refusing HERE rather than after the dump: a non-empty destination is the one
# preflight answer that means somebody is about to lose data, and it costs
# nothing to find that out before an hour of copying instead of after it.
if [ -n "$TARGET" ] && [ "$OVERWRITE" != "yes" ]; then
  say "the destination is NOT empty and overwrite was not confirmed; refusing"
  printf '%s\n' "$TARGET"
  exit 1
fi

say "dumping"
mongodump --config "$FROM" --archive=/tmp/dump.gz --gzip --quiet
say "dump is $(wc -c < /tmp/dump.gz) bytes"

say "restoring"
mongorestore --config "$TO" --archive=/tmp/dump.gz --gzip --drop --quiet

# A migration nobody counted is a hypothesis — the same argument the design
# makes about backups. This exits non-zero on a mismatch because a job that
# reports success for an incomplete copy is worse than one that fails.
say "verify: counting both ends"
AFTER="$(count "$TO_JS")"
if [ "$BEFORE" = "$AFTER" ]; then
  say "VERIFIED: every collection matches"
  printf '%s\n' "$AFTER"
  exit 0
fi
say "MISMATCH — the copy does not equal the source"
say "source:"; printf '%s\n' "$BEFORE"
say "destination:"; printf '%s\n' "$AFTER"
exit 1
"""

    def migrate_status(self):
        """
        `{state, since, detail}` for the running or last migration, or None.

        Read from Swarm rather than from a file this panel writes: the job IS a
        service, so its task state is the truth about whether it is running, and
        it survives the panel restarting, which an in-process phase would not.
        """
        out = base.docker_out([
            "service", "ps", self.migrate_service(), "--no-trunc",
            "--format", "{{.CurrentState}}\t{{.Error}}"])
        rows = [line for line in out.splitlines() if line.strip()]
        if not rows:
            return None
        current, _, error = rows[0].partition("\t")
        word = current.split()[0].lower() if current else "unknown"
        return {
            "state": word,
            "since": current,
            "running": word in ("running", "preparing", "starting", "ready",
                                "assigned", "accepted", "new", "pending"),
            "ok": word == "complete",
            "detail": error.strip(),
        }

    def migrate_logs(self, lines=200):
        ok, out = base.run(["docker", "service", "logs", "--no-trunc", "--raw",
                            "--tail", str(lines), self.migrate_service()],
                           timeout=30)
        return out if ok else ""

    def start_migration(self, direction, overwrite=False):
        """
        Launch one migration, detached, and return immediately.

        Detached for the reason every other long action here is: this panel is
        served through Cloudflare's 100-second origin timeout, and a dump of any
        real database is longer than that. Swarm runs the job; the Backups tab
        reads its state back.

        `--restart-condition none`, so a job that fails stays failed and says so
        instead of dumping the database again every thirty seconds.
        """
        if direction not in ("export", "import"):
            return False, "Unknown direction."
        if not self.managed:
            return False, ("Migration needs the managed shape — turn Dataguard on "
                           "first, so there is a replica set to read from.")
        atlas = (self.secret("ATLAS_URI") or "").strip()
        if not atlas:
            return False, ("No Atlas connection string is set. Add it on the "
                           "Credentials tab first.")
        running = self.migrate_status()
        if running and running["running"]:
            return False, "A migration is already running for this component."

        # Both URIs as secrets, both through stdin. The Atlas one is rewritten
        # every run because it may have changed on the Credentials tab since the
        # last migration, and a job authenticating with last month's password is
        # a confusing way to find that out.
        here = self._ensure_secret("migrate-here", self.connection_url(), replace=True)
        there = self._ensure_secret("migrate-there", atlas, replace=True)

        base.run(["docker", "service", "rm", self.migrate_service()], timeout=60)
        ok, out = base.run([
            "docker", "service", "create", "--detach",
            "--name", self.migrate_service(),
            "--restart-condition", "none",
            "--network", base.EDGE_NETWORK,
            "--constraint", "node.role == manager",
            "--secret", f"source={here},target=migrate-here.uri,"
                        f"uid={MIGRATE_UID},mode=0400",
            "--secret", f"source={there},target=migrate-there.uri,"
                        f"uid={MIGRATE_UID},mode=0400",
            "--secret", f"source={self.secret_name('tls-ca')},"
                        f"target={self.name}-ca.crt,mode=0444",
            "--env", f"DIRECTION={direction}",
            "--env", f"OVERWRITE={'yes' if overwrite else 'no'}",
            "--label", f"infra.component={self.name}",
            "--label", f"dataguard.role={self.MIGRATE_ROLE}",
            "--reserve-cpu", "0.1", "--reserve-memory", "256M",
            MIGRATE_IMAGE, "sh", "-c", self._MIGRATE_SCRIPT,
        ], timeout=120)
        if not ok:
            return False, f"Could not start the migration: {out}"
        way = ("this cluster into Atlas" if direction == "export"
               else "Atlas into this cluster")
        return True, (f"Migrating {way}. It runs in the background — this tab "
                      f"shows how it ends. Writes made while it runs are NOT "
                      f"copied.")

    def _controller(self):
        out = base.docker_out([
            "ps", "--filter",
            f"label=com.docker.swarm.service.name={self.stack}_pbm-ctl",
            "--filter", "status=running", "--format", "{{.ID}}"])
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        return lines[0] if lines else ""

    def access(self):
        return {
            "target": ",".join(self.seed_hosts()),
            "note": ("These are the same hosts the connection string on the Credentials tab names, in the same order — this box leaves out the scheme, the password and the options because it is not behind a reveal, and the Overview tab must not put a password on screen. Connect with the string, not with this; this is here for a client that wants the hosts as a list rather than as a URL. "
                     "Every member is reachable from any component on the edge "
                     "network by these names, whether it exists yet or not — the "
                     "driver ignores the ones it cannot resolve. Do not put this "
                     "behind the tunnel; the tunnel speaks HTTP."),
        }

    def summary(self):
        # The live count alone, for the reason given on RedisComponent.summary:
        # `1/4 members` read as three missing ones, when they were slots nobody
        # had needed yet.
        live = sum(1 for i in range(1, self.slots + 1)
                   if (self.live_replicas(self.member_service(i)) or 0) >= 1)
        word = "member" if live == 1 else "members"
        return f"mongo:{self.spec.get('version', '')} · {live} {word}"
