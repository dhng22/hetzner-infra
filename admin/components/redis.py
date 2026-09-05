"""
A Redis with a Sentinel quorum in front of it, addressed the same way on the day
you create it as on the day it has four machines.

THE SAME TRICK AS MONGO, AND ONE HONEST DIFFERENCE
--------------------------------------------------
The connection URL names the SENTINELS, not the server, from the first day —
while there is one Redis and one sentinel, both on the master. A sentinel-aware
client asks the sentinels who the primary is, so a failover changes the answer
and never the question. That is the whole reason the URL can be written once.

The difference from Mongo is worth stating plainly rather than hiding: a client
that cannot speak Sentinel will not fail over. `redis://host:6379` is a promise
that one server is the database, and no amount of infrastructure can keep that
promise while the server changes. redis-py, ioredis, Lettuce and go-redis all
speak it; if yours does not, this component is HA for the data and not for that
client, and the Credentials tab says so instead of implying otherwise.

WHO VOTES
---------
The SENTINELS vote, not the data nodes — a separate quorum from the replicas,
which is why adding a replica here does not change the arithmetic the way adding
a replica-set member does. The sentinel count is what must stay odd, and it
follows the machine count: one while everything is on the master, three once
there are three machines to spread them across.

BACKUP NEEDS AOF, AND IS NOT WHAT MONGO'S IS
--------------------------------------------
With `appendonly` off there is nothing on disk to ship. With it on, what is
shipped is an RDB snapshot plus the append-only file, and a restore replays to
the last `everysec` fsync. That is up to a second of writes, and it is NOT
arbitrary-timestamp point-in-time recovery the way PBM gives Mongo. Calling both
of them "PITR" in the same panel would be the panel lying to you about which one
you have.
"""

from urllib.parse import quote

from . import base, store
from .base import Component, Field, Secret

EXPORTER_IMAGE = "oliver006/redis_exporter:v1.66.0"
VIEWER_IMAGE = "redis/redisinsight:2.60"

#: How many sentinels exist ONCE THERE IS SOMETHING TO FAIL OVER TO. Odd,
#: always: an even quorum tolerates the same number of failures as the odd one
#: below it while costing another container.
#:
#: All three are DECLARED from the first deploy and only the first one RUNS while
#: the component is a single server on the master — the same live-count-wins
#: contract the data members already have, so dataguard starts the other two when
#: it starts the second member and stops them again when it shrinks back. Three
#: sentinels pinned to one machine is not a quorum, it is three copies of the
#: same single point of failure, and it was costing three containers to watch a
#: server that had no replica to be promoted in its place.
#:
#: The quorum below stays 2 in every case, and that is deliberate rather than an
#: oversight: while one sentinel is running the set has one member, so there is
#: nothing a failover could promote and a sentinel that cannot reach quorum is
#: exactly the right behaviour.
SENTINEL_COUNT = 3


def _resp(*words):
    """
    One Redis command as RESP, hex-encoded, for `tcp-check send-binary`.

    Redis has spoken this since 2.0 and the inline alternative does not survive
    a password with a space in it, so the array form is the only one that is
    correct for every password this component will accept.
    """
    out = b"*%d\r\n" % len(words)
    for word in words:
        raw = word.encode()
        out += b"$%d\r\n%s\r\n" % (len(raw), raw)
    return out.hex()


