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
from urllib.parse import quote

from . import base, store
from .base import Component, Field, Secret

EXPORTER_IMAGE = "percona/mongodb_exporter:0.43.1"
PBM_IMAGE = "percona/percona-backup-mongodb:2.8.0"
VIEWER_IMAGE = "mongo-express:1.0.2-20"

#: The uid the official mongo image runs as. Swarm secrets are mounted with an
#: explicit owner because mongod refuses a certificate or key file it does not
#: own, and the error names the file rather than the ownership.
MONGO_UID = "999"


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
            Field("replica_pool", "Replica pool", "number", 3, required=True,
                  minimum=3, maximum=6, immutable=True,
                  help="How many members BEYOND the one on the master this set may "
                       "ever have. Every one of them is named in the connection "
                       "string from the day you create this, whether it exists or "
                       "not, so raising it later is the one change that DOES alter "
                       "the string — pick the ceiling now. Three is the minimum "
                       "that can lose a machine and still elect."),
            Field("max_members", "Members in use at once", "number", 4,
                  minimum=3, maximum=7,
                  help="A budget cap on how many of the pool dataguard may run at "
                       "once. It does not have to be odd — dataguard keeps the "
                       "VOTING members odd underneath it and carries any extra as "
                       "non-voting, which is what lets a fourth member exist to "
                       "serve reads without making the majority an even number."),
            Field("username", "Root username", "text", "root", required=True,
                  immutable=True,
                  help="Created on the FIRST start only, from an empty volume. "
                       "Changing it later does nothing until the data directory is "
                       "empty again."),
            Field("cache_mb", "WiredTiger cache (MB)", "memory", 256, required=True,
                  minimum=64, maximum=131072,
                  help="Mongo's own data cache. Left unset it is sized from the "
                       "HOST's RAM and ignores this container's limit entirely, "
                       "which is how a small Mongo gets OOM-killed on a large "
                       "machine. Applies to every member."),
            Field("external_port", "Published port", "port", None,
                  minimum=1024, maximum=65535,
                  help="Optional. Publishes the member on the master at this port "
                       "for an external client. The firewall still denies it until "
                       "you open it on this page. It reaches ONE member, not the "
                       "set — a replica set is discovered, not proxied."),
            Field("cpu_reservation", "CPU reserved", "cpu", 0.3, required=True,
                  minimum=0.01, maximum=32),
            Field("memory_reservation_mb", "Memory reserved (MB)", "memory", 768,
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
        ]

    # --- credentials --------------------------------------------------------

    SECRETS = (
        Secret("MONGO_PASSWORD", "Password",
               help="Leave blank to generate a strong one. Set it yourself if you "
                    "are moving an existing database and its clients already know "
                    "the password."),
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
        for index in range(1, self.pool + 1):
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
    def pool(self):
        """Total members named in the connection string: n on top of the master."""
        return int(self.spec.get("replica_pool") or 3) + 1

    def member_key(self, index):
        return f"{self.TYPE}-{index}"

    def member_service(self, index):
        return f"{self.stack}_{self.member_key(index)}"

    def service_key(self):
        """Member 1 is the component's main service — the one on the master."""
        return self.member_key(1)

    def services(self):
        names = [self.member_service(i) for i in range(1, self.pool + 1)]
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

    def seed_hosts(self, port=27017):
        return [f"{self.member_service(i)}:{port}" for i in range(1, self.pool + 1)]

    def connection_url(self, host=None, port=None):
        """
        The string, written once, that never changes again.

        Every option in it is load-bearing:

          replicaSet          makes the driver DISCOVER the topology instead of
                              treating the seeds as a list of separate servers.
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
        hosts = f"{host}:{port or 27017}" if host else ",".join(self.seed_hosts())
        options = [f"replicaSet={self.stack}", "authSource=admin", "tls=true",
                   "retryWrites=true", "w=majority", "readConcernLevel=majority"]
        if self.spec.get("secondary_reads"):
            # maxStalenessSeconds is the DRIVER's own guard and cannot go below
            # 90 by protocol. Dataguard's lag budget is the tight one — it hides
            # a lagging member outright — and this is the belt underneath it, for
            # the seconds before dataguard notices.
            options += ["readPreference=secondaryPreferred", "maxStalenessSeconds=90"]
        return f"mongodb://{user}:{secret}@{hosts}/?{'&'.join(options)}"

    def credentials(self, master_ip=""):
        port = self.spec.get("external_port")
        return {
            "password": self.password(),
            "internal_host": self.member_service(1),
            "internal_port": "27017",
            "internal_url": self.connection_url(),
            "external_port": port,
            "external_host": master_ip,
            "external_url": self.connection_url(master_ip, port) if port else "",
            "ca_certificate": self.ca_certificate(),
            "notes": self._credential_notes(),
        }

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
            "TLS is required. A client outside the cluster needs the CA certificate "
            "above; one inside it gets the same file mounted already.",
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
        pool, cap = self.pool, self.spec.get("max_members") or 0
        if cap and cap > pool:
            problems.append(
                f"Members in use ({cap}) is above the replica pool ({pool}). Only "
                f"{pool} members are named in the connection string, so nothing "
                "could ever address the rest.")
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
        for index in range(1, self.pool + 1):
            path = store.path_for(self.name, f"tls/member-{index}.pem")
            existing = _read_bytes(path)
            if existing is not None and not pki.needs_renewal(existing):
                self._ensure_secret(f"tls-{index}", existing)
                continue
            host = self.member_service(index)
            pem = pki.issue_member(key_pem, crt_pem, self.cluster_name(), self.name,
                                   [host, f"tasks.{host}", "localhost", "127.0.0.1"])
            store._write_atomic(path, pem.decode(), 0o600)
            self._ensure_secret(f"tls-{index}", pem, replace=existing is not None)

    @staticmethod
    def cluster_name():
        return os.environ.get("APP_NAME", "cluster")

    def secret_versions(self, suffix):
        prefix = f"{self.name}-{suffix}-v"
        out = []
        for line in base.docker_out(["secret", "ls", "--format", "{{.Name}}"]).splitlines():
            line = line.strip()
            if line.startswith(prefix):
                try:
                    out.append((int(line[len(prefix):]), line))
                except ValueError:
                    continue
        return sorted(out)

    def secret_name(self, suffix):
        """
        The newest existing version of one of this component's Swarm secrets.

        Read from Docker rather than remembered, because dataguard creates new
        versions during a renewal and this file must render whatever is current —
        the same reason the running image and replica count are read back.
        """
        versions = self.secret_versions(suffix)
        return versions[-1][1] if versions else f"{self.name}-{suffix}-v1"

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
            "networks": [base.EDGE_NETWORK],
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
        if index == 1 and self.spec.get("external_port"):
            service["ports"] = [{
                "target": 27017,
                "published": int(self.spec["external_port"]),
                "protocol": "tcp",
                # host mode: the master's interface only, where ufw still denies
                # it until the port is opened deliberately.
                "mode": "host",
            }]
        return service

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
            "dataguard.member": str(index),
            "dataguard.pool": str(self.pool),
            "dataguard.set": self.stack,
            "dataguard.enabled": "true" if self.managed else "false",
            "dataguard.max_members": str(s.get("max_members") or self.pool),
            "dataguard.lag_budget_seconds": str(s.get("lag_budget_seconds") or 10),
            "dataguard.secondary_reads": "true" if s.get("secondary_reads") else "false",
            "dataguard.backup_target": str(s.get("backup_target") or ""),
            "dataguard.viewer": "true" if s.get("visualizer") else "false",
        }
        return labels

    def render(self):
        s = self.spec
        self.ensure_password()
        self.ensure_tls()

        services = {self.member_key(i): self._member(i)
                    for i in range(1, self.pool + 1)}
        secrets = {self.secret_name("tls-ca"): {"external": True}}
        for i in range(1, self.pool + 1):
            secrets[self.secret_name(f"tls-{i}")] = {"external": True}
        volumes = {f"{self.name}-{i}-data": {} for i in range(1, self.pool + 1)}
        networks = [base.EDGE_NETWORK]

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
                             "target": "tls-ca.crt", "mode": 0o444}],
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

        return {
            "version": "3.8",
            "services": services,
            "networks": base.compose_networks(*networks),
            "volumes": volumes,
            "secrets": secrets,
        }

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
        for index in range(1, self.pool + 1):
            out[f"pbm-agent-{index}"] = {
                "image": PBM_IMAGE,
                "command": ["pbm-agent"],
                "environment": {"PBM_MONGODB_URI": uri},
                "volumes": [f"{self.name}-{index}-data:/data/db"],
                "secrets": [{"source": self.secret_name("tls-ca"),
                             "target": "tls-ca.crt", "mode": 0o444}],
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
                         "target": "tls-ca.crt", "mode": 0o444}],
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
                # Its own auth is off ON PURPOSE. Two passwords for one door is
                # one password nobody rotates; the door is the panel's session,
                # and the service has no published port for anything else to knock on.
                "ME_CONFIG_BASICAUTHENABLED": "false",
                "ME_CONFIG_SITE_BASEURL": f"/components/{self.name}/viewer/",
            },
            "secrets": [{"source": self.secret_name("tls-ca"),
                         "target": "tls-ca.crt", "mode": 0o444}],
            "networks": [base.EDGE_NETWORK],
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
            "note": ("Every member is reachable from any component on the edge "
                     "network by these names, whether it exists yet or not — the "
                     "driver ignores the ones it cannot resolve. Do not put this "
                     "behind the tunnel; the tunnel speaks HTTP."),
        }

    def summary(self):
        live = sum(1 for i in range(1, self.pool + 1)
                   if (self.live_replicas(self.member_service(i)) or 0) >= 1)
        return f"mongo:{self.spec.get('version', '')} · {live}/{self.pool} members"
