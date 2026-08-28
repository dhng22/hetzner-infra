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

#: How many sentinels exist. Odd, always: an even quorum tolerates the same
#: number of failures as the odd one below it while costing another container.
SENTINEL_COUNT = 3


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
            Field("replica_pool", "Replica pool", "number", 3, required=True,
                  minimum=3, maximum=6, immutable=True,
                  help="How many servers BEYOND the one on the master this may ever "
                       "have. Fixed at creation because the sentinels are named in "
                       "the connection URL — raising it later is the one change "
                       "that does alter the string."),
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
            Field("external_port", "Published port", "port", None,
                  minimum=1024, maximum=65535,
                  help="Optional. Publishes the primary's replica on the master at "
                       "this port for an external client. The firewall still denies "
                       "it until you open it on this page, and it reaches ONE "
                       "server rather than following a failover."),
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
    def pool(self):
        return int(self.spec.get("replica_pool") or 3) + 1

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
            names = [self.member_service(i) for i in range(1, self.pool + 1)]
            names += [self.sentinel_service(i)
                      for i in range(1, SENTINEL_COUNT + 1)]
        else:
            names = [self.member_service(1)]
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
        The sentinel URL, written once.

        `redis+sentinel://` is what redis-py takes directly. Everything else in
        the Credentials tab is the same three facts spelled out — sentinel hosts,
        master name, password — because ioredis and Lettuce want them as fields
        rather than as a URL, and handing somebody only the scheme their client
        cannot parse is not help.
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

    def credentials(self, master_ip=""):
        port = self.spec.get("external_port")
        return {
            "password": self.password(),
            "internal_host": ",".join(self.sentinel_hosts()),
            "internal_port": "26379",
            "internal_url": self.connection_url(),
            "external_port": port,
            "external_host": master_ip,
            "external_url": self.connection_url(master_ip, port) if port else "",
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
                "and the same password for both the sentinels and the servers.",
            ],
        }

    # --- validation ---------------------------------------------------------

    def validate(self):
        problems = super().validate()
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
            "dataguard.member": str(index),
            "dataguard.pool": str(self.pool),
            "dataguard.set": self.stack,
            "dataguard.enabled": "true" if self.managed else "false",
            # `pool - 1`, not `pool`: slot 1 is the copy on the master and is
            # never handed out for growth, so a set that has grown off the master
            # can only ever fill the slots beyond it. Telling dataguard `pool`
            # promises a member it has nowhere to put.
            "dataguard.max_members": str(self.pool - 1),
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
        if index == 1 and self.spec.get("external_port"):
            service["ports"] = [{
                "target": 6379,
                "published": int(self.spec["external_port"]),
                "protocol": "tcp",
                "mode": "host",
            }]
        return service

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
                "replicas": 1,
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

    def render(self):
        s = self.spec
        self.ensure_password()

        members = range(1, self.pool + 1) if self.managed else range(1, 2)
        services = {self.member_key(i): self._member(i) for i in members}
        volumes = {f"{self.name}-{i}-data": {} for i in members}
        if self.managed:
            for i in range(1, SENTINEL_COUNT + 1):
                services[f"sentinel-{i}"] = self._sentinel(i)
                volumes[f"{self.name}-sentinel-{i}"] = {}
        networks = [base.EDGE_NETWORK]

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
            services["viewer"] = {
                "image": VIEWER_IMAGE,
                "environment": {
                    "RI_REDIS_HOST": self.member_service(1),
                    "RI_REDIS_PORT": "6379",
                    "RI_REDIS_PASSWORD": self.password(),
                    "RI_PROXY_PATH": f"/components/{self.name}/viewer",
                },
                "networks": [base.EDGE_NETWORK],
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
            "note": ("These are the SENTINELS, and they are what your client should "
                     f"be given, with master name `{self.stack}`. Reachable from any "
                     "component on the edge network. Do not put this behind the "
                     "tunnel — the tunnel speaks HTTP."),
        }

    def summary(self):
        live = sum(1 for i in range(1, self.pool + 1)
                   if (self.live_replicas(self.member_service(i)) or 0) >= 1)
        return f"redis:{self.spec.get('version', '')} · {live}/{self.pool} replicas"
