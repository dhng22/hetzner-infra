"""
A Redis instance that owns its own credentials and tells nobody.

Its password lives in that component's `secret.env` at 0600 and is injected
into exactly one service: this one. You set it when you create the component, or
leave that blank and get a generated one; either way you can change it later or
press Rotate for a fresh one. It is NOT written into any application's
environment, and no application is "linked" to it. The panel shows you a
connection URL; what you do with it is your business.

That is a deliberate reversal. The previous design put `REDIS_PASSWORD` in a
file shared with the application, fanned it out to three services, and made
rotation a thing that quietly broke clients. Here, rotating is one component
redeploying itself, and nothing else in the cluster notices.
"""

from urllib.parse import quote

from . import base, store
from .base import Component, Field, Secret

EXPORTER_IMAGE = "oliver006/redis_exporter:v1.66.0"


class RedisComponent(Component):
    TYPE = "redis"
    LABEL = "Redis"
    BLURB = "A password-protected Redis, with its own volume."
    CATEGORY = "Data"
    GROUP = "Database"

    @classmethod
    def fields(cls):
        return [
            Field("placement_mode", "Placement", "choice", "master",
                  choices=("master", "any"),
                  help="This has a VOLUME, and a volume lives on one machine. "
                       "`master` keeps it on the node that will not be deleted. "
                       "`any` lets Swarm place it anywhere — including a worker "
                       "the autoscaler deletes later, taking the data with it. "
                       "Only useful if you have moved the data yourself."),
            Field("placement_extra", "Extra constraints", "text", "",
                  placeholder="node.labels.disk == ssd",
                  help="Comma separated, added to whatever the mode implies."),
            Field("version", "Version", "choice", "7.4-alpine",
                  choices=("7.4-alpine", "7.2-alpine", "6.2-alpine"),
                  help="Changing this restarts the server. The volume survives."),
            Field("maxmemory_mb", "Max memory (MB)", "memory", 512, required=True,
                  minimum=16, maximum=65536,
                  help="Redis evicts above this. Keep it below the memory limit or the "
                       "container is OOM-killed before Redis ever starts evicting."),
            Field("maxmemory_policy", "Eviction policy", "choice", "allkeys-lru",
                  choices=("allkeys-lru", "allkeys-lfu", "volatile-lru",
                           "volatile-ttl", "noeviction"),
                  help="`noeviction` turns a full cache into write errors — correct for a "
                       "queue, wrong for a cache."),
            Field("appendonly", "Persist to disk (AOF)", "bool", True,
                  help="Survives a restart. Turn it off for a pure cache."),
            Field("external_port", "Published port", "port", None,
                  minimum=1024, maximum=65535,
                  help="Optional. Publishes Redis on the master at this port for an "
                       "external client. The firewall still denies it until you open it "
                       "on the component's page."),
            Field("exporter", "Metrics exporter", "bool", True,
                  help="Runs redis_exporter alongside it so Grafana has memory, hit rate "
                       "and connection counts."),
            Field("cpu_reservation", "CPU reserved", "cpu", 0.2, required=True,
                  minimum=0.01, maximum=32),
            Field("memory_reservation_mb", "Memory reserved (MB)", "memory", 640,
                  required=True, minimum=32, maximum=131072,
                  help="Must exceed max memory with room for Redis itself and its AOF "
                       "buffer, or eviction never gets the chance to run."),
        ]

    # --- credentials --------------------------------------------------------

    SECRETS = (
        Secret("REDIS_PASSWORD", "Password",
               help="Leave blank to generate a strong one. Set it yourself if you are "
                    "moving an existing database and its clients already know the "
                    "password."),
    )

    def password(self):
        return self.secret("REDIS_PASSWORD")

    def ensure_password(self):
        """Generate one if there is none. Idempotent, so a redeploy is safe."""
        if self.password():
            return False
        self.apply_secrets({})
        return True

    def set_password(self, value):
        return self.apply_secrets({"REDIS_PASSWORD": value})

    def rotate_password(self):
        """
        New password, then redeploy this stack — and only this stack.

        Nothing else holds a copy, so there is nothing else to update and
        nothing else to break. Clients using the old password will fail to
        authenticate until you give them the new one, which is what rotation
        means and is why the button says so.
        """
        self.rotate_secrets()
        ok, out = self.deploy()
        if not ok:
            return False, f"Password rotated, but the redeploy failed: {out}"
        return True, "Password rotated and the server restarted with it."

    def connection_url(self, host=None, port=None):
        # The password is percent-encoded because it goes into a URL and a user
        # may well choose one containing `@`, `:` or `/`. Without this,
        # `redis://default:p@ss@host:6379` parses as a different host and the
        # client's error message blames DNS.
        return (f"redis://default:{quote(self.password(), safe='')}@"
                f"{host or self.service}:{port or 6379}")

    # --- validation ---------------------------------------------------------

    def validate(self):
        problems = super().validate()
        maxmem = self.spec.get("maxmemory_mb") or 0
        reserved = self.spec.get("memory_reservation_mb") or 0
        if maxmem and reserved and maxmem >= reserved:
            problems.append(
                f"Max memory ({maxmem} MB) must be below the memory reservation "
                f"({reserved} MB) — Redis needs room above the dataset for itself "
                "and, with AOF on, for its rewrite buffer."
            )
        return problems

    # --- rendering ----------------------------------------------------------

    def services(self):
        names = [self.service]
        if self.spec.get("exporter"):
            names.append(f"{self.stack}_redis-exporter")
        return names

    def _server_command(self):
        s = self.spec
        args = [
            "redis-server",
            # $$ survives compose interpolation as a literal $, and `sh -c`
            # is what actually expands it. Without the shell, Redis would
            # enforce the eleven characters "$REDIS_PASSWORD" as its password
            # while every client was handed the real one.
            "--requirepass", '"$$REDIS_PASSWORD"',
            "--maxmemory", f"{s['maxmemory_mb']}mb",
            "--maxmemory-policy", s["maxmemory_policy"],
        ]
        if s.get("appendonly"):
            args += ["--appendonly", "yes", "--appendfsync", "everysec"]
        else:
            args += ["--appendonly", "no"]
        return base.shell_command(args)

    def _placement(self):
        """
        Where this may run. `master` unless someone has deliberately said
        otherwise, because the volume does not move with the task.
        """
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

        server = {
            "image": f"redis:{s['version']}",
            "command": self._server_command(),
            "environment": {"REDIS_PASSWORD": self.password()},
            "volumes": [f"{volume}:/data"],
            # edge only. Applications reach it by service DNS; vmagent does not
            # need to, because the exporter is what carries the metrics.
            "networks": [base.EDGE_NETWORK],
            "logging": self.loki_logging(),
            "stop_grace_period": "30s",
            "deploy": {
                "replicas": 1,
                "labels": dict(self.base_labels()),
                # Stateful: it has a volume, so by default it stays on the
                # master. It carries no infra.workload label, which is exactly
                # what keeps the autoscaler from ever moving it onto a worker
                # that is later deleted.
                "placement": self._placement(),
                "restart_policy": {"condition": "any", "delay": "5s"},
                "resources": self.resources(),
            },
        }

        if s.get("external_port"):
            server["ports"] = [{
                "target": 6379,
                "published": int(s["external_port"]),
                "protocol": "tcp",
                # host mode: published on the master's interface only, where
                # ufw still denies it until the port is opened deliberately.
                "mode": "host",
            }]

        services = {self.service_key(): server}

        if s.get("exporter"):
            services["redis-exporter"] = {
                "image": EXPORTER_IMAGE,
                "environment": {
                    "REDIS_ADDR": f"redis://{self.service}:6379",
                    "REDIS_PASSWORD": self.password(),
                },
                "networks": [base.EDGE_NETWORK, base.MONITORING_NETWORK],
                "logging": self.loki_logging(),
                "deploy": {
                    "replicas": 1,
                    # The exporter follows the server it scrapes; a stateless
                    # sidecar on a different node would just be scraping over
                    # the network for no reason.
                    "placement": self._placement(),
                    "labels": dict(self.base_labels(), **{
                        "prometheus.scrape": "true",
                        "prometheus.port": "9121",
                        "prometheus.path": "/metrics",
                    }),
                    "restart_policy": {"condition": "any", "delay": "10s"},
                    "resources": {"reservations": {"cpus": "0.05", "memory": "32M"}},
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

    def remove(self):
        """
        Removing a Redis leaves its volume behind, on purpose.

        `docker stack rm` does not delete volumes and neither do we. A mistyped
        delete should cost you a redeploy, not your data. The volume is named
        `<component>-data`; remove it by hand once you are sure.
        """
        ok, out = super().remove()
        if ok:
            out = (f"{out}\nThe volume {self.name}-data was kept. Delete it with "
                   f"`docker volume rm {self.name}-data` once you are sure.")
        return ok, out

    # --- panel surface ------------------------------------------------------

    def tabs(self):
        return [("overview", "Overview"), ("credentials", "Credentials"),
                ("settings", "Settings"), ("logs", "Logs")]

    def actions(self):
        actions = super().actions()
        actions.pop("rollback", None)   # a database is not a thing to roll back casually
        actions["rotate"] = (
            self.rotate_password, "Rotate password",
            "Generate a new password and restart the server with it? Anything "
            "still using the old one will stop being able to authenticate.",
        )
        return actions

    def access(self):
        return {
            "target": f"{self.service}:6379",
            "note": ("Reachable from any component on the edge network by this name. "
                     "Do not put it behind the tunnel — the tunnel speaks HTTP."),
        }

    def summary(self):
        return f"redis:{self.spec.get('version', '')}"
