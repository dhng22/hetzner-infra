"""
A MongoDB instance that owns its own credentials and tells nobody.

The same bargain as Redis, and for the same reasons: the password lives in this
component's `secret.env` at 0600, is injected into this service and no other,
and rotating it redeploys one stack while nothing else in the cluster notices.
Nothing is "linked" — the panel hands you a connection string and what you do
with it is your business.

The one thing here that is genuinely Mongo's and not a copy of Redis is the
WiredTiger cache. It sizes itself from the HOST's memory, not the container's
limit, so a 512 MB service on a 8 GB master helps itself to 3.5 GB and is
OOM-killed by the kernel long before Mongo believes anything is wrong. Setting
it explicitly is not tuning, it is the difference between running and not.
"""

from urllib.parse import quote

from . import base
from .base import Component, Field, Secret

EXPORTER_IMAGE = "percona/mongodb_exporter:0.43.1"


class MongoComponent(Component):
    TYPE = "mongo"
    LABEL = "MongoDB"
    BLURB = "An authenticated MongoDB, with its own volume."
    CATEGORY = "Data"
    GROUP = "Database"
    KEEPS_VOLUME = True

    @classmethod
    def fields(cls):
        return [
            Field("placement_mode", "Placement", "choice", "master",
                  choices=("master", "any"),
                  help="This has a VOLUME, and a volume lives on one machine. "
                       "`master` keeps it on the node that will not be deleted. "
                       "`any` lets Swarm place it anywhere — including a worker "
                       "the autoscaler deletes later, taking the data with it."),
            Field("placement_extra", "Extra constraints", "text", "",
                  placeholder="node.labels.disk == ssd",
                  help="Comma separated, added to whatever the mode implies."),
            Field("version", "Version", "choice", "7.0",
                  choices=("7.0", "6.0", "5.0"),
                  help="Changing this restarts the server. The volume survives, but "
                       "Mongo only upgrades one major version at a time — go 5 to 6 "
                       "to 7, never 5 to 7."),
            Field("username", "Root username", "text", "root", required=True,
                  help="The initial administrative user. It is created on the FIRST "
                       "start only, from the empty volume; changing it later does "
                       "nothing until the data directory is empty again."),
            Field("cache_mb", "WiredTiger cache (MB)", "memory", 256, required=True,
                  minimum=64, maximum=131072,
                  help="Mongo's own data cache. Left unset it is sized from the HOST's "
                       "RAM and ignores this container's limit entirely, which is how "
                       "a small Mongo gets OOM-killed on a large machine."),
            Field("external_port", "Published port", "port", None,
                  minimum=1024, maximum=65535,
                  help="Optional. Publishes Mongo on the master at this port for an "
                       "external client. The firewall still denies it until you open "
                       "it on the component's page."),
            Field("exporter", "Metrics exporter", "bool", True,
                  help="Runs mongodb_exporter alongside it so Grafana has connections, "
                       "operation counters and replication lag."),
            Field("cpu_reservation", "CPU reserved", "cpu", 0.3, required=True,
                  minimum=0.01, maximum=32),
            Field("memory_reservation_mb", "Memory reserved (MB)", "memory", 768,
                  required=True, minimum=64, maximum=131072,
                  help="Must exceed the WiredTiger cache with room for connections, "
                       "indexes being built and the server itself."),
        ]

    # --- credentials --------------------------------------------------------

    SECRETS = (
        Secret("MONGO_PASSWORD", "Password",
               help="Leave blank to generate a strong one. Set it yourself if you are "
                    "moving an existing database and its clients already know the "
                    "password."),
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
        New password, then redeploy — and, unlike Redis, one more step.

        `MONGO_INITDB_ROOT_PASSWORD` is read on the FIRST start only, off an
        empty data directory. Redeploying with a new value changes nothing for
        an existing volume, so the change has to be made in the running server
        with `db.changeUserPassword` before the environment is updated to match.
        Doing only the redeploy is what a naive copy of the Redis button would
        do, and it would report success while every client kept working on the
        old password.
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
        return True, ("Password rotated in the running server and saved. Anything "
                      "still using the old one can no longer authenticate.")

    def _change_password(self, new):
        container = self._local_container()
        if not container:
            return False, (f"No running {self.service} container on this node. The "
                           f"panel can only reach containers on the master, and a "
                           f"Mongo password has to be changed in the server itself — "
                           f"the environment variable is read on first start only.")
        # Both passwords travel in the container's environment, never in argv:
        # the master's process table is readable by anything else on the box.
        script = ('db.getSiblingDB("admin").changeUserPassword('
                  'process.env.MONGO_INITDB_ROOT_USERNAME, process.env.NEW_PASSWORD)')
        ok, out = base.run([
            "docker", "exec", "-e", f"NEW_PASSWORD={new}", container,
            "sh", "-c",
            'exec mongosh --quiet -u "$MONGO_INITDB_ROOT_USERNAME" '
            '-p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin '
            f'--eval {base.quote(script)}'], timeout=120)
        if not ok:
            return False, f"Could not change the password in the server: {out}"
        return True, ""

    def _local_container(self):
        out = base.docker_out([
            "ps", "--filter", f"label=com.docker.swarm.service.name={self.service}",
            "--filter", "status=running", "--format", "{{.ID}}"])
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        return lines[0] if lines else ""

    def connection_url(self, host=None, port=None):
        # Percent-encoded for the same reason as Redis: a chosen password
        # containing `@`, `:` or `/` otherwise re-parses as a different host and
        # the driver's error blames DNS.
        user = quote(self.spec.get("username") or "root", safe="")
        return (f"mongodb://{user}:{quote(self.password(), safe='')}@"
                f"{host or self.service}:{port or 27017}/?authSource=admin")

    def credentials(self, master_ip=""):
        port = self.spec.get("external_port")
        return {
            "password": self.password(),
            "internal_host": self.service,
            "internal_port": "27017",
            "internal_url": self.connection_url(),
            "external_port": port,
            "external_host": master_ip,
            "external_url": self.connection_url(master_ip, port) if port else "",
        }

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
        return problems

    # --- rendering ----------------------------------------------------------

    def services(self):
        names = [self.service]
        if self.spec.get("exporter"):
            names.append(f"{self.stack}_mongo-exporter")
        return names

    def _placement(self):
        constraints = []
        if (self.spec.get("placement_mode") or "master") == "master":
            constraints.append("node.role == manager")
        for extra in (self.spec.get("placement_extra") or "").split(","):
            extra = extra.strip()
            if extra:
                constraints.append(extra)
        return {"constraints": constraints} if constraints else {}

    def render(self):
        s = self.spec
        self.ensure_password()
        volume = f"{self.name}-data"
        # Mongo takes gigabytes as a float and refuses anything below 0.25.
        cache_gb = max(round((s["cache_mb"] or 256) / 1024, 3), 0.25)

        environment = {
            "MONGO_INITDB_ROOT_USERNAME": s.get("username") or "root",
            "MONGO_INITDB_ROOT_PASSWORD": self.password(),
        }

        server = {
            "image": f"mongo:{s['version']}",
            "command": ["mongod", "--bind_ip_all",
                        "--wiredTigerCacheSizeGB", str(cache_gb)],
            "environment": environment,
            "volumes": [f"{volume}:/data/db"],
            "networks": [base.EDGE_NETWORK],
            "logging": self.loki_logging(),
            "stop_grace_period": "60s",
            "deploy": {
                "replicas": 1,
                "labels": dict(self.base_labels()),
                # No infra.workload label, and manager-pinned by default: this
                # has a volume, and the autoscaler must never move it onto a
                # worker it is about to delete.
                "placement": self._placement(),
                "restart_policy": {"condition": "any", "delay": "5s"},
                "resources": self.resources(),
            },
        }

        if s.get("external_port"):
            server["ports"] = [{
                "target": 27017,
                "published": int(s["external_port"]),
                "protocol": "tcp",
                # host mode: the master's interface only, where ufw still denies
                # it until the port is opened deliberately.
                "mode": "host",
            }]

        services = {self.service_key(): server}

        if s.get("exporter"):
            services["mongo-exporter"] = {
                "image": EXPORTER_IMAGE,
                "command": ["--collect-all", "--compatible-mode"],
                "environment": {"MONGODB_URI": self.connection_url()},
                "networks": [base.EDGE_NETWORK, base.MONITORING_NETWORK],
                "logging": self.loki_logging(),
                "deploy": {
                    "replicas": 1,
                    # Follows the server it scrapes; a stateless sidecar on
                    # another node would just scrape over the network.
                    "placement": self._placement(),
                    "labels": dict(self.base_labels(), **{
                        "prometheus.scrape": "true",
                        "prometheus.port": "9216",
                        "prometheus.path": "/metrics",
                    }),
                    "restart_policy": {"condition": "any", "delay": "10s"},
                    "resources": {"reservations": {"cpus": "0.05", "memory": "64M"}},
                },
            }

        networks = [base.EDGE_NETWORK]
        if s.get("exporter"):
            networks.append(base.MONITORING_NETWORK)

        return {
            "version": "3.8",
            "services": services,
            "networks": base.compose_networks(*networks),
            "volumes": {volume: {}},
        }

    # --- panel surface ------------------------------------------------------

    def tabs(self):
        return [("overview", "Overview"), ("credentials", "Credentials"),
                ("settings", "Settings"), ("logs", "Logs")]

    def actions(self):
        actions = super().actions()
        actions.pop("rollback", None)   # a database is not a thing to roll back casually
        actions["rotate"] = base.action(
            self.rotate_password, "Rotate password",
            "Generate a new password, change it in the running server and restart "
            "with it? Anything still using the old one will stop being able to "
            "authenticate.",
            tone="danger", when="running")
        return actions

    def access(self):
        return {
            "target": f"{self.service}:27017",
            "note": ("Reachable from any component on the edge network by this name. "
                     "Do not put it behind the tunnel — the tunnel speaks HTTP."),
        }

    def summary(self):
        return f"mongo:{self.spec.get('version', '')}"