class RedisComponent(Component):
    TYPE = "redis"
    LABEL = "Redis"
    BLURB = "A password-protected Redis with Sentinel failover."
    CATEGORY = "Data"
    GROUP = "Database"
    KEEPS_VOLUME = True
    MANAGER_FIELD = "dataguard"
    GROUPS = (
        ("observability", "Observability", None,
         "Two containers you can have alongside the database. The exporter is what "
         "Grafana and every Redis alert read it through. The visualiser is a "
         "console with full access to your data and no password of its own, so it "
         "is never published — the View button proxies it through the panel "
         "session you are already signed in to."),
        ("dataguard", "Dataguard", "dataguard",
         "With this off you get one Redis, one volume and no failover. Dataguard "
         "still measures this component either way."),
    )

    @classmethod
    def fields(cls):
        return [
            Field("placement_mode", "Placement", "choice", "auto",
                  choices=("auto", "master", "any"),
                  help="`auto` lets dataguard decide which machines the replicas "
                       "run on, which is the only way it can grow this off the "
                       "master. `master` keeps everything on the node that will "
                       "not be deleted, and turns dataguard off."),
            Field("placement_extra", "Extra constraints", "text", "",
                  placeholder="node.labels.disk == ssd",
                  help="Comma separated, added to whatever the mode implies."),
            Field("version", "Version", "choice", "7.4-alpine",
                  choices=("7.4-alpine", "7.2-alpine", "6.2-alpine"),
                  help="Changing this restarts the replicas one at a time. The "
                       "volumes survive."),
            Field("maxmemory_mb", "Max memory (MB)", "memory", 512, required=True,
                  minimum=16, maximum=65536,
                  help="Redis evicts above this. Keep it below the memory limit or "
                       "the container is OOM-killed before Redis ever starts "
                       "evicting. Applies to every replica."),
            Field("maxmemory_policy", "Eviction policy", "choice", "allkeys-lru",
                  choices=("allkeys-lru", "allkeys-lfu", "volatile-lru",
                           "volatile-ttl", "noeviction"),
                  help="`noeviction` turns a full cache into write errors — correct "
                       "for a queue, wrong for a cache."),
            Field("appendonly", "Persist to disk (AOF)", "bool", True,
                  help="Survives a restart, and is what makes a replica able to "
                       "resync without a full transfer. Turn it off for a pure "
                       "cache — and note that with it off there is nothing to back "
                       "up, so the backup fields below stop meaning anything."),
            Field("external_hostname", "Tunnel hostname", "text", "",
                  placeholder="cache.example.com",
                  help="A DNS name on a domain in your Cloudflare account, which you invent — `cache.example.com`. Setting it here does not create it: the Credentials tab then gives you three steps, and until you have done them nothing outside the cluster can reach this database. Leave it empty to keep it in-cluster only."),
            Field("cpu_reservation", "CPU reserved", "cpu", 0.2, required=True,
                  minimum=0.01, maximum=32),
            Field("memory_reservation_mb", "Memory reserved (MB)", "memory", 640,
                  required=True, minimum=32, maximum=131072,
                  help="Must exceed max memory with room for Redis itself and, with "
                       "AOF on, for its rewrite buffer — or eviction never gets the "
                       "chance to run."),

            Field("exporter", "Metrics exporter", "bool", True,
                  group="observability", switch=True,
                  help="Runs redis_exporter so Grafana has memory, hit rate, "
                       "connection counts and replication offsets."),
            Field("visualizer", "Data visualizer", "bool", False,
                  group="observability", switch=True,
                  help="RedisInsight, in your browser. It is FULL access with no "
                       "password of its own, so it is never published: the View "
                       "button proxies it through your panel session, and dataguard "
                       "stops it again once nobody has looked at it."),

            Field("dataguard", "Dataguard", "bool", True, group="dataguard",
                  help="Let dataguard own the shape: how many replicas there are, "
                       "which machines they run on, and failover."),
            Field("lag_budget_seconds", "Lag budget (s)", "number", 10,
                  minimum=1, maximum=3600, group="dataguard",
                  help="A replica further behind than this is not promoted and is "
                       "reported. Redis has no per-replica read flag, so unlike "
                       "Mongo this cannot take it out of rotation — a "
                       "sentinel-aware client picks its own."),
            Field("backup_target", "Backup to", "text", "", group="dataguard",
                  placeholder="name from the Storage tab",
                  help="Requires AOF. What is shipped is an RDB snapshot plus the "
                       "append-only file, and a restore replays to the last "
                       "`everysec` fsync — up to a second of writes lost. That is "
                       "NOT the arbitrary-timestamp recovery MongoDB gets here, and "
                       "the difference is worth knowing before you need it."),
            Field("backup_interval_hours", "Backup every (hours)", "number", 24,
                  minimum=1, maximum=720, group="dataguard"),
            Field("max_snapshots", "Keep at most (snapshots)", "number", 7,
                  minimum=1, maximum=365, group="dataguard",
                  help="Once there are more snapshots than this, the oldest are "
                       "deleted after each new one completes — oldest first, and "
                       "only after the new one has succeeded. Storage is billed by "
                       "the gigabyte-month, and a snapshot a day forever is a bill "
                       "that only goes one way."),
        ]

    # --- credentials --------------------------------------------------------

    SECRETS = (
        Secret("REDIS_PASSWORD", "Password",
               help="Leave blank to generate a strong one. Set it yourself if you "
                    "are moving an existing database and its clients already know "
                    "the password."),
    )

    def password(self):
        return self.secret("REDIS_PASSWORD")

    def ensure_password(self):
        if self.password():
            return False
        self.apply_secrets({})
        return True

    def set_password(self, value):
        return self.apply_secrets({"REDIS_PASSWORD": value})

    def rotate_password(self):
        """
        New password, then redeploy every replica and every sentinel.

        One more moving part than it used to have: the sentinels authenticate to
        the servers too, so a rotation that updated only the servers would leave
        the quorum unable to see the primary and calling a failover that could
        not succeed. They are all in this one stack, which is why one redeploy is
        still enough.
        """
        self.rotate_secrets()
        ok, out = self.deploy()
        if not ok:
            return False, f"Password rotated, but the redeploy failed: {out}"
        return True, ("Password rotated; every replica and sentinel restarted with "
                      "it. Anything still using the old one can no longer connect.")

    # --- identity -----------------------------------------------------------

    @property
    def slots(self):
        """
        How many member slots exist. A constant — see `base.MEMBER_SLOTS`.

        SLOTS, not servers: slot 1 is the copy on the master and is never handed
        out for growth, so the grown set is one smaller than this.

        This used to be `replica_pool + 1`, a number chosen at creation and
        frozen, on the stated grounds that "the sentinels are named in the
        connection URL". They are — but there are always exactly
        `SENTINEL_COUNT` of them and the count never depended on it, so the URL
        never changed with it and the whole restriction was answering a question
        Redis does not ask.
        """
        return base.MEMBER_SLOTS

    def member_indexes(self):
        """Every slot this component renders. One when nothing manages it."""
        return range(1, (self.slots if self.managed else 1) + 1)

    @property
    def managed(self):
        return bool(self.spec.get("dataguard"))

    def member_key(self, index):
        """
        The service key for a member. UNSUFFIXED when nothing manages this one.

        The Dataguard panel on this page promises, in as many words, "with this
        off you get one Redis, one volume and no failover" — and the renderer did
        not keep it: it emitted four members and three sentinels either way, and
        the switch only decided whether dataguard would then touch them. For a
        component created before the switch existed that was a silent migration
        with teeth. Components deploy with `--prune`, so `<name>_redis` is not
        left running beside the new `<name>_redis-1`; it is deleted, along with
        the only copy of the data, while every client's connection string
        changes underneath it. A password rotation was enough to trigger it.

        So the suffix belongs to the managed shape, and the single unsuffixed
        service is what an unmanaged Redis has always been called. Turning the
        switch ON is then the migration, done deliberately, once.
        """
        if not self.managed:
            return self.TYPE
        return f"{self.TYPE}-{index}"

    def member_service(self, index):
        return f"{self.stack}_{self.member_key(index)}"

    def sentinel_service(self, index):
        return f"{self.stack}_sentinel-{index}"

    def service_key(self):
        return self.member_key(1)

    def services(self):
        if self.managed:
            names = [self.member_service(i) for i in range(1, self.slots + 1)]
            names += [self.sentinel_service(i)
                      for i in range(1, SENTINEL_COUNT + 1)]
        else:
            names = [self.member_service(1)]
        if self.external_hostname():
            names.append(f"{self.stack}_gateway")
        if self.spec.get("exporter"):
            names.append(f"{self.stack}_redis-exporter")
        if self.spec.get("visualizer"):
            names.append(f"{self.stack}_viewer")
        return names

    # --- addressing ---------------------------------------------------------

    def sentinel_hosts(self):
        return [f"{self.sentinel_service(i)}:26379"
                for i in range(1, SENTINEL_COUNT + 1)]

    def connection_url(self, host=None, port=None):
        """
        The sentinel URL, written once — or the external one, when given a host.

        `redis+sentinel://` is what redis-py takes directly. Everything else in
        the Credentials tab is the same three facts spelled out — sentinel hosts,
        master name, password — because ioredis and Lettuce want them as fields
        rather than as a URL, and handing somebody only the scheme their client
        cannot parse is not help.

        The external form is a plain `redis://` to one address, and the gateway
        makes that address always the current primary — so a failover changes
        nothing about it and the client needs no sentinel support. See
        `_gateway` for why sentinel cannot serve a client outside the cluster.
        """
        if host:
            return (f"redis://default:{quote(self.password(), safe='')}@"
                    f"{host}:{port or 6379}")
        secret = quote(self.password(), safe="")
        if not self.managed:
            # No sentinels exist to ask, so naming them would hand out an
            # address that resolves to nothing.
            return f"redis://default:{secret}@{self.member_service(1)}:6379/0"
        hosts = ",".join(self.sentinel_hosts())
        return f"redis+sentinel://default:{secret}@{hosts}/{self.stack}/0"

    def external_hostname(self):
        return base.clean_hostname(self.spec.get("external_hostname"))

    def credentials(self):
        hostname = self.external_hostname()
        return {
            "password": self.password(),
            # Derived from the SAME list the URL is built from, so the two can
            # never disagree. It used to name each sentinel with its port already attached, beside a
            # Port row repeating it, while the URL beside it and
            # the "How to reach it" panel both named the hosts alone — one component
            # described three different ways on one page.
            "internal_host": ", ".join(h.rsplit(":", 1)[0]
                                       for h in self.sentinel_hosts()),
            "internal_port": "26379",
            "internal_url": self.connection_url(),
            "external_hostname": hostname,
            # What to paste into the Cloudflare dashboard, and what to run
            # beside your application. The panel owes you both exactly, the same
            # way it owes an app its tunnel target — routing is not automated
            # here on purpose.
            "external_target": f"tcp://{self.stack}_gateway:6379",
            "external_command": base.access_command(hostname, 6379) if hostname else "",
            "external_url": self.connection_url("127.0.0.1", 6379) if hostname else "",
            "external_steps": self._external_steps(hostname) if hostname else [],
            "external_notes": self._external_notes(),
            # The port an older cluster published on, so the panel can offer to
            # close the firewall rule that is still standing in front of it.
            "legacy_port": self.spec.get("external_port"),
            "sentinels": self.sentinel_hosts(),
            "master_name": self.stack,
            "notes": [
                "The URL names the SENTINELS, not the server. Your client asks them "
                "who the primary is, so a failover changes the answer and never the "
                "address you configured.",
                "A CLIENT THAT CANNOT SPEAK SENTINEL WILL NOT FAIL OVER. redis-py, "
                "ioredis, Lettuce and go-redis all can; if yours cannot, use the "
                "sentinel host list and master name below with whatever it does "
                "support, or accept that this is HA for the data and not for that "
                "client.",
                "ioredis and Lettuce want the parts rather than the URL: "
                f"sentinels={self.sentinel_hosts()}, name=\"{self.stack}\", "
                "and this password.",
                "THE SENTINELS THEMSELVES TAKE NO PASSWORD; the one above is the "
                "server's. That is not a gap left open by accident — no client "
                "reads a sentinel password out of a sentinel URL, so requiring "
                "one would make the string above unusable in every language. "
                "Reaching the sentinels means being on this cluster's private "
                "network already.",
            ],
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
             "code": f"tcp://{self.stack}_gateway:6379",
             "note": f"In the Cloudflare Zero Trust dashboard, add {hostname} to "
                     "this cluster's tunnel with that target. PUT AN ACCESS POLICY "
                     "ON IT: without one, anyone who knows the name reaches this "
                     "database, with only the password in the way."},
            {"title": "Install cloudflared where your app runs, and keep this running",
             "code": base.access_command(hostname, 6379),
             "note": "It holds the tunnel open and listens on 127.0.0.1:6379. "
                     "Nothing can connect while it is stopped."},
            {"title": "Point your application at this",
             "code": self.connection_url("127.0.0.1", 6379),
             "note": "Your application talks to the helper from step 2, which is "
                     "running beside it and listening on this port; the helper "
                     "carries the traffic through the tunnel. That is why this is "
                     "a local address and not the hostname."},
        ]

    def _external_notes(self):
        return [
            "Whatever it reaches is the current primary; a failover changes "
            "nothing here, and your client needs no sentinel support.",
            "Redis has no TLS of its own. The tunnel encrypts the hop across the "
            "internet; the short hops at each end are in the clear.",
        ]

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
        maxmem = self.spec.get("maxmemory_mb") or 0
        reserved = self.spec.get("memory_reservation_mb") or 0
        if maxmem and reserved and maxmem >= reserved:
            problems.append(
                f"Max memory ({maxmem} MB) must be below the memory reservation "
                f"({reserved} MB) — Redis needs room above the dataset for itself "
                "and, with AOF on, for its rewrite buffer.")
        if self.spec.get("backup_target") and not self.spec.get("appendonly"):
            problems.append(
                "A backup target with AOF off has nothing to back up. Turn on "
                "'Persist to disk', or clear the backup target.")
        return problems

    # --- rendering ----------------------------------------------------------

    def _placement(self, index):
        """
        Where replica `index` may run. Replica 1 is the master's copy.

        Replicas 2 and up carry whatever node constraint dataguard last set, read
        back off the live service — writing this file's idea of it would move a
        replica to a different machine on the next unrelated save, and a Redis
        that changes machines loses its append-only file.
        """
        mode = (self.spec.get("placement_mode") or "auto").strip()
        constraints = []
        if index == 1 or mode == "master":
            constraints.append("node.role == manager")
        else:
            out = base.docker_out([
                "service", "inspect", self.member_service(index), "--format",
                "{{range .Spec.TaskTemplate.Placement.Constraints}}{{println .}}{{end}}"])
            constraints += [line.strip() for line in out.splitlines()
                            if line.strip().startswith("node.hostname")]
        for extra in (self.spec.get("placement_extra") or "").split(","):
            extra = extra.strip()
            if extra:
                constraints.append(extra)
        return {"constraints": constraints} if constraints else {}

    def _member_replicas(self, index):
        live = self.live_replicas(self.member_service(index))
        if live is not None:
            return live
        return 1 if index == 1 else 0

    def _sentinel_replicas(self, index):
        """
        Live count wins, and sentinel 1 is the floor. Same rule as a member.

        Dataguard owns whether sentinels 2 and 3 are running, because it owns
        whether there is a second server for them to fail over to. Applying this
        file's idea of that would stop a sentinel quorum mid-failover at whatever
        moment an unrelated setting was saved.
        """
        live = self.live_replicas(self.sentinel_service(index))
        if live is not None:
            return live
        return 1 if index == 1 else 0

    def _server_command(self, index):
        """
        The server, following replica 1 unless it IS replica 1.

        `--replicaof` on the others is what makes a fresh replica join without
        anybody telling it to; the sentinels notice it and take over from there.
        The primary's own `--replicaof no one` is deliberate and NOT a no-op: a
        replica promoted by a failover has that written into its config, and
        restarting it without this would send it back to following a server that
        is now behind it.

        `$$` survives compose interpolation as a literal `$`, and `sh -c` is what
        expands it — without the shell, Redis enforces the eleven characters
        `$REDIS_PASSWORD` while every client is handed the real one.
        """
        s = self.spec
        args = [
            "redis-server",
            "--requirepass", '"$$REDIS_PASSWORD"',
            # The password a REPLICA uses to authenticate to its primary. Without
            # it, replication silently never starts and the replica reports
            # `master_link_status:down` in a log nobody reads.
            "--masterauth", '"$$REDIS_PASSWORD"',
            "--maxmemory", f"{s['maxmemory_mb']}mb",
            "--maxmemory-policy", s["maxmemory_policy"],
            # Announce the SERVICE NAME, not the container's IP. A sentinel that
            # learned an overlay IP would keep handing clients an address that
            # changes every time the task is rescheduled.
            "--replica-announce-ip", self.member_service(index),
            "--replica-announce-port", "6379",
        ]
        if index == 1:
            args += ["--replicaof", "no", "one"]
        else:
            args += ["--replicaof", self.member_service(1), "6379"]
        if s.get("appendonly"):
            args += ["--appendonly", "yes", "--appendfsync", "everysec"]
        else:
            args += ["--appendonly", "no"]
        return base.shell_command(args)

    def _sentinel_command(self, index):
        """
        A sentinel writes to its own config file, so it gets a real one.

        `sentinel monitor` and friends cannot be passed as flags — redis-sentinel
        takes a config file and REWRITES it as the topology changes. Handing it a
        read-only mount is how you get a sentinel that works until the first
        failover and then crashes trying to record it.
        """
        quorum = SENTINEL_COUNT // 2 + 1
        conf = "\n".join([
            "port 26379",
            # NO `requirepass` HERE, AND IT IS NOT AN OVERSIGHT. It was tried
            # and it broke every client. `auth-pass` below is the password
            # sentinel presents to the MASTER, which is a different door from
            # the one a client knocks on to ask sentinel where the master is.
            #
            # The password in `redis+sentinel://default:<pw>@…` is the DATA
            # NODE's, in every client that reads such a URL — Lettuce, redis-py,
            # ioredis, go-redis all agree on that and none of them sends it to
            # the sentinel hosts. A sentinel password is a separate field none
            # of them will read out of a URL (Lettuce needs
            # `RedisURI.getSentinels()` mutated by hand). So a platform cannot
            # publish one working string AND require sentinel auth; asking for
            # both is how the last deploy left every client stuck on
            # `NOAUTH HELLO must be called with the client already authenticated`
            # and dataguard reading the component as having no master at all.
            #
            # What that gives up, exactly: anything already on `edge` can call
            # `SENTINEL FAILOVER`. It cannot read or write a key — the data
            # nodes keep `requirepass` and `masterauth` — and anything on `edge`
            # is an application container that already holds a database password
            # of its own. Redis ACLs could keep the failover verbs behind auth
            # while leaving discovery open, but the same `default` user is what
            # sentinels use to gossip with each other, and a mistake there is a
            # split brain rather than an error message.
            f"sentinel monitor {self.stack} {self.member_service(1)} 6379 {quorum}",
            f"sentinel auth-pass {self.stack} $$REDIS_PASSWORD",
            f"sentinel down-after-milliseconds {self.stack} 5000",
            f"sentinel failover-timeout {self.stack} 60000",
            f"sentinel parallel-syncs {self.stack} 1",
            # Resolve hostnames: every address here is Swarm service DNS, and a
            # sentinel that refuses to resolve one has no primary to monitor.
            "sentinel resolve-hostnames yes",
            "sentinel announce-hostnames yes",
            f"sentinel announce-ip {self.sentinel_service(index)}",
            "",
        ])
        return ["sh", "-c",
                f'set -e; printf %s "{conf}" > /data/sentinel.conf; '
                'exec redis-sentinel /data/sentinel.conf']

    def dataguard_labels(self, index):
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
            # `pool - 1`, not `pool`: slot 1 is the copy on the master and is
            # never handed out for growth, so a set that has grown off the master
            # can only ever fill the slots beyond it. Telling dataguard `pool`
            # promises a member it has nowhere to put.
            "dataguard.max_members": str(self.slots - 1),
            "dataguard.lag_budget_seconds": str(s.get("lag_budget_seconds") or 10),
            # Redis has no per-replica read flag: a sentinel-aware client chooses
            # a replica itself, and nothing here can take one out of rotation. So
            # this says false rather than claiming a control that does not exist.
            "dataguard.secondary_reads": "false",
            "dataguard.backup_target": str(s.get("backup_target") or ""),
            "dataguard.max_snapshots": str(s.get("max_snapshots") or 7),
            "dataguard.viewer": "true" if s.get("visualizer") else "false",
        }
        return labels

    def _member(self, index):
        volume = f"{self.name}-{index}-data"
        service = {
            "image": f"redis:{self.spec['version']}",
            "command": self._server_command(index),
            "environment": {"REDIS_PASSWORD": self.password()},
            "volumes": [f"{volume}:/data"],
            "networks": [base.EDGE_NETWORK],
            "logging": self.loki_logging(),
            "stop_grace_period": "30s",
            "deploy": {
                "replicas": self._member_replicas(index),
                "labels": dict(self.base_labels(), **self.dataguard_labels(index)),
                "placement": self._placement(index),
                "restart_policy": {"condition": "any", "delay": "5s"},
                "resources": self.resources(),
            },
        }
        return service

    def _gateway(self):
        """
        The external endpoint: one address that is always the current primary.

        `INFO replication` is the whole trick. Every member is a backend and
        every member is asked the same question; only the one that answers
        `role:master` passes, so it is the only one traffic goes to. A failover
        is two checks changing their minds a second apart, and
        `on-marked-down shutdown-sessions` closes the connections that were
        pinned to the old primary so the client reconnects into the new one
        instead of sitting on a server that now refuses writes.

        Sentinel cannot do this job from outside — it answers "the primary is
        `cache_redis-2`", a name that resolves on the overlay network and
        nowhere else — so the proxy asks from inside, where the answer means
        something, and the external client needs no sentinel support.

        The AUTH is sent as RESP hex rather than as text. `tcp-check send` would
        need the password escaped for HAProxy's parser, and it would need it
        escaped again for the shell that writes this file, and a `$` in it would
        be eaten by compose before either of them saw it. Hex has none of those
        characters in it at all.
        """
        return base.gateway_service(
            name="redis",
            port=6379,
            # Every slot the render emits, which for an unmanaged component is
            # the one unsuffixed server: `member_key` collapses to `redis` there,
            # so looping over eight slots would declare the same backend eight
            # times and HAProxy refuses a duplicate server name outright.
            backends=[(self.member_key(i), f"{self.member_service(i)}:6379")
                      for i in self.member_indexes()],
            check=[
                f"tcp-check send-binary {_resp('AUTH', 'default', self.password())}",
                "tcp-check expect string +OK",
                f"tcp-check send-binary {_resp('INFO', 'replication')}",
                # A replica answers `role:slave`, and no field in the section
                # contains this string but the one we want.
                "tcp-check expect string role:master",
                f"tcp-check send-binary {_resp('QUIT')}",
                "tcp-check expect string +OK",
            ],
            labels=self.base_labels(),
            logging=self.loki_logging())

    def _sentinel(self, index):
        return {
            "image": f"redis:{self.spec['version']}",
            "command": self._sentinel_command(index),
            "environment": {"REDIS_PASSWORD": self.password()},
            # Its own small volume, because it rewrites its config as the
            # topology changes and must remember the result across a restart.
            "volumes": [f"{self.name}-sentinel-{index}:/data"],
            "networks": [base.EDGE_NETWORK],
            "logging": self.loki_logging(),
            "deploy": {
                "replicas": self._sentinel_replicas(index),
                # Sentinels are cheap and must be able to see a failure that
                # takes out a machine, so they spread rather than following the
                # servers. Nothing pins them to the master: a quorum that lives
                # entirely on one box cannot survive that box.
                "placement": {"preferences": [{"spread": "node.id"}],
                              "constraints": ["node.labels.dedicated != true"]},
                "labels": dict(self.base_labels(),
                               **{"infra.managed_by": "dataguard",
                                  "dataguard.role": "sentinel",
                                  "dataguard.member": str(index)}),
                "restart_policy": {"condition": "any", "delay": "5s"},
                "resources": {"reservations": {"cpus": "0.01", "memory": "24M"}},
            },
        }

    def viewer_databases(self):
        """
        What RedisInsight is connected to the moment you open it.

        Registered through its REST API by the proxy route, because no
        environment variable in this image can carry an authenticated
        connection — see the note on the viewer service above. This is the same
        call the console's own "add database" form makes.

        Replica 1, not the sentinel list: the console browses ONE server, and
        this is the one that always exists and the one writes go to while the
        set is a single member. A sentinel-aware console would be better and
        RedisInsight is not one.
        """
        return [{"name": self.name,
                 "host": self.member_service(1),
                 "port": 6379,
                 "username": "default",
                 "password": self.password()}]

    def render(self):
        s = self.spec
        self.ensure_password()

        members = self.member_indexes()
        services = {self.member_key(i): self._member(i) for i in members}
        volumes = {f"{self.name}-{i}-data": {} for i in members}
        if self.managed:
            for i in range(1, SENTINEL_COUNT + 1):
                services[f"sentinel-{i}"] = self._sentinel(i)
                volumes[f"{self.name}-sentinel-{i}"] = {}
        networks = [base.EDGE_NETWORK]

        if self.external_hostname():
            services["gateway"] = self._gateway()

        if s.get("exporter"):
            networks.append(base.MONITORING_NETWORK)
            services["redis-exporter"] = {
                "image": EXPORTER_IMAGE,
                "environment": {
                    # Pointed at replica 1 rather than at the sentinels: the
                    # exporter reports on ONE server, and the one on the master is
                    # the one that always exists.
                    "REDIS_ADDR": f"redis://{self.member_service(1)}:6379",
                    "REDIS_PASSWORD": self.password(),
                },
                "networks": [base.EDGE_NETWORK, base.MONITORING_NETWORK],
                "logging": self.loki_logging(),
                "deploy": {
                    "replicas": 1,
                    "placement": {"constraints": ["node.role == manager"]},
                    "labels": dict(self.base_labels(), **{
                        "prometheus.scrape": "true",
                        "prometheus.port": "9121",
                        "prometheus.path": "/metrics",
                    }),
                    "restart_policy": {"condition": "any", "delay": "10s"},
                    "resources": {"reservations": {"cpus": "0.05", "memory": "32M"}},
                },
            }

        if s.get("visualizer"):
            if base.MONITORING_NETWORK not in networks:
                networks.append(base.MONITORING_NETWORK)
            services["viewer"] = {
                "image": VIEWER_IMAGE,
                # ONLY the proxy path. `RI_REDIS_HOST`, `RI_REDIS_PORT` and
                # `RI_REDIS_PASSWORD` were here and are read by NOTHING in this
                # image — grep the bundle and they do not appear. So the console
                # opened on an empty "add a database" form, and the password sat
                # in `docker service inspect` buying nothing. What this build
                # does read is `RI_REDIS_STACK_DATABASE_*`, which applies only
                # when RI_BUILD_TYPE is REDIS_STACK and has no password field at
                # all, so it cannot describe a server with `requirepass` set.
                # `viewer_databases` below is the mechanism that works.
                "environment": {
                    "RI_PROXY_PATH": f"/components/{self.name}/viewer",
                },
                # BOTH networks, and `monitoring` is the one that makes View
                # work. `edge` is where Redis is; `monitoring` is where the PANEL
                # is, and the panel proxying to a name it cannot resolve is a 502
                # with nothing in any log to say why. The visualiser is infra,
                # not an application, so it belongs on the infra network beside
                # the exporter rather than the panel being moved onto `edge` —
                # putting a root console on the network every application
                # container shares would buy the same feature at a much worse
                # price.
                "networks": [base.EDGE_NETWORK, base.MONITORING_NETWORK],
                "logging": self.loki_logging(),
                "deploy": {
                    # Starts at zero. It does not exist until somebody asks for
                    # it, and dataguard puts it away again when they stop looking.
                    "replicas": self.live_replicas(f"{self.stack}_viewer") or 0,
                    "placement": {"constraints": ["node.role == manager"]},
                    "labels": dict(self.base_labels(),
                                   **{"infra.managed_by": "dataguard",
                                      "dataguard.role": "viewer"}),
                    "restart_policy": {"condition": "any", "delay": "10s"},
                    "resources": {"reservations": {"cpus": "0.02", "memory": "128M"}},
                },
            }

        return {
            "version": "3.8",
            "services": services,
            "networks": base.compose_networks(*networks),
            "volumes": volumes,
        }

    # --- actions ------------------------------------------------------------

    def _local_container(self, service=None):
        out = base.docker_out([
            "ps", "--filter",
            f"label=com.docker.swarm.service.name={service or self.member_service(1)}",
            "--filter", "status=running", "--format", "{{.ID}}"])
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        return lines[0] if lines else ""

    def _redis_cli(self, container, *args):
        # The password is read from the container's own environment rather than
        # passed in argv, so it never appears in the process table of the master
        # or in any error this returns.
        return base.run(["docker", "exec", container, "sh", "-c",
                         'exec redis-cli --no-auth-warning -a "$REDIS_PASSWORD" '
                         + " ".join(base.quote(a) for a in args)], timeout=120)

    def purge(self):
        """
        Drop every key — and, with AOF on, the file that would put them back.

        FLUSHALL only empties the dataset in memory. The append-only file on disk
        still holds every command that built it, so the next restart replays the
        whole thing and the "purged" cache comes back in full. BGREWRITEAOF is
        what rewrites that file from the live (now empty) dataset.

        Run against the PRIMARY, and the replicas follow it — flushing a replica
        would achieve nothing, because replication would refill it a second later.
        """
        container = self._local_container()
        if not container:
            return False, (f"No running {self.member_service(1)} container on this "
                           f"node. The panel can only reach containers on the "
                           f"master; flush it from the node it is actually on.")
        ok, out = self._redis_cli(container, "FLUSHALL")
        if not ok:
            return False, f"FLUSHALL failed: {out}"
        if not self.spec.get("appendonly"):
            return True, f"Every key in {self.name} was dropped."
        ok, out = self._redis_cli(container, "BGREWRITEAOF")
        if not ok:
            return False, (f"Every key was dropped, but rewriting the append-only "
                           f"file failed: {out}. The data is gone from memory and "
                           f"would come back on the next restart — run BGREWRITEAOF "
                           f"against this server by hand.")
        return True, (f"Every key in {self.name} was dropped and the append-only "
                      f"file was rewritten empty, so nothing returns on a restart.")

    # --- panel surface ------------------------------------------------------

    def tabs(self):
        tabs = [("overview", "Overview"), ("credentials", "Credentials"),
                ("map", "Map")]
        if self.managed and self.spec.get("backup_target"):
            tabs.append(("backups", "Backups"))
        return tabs + [("settings", "Settings"), ("logs", "Logs")]

    def actions(self):
        actions = super().actions()
        actions.pop("rollback", None)   # a database is not a thing to roll back casually
        actions["purge"] = base.action(
            self.purge, "Purge data",
            f"Drop EVERY key in {self.name}? "
            + ("Persistence is on, so the append-only file is rewritten empty too "
               "and nothing comes back on a restart. "
               if self.spec.get("appendonly") else "")
            + "The replicas follow the primary, so it goes everywhere. There is no "
              "undo.",
            tone="danger", when="running")
        actions["rotate"] = base.action(
            self.rotate_password, "Rotate password",
            "Generate a new password and restart every replica and sentinel with "
            "it? Anything still using the old one will stop being able to connect.",
            tone="danger", when="running")
        return actions

    def access(self):
        return {
            "target": ",".join(self.sentinel_hosts()),
            "note": ("These are the same hosts the connection string on the Credentials tab names, in the same order — this box leaves out the scheme, the password and the options because it is not behind a reveal, and the Overview tab must not put a password on screen. Connect with the string, not with this; this is here for a client that wants the hosts as a list rather than as a URL. "
                     "These are the SENTINELS, and they are what your client should "
                     f"be given, with master name `{self.stack}`. Reachable from any "
                     "component on the edge network. Do not put this behind the "
                     "tunnel — the tunnel speaks HTTP."),
        }

    def summary(self):
        # The live count alone. It used to read `1/4 replicas`, which invited the
        # question "why are three of them missing" — they were not missing, they
        # were slots nobody needed yet. There is no ceiling to be short of.
        live = sum(1 for i in range(1, self.slots + 1)
                   if (self.live_replicas(self.member_service(i)) or 0) >= 1)
        word = "server" if live == 1 else "servers"
        return f"redis:{self.spec.get('version', '')} · {live} {word}"
