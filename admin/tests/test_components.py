"""
Tests for the component library.

    python3 -m unittest discover -s admin/tests -v

This is the one part of the platform where a bug is silent and expensive: a
rendered stack that looks plausible and is subtly wrong gets deployed, and the
first symptom is a database that will not authenticate. So the assertions here
are mostly about the render — what ends up in the YAML, and what must never.

No cluster, no docker, no network. INFRA_DIR is redirected at a temp directory,
and the live-state reads (which shell out to `docker`) are stubbed, so this runs
anywhere.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ComponentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="components-test-")
        os.environ["INFRA_DIR"] = self.tmp
        os.environ["MASTER_PRIVATE_IP"] = "10.0.0.9"
        for module in [m for m in list(sys.modules) if m.startswith("components")]:
            del sys.modules[module]
        import components
        self.components = components
        # Nothing is deployed in a test, so every live read is "no such service".
        components.base.Component.live_replicas = lambda self, service=None: None
        components.base.Component.live_image = lambda self, service=None: None
        components.base.Component.live_worker_pinned = lambda self, service=None: False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_app(self, name="api", **spec):
        base = {"image": "ghcr.io/you/app:sha-abc1234", "port": 8080}
        component, problems = self.components.create("app", name, {**base, **spec})
        self.assertEqual(problems, [], f"unexpected problems: {problems}")
        return component

    def make_redis(self, name="cache", **spec):
        component, problems = self.components.create("redis", name, spec)
        self.assertEqual(problems, [], f"unexpected problems: {problems}")
        return component

    # --- names ------------------------------------------------------------

    def test_name_rules(self):
        for bad in ("Api", "a", "1api", "api_1", "api.1", "x" * 33, ""):
            with self.assertRaises(self.components.ComponentError, msg=bad):
                self.components.store.check_name(bad)
        for reserved in ("monitoring", "ingress", "admin"):
            with self.assertRaises(self.components.ComponentError):
                self.components.store.check_name(reserved)
        for good in ("api", "api-staging", "my-cache-2"):
            self.assertEqual(self.components.store.check_name(good), good)

    def test_duplicate_is_refused(self):
        self.make_app()
        with self.assertRaises(self.components.ComponentError):
            self.components.create("app", "api", {"image": "x/y:v1", "port": 80})

    # --- validation -------------------------------------------------------

    def test_floating_tags_are_refused(self):
        for bad in ("ghcr.io/you/app:latest", "ghcr.io/you/app", "nginx"):
            _, problems = self.components.create("app", "bad", {"image": bad, "port": 80})
            self.assertTrue(problems, f"{bad} should have been refused")

    def test_digest_pin_is_accepted(self):
        _, problems = self.components.create(
            "app", "pinned",
            {"image": "ghcr.io/you/app:v1@sha256:" + "a" * 64, "port": 80})
        self.assertEqual(problems, [])

    def test_reservation_above_limit_is_refused(self):
        _, problems = self.components.create("app", "fat", {
            "image": "x/y:v1", "port": 80,
            "cpu_reservation": 2.0, "cpu_limit": 1.0})
        self.assertIn("CPU reserved is above the CPU limit — the task could never be placed.",
                      problems)

    def test_crossed_autoscale_thresholds_are_refused(self):
        _, problems = self.components.create("app", "osc", {
            "image": "x/y:v1", "port": 80, "autoscale": "true",
            "up_p95_ratio": "0.4", "down_p95_ratio": "0.8"})
        self.assertTrue(any("oscillates" in p for p in problems))

    def test_redis_maxmemory_must_leave_headroom(self):
        _, problems = self.components.create("redis", "big", {
            "maxmemory_mb": 1024, "memory_reservation_mb": 1024})
        self.assertTrue(any("must be below the memory reservation" in p for p in problems))

    def test_reservations_are_mandatory_at_render(self):
        component = self.make_app()
        component.spec["memory_reservation_mb"] = None
        with self.assertRaises(self.components.ComponentError):
            component.render()

    # --- app rendering ----------------------------------------------------

    def test_app_render_shape(self):
        rendered = self.make_app().render()
        service = rendered["services"]["app"]
        self.assertEqual(rendered["services"].keys(), {"app"})
        self.assertEqual(service["networks"], ["edge", "monitoring"])
        self.assertEqual(rendered["networks"]["edge"], {"external": True})
        self.assertEqual(service["deploy"]["resources"]["reservations"],
                         {"cpus": "0.5", "memory": "384M"})
        self.assertEqual(service["healthcheck"]["test"][-1],
                         "http://localhost:8080/health")
        # start-first is what makes a deploy gapless; losing it is silent.
        self.assertEqual(service["deploy"]["update_config"]["order"], "start-first")
        self.assertEqual(service["deploy"]["update_config"]["failure_action"], "rollback")

    def test_app_is_never_pinned_by_the_renderer(self):
        """
        The autoscaler owns node.role. Emitting it from the spec would slam
        placement back on every deploy, at whatever moment CI shipped.
        """
        placement = self.make_app().render()["services"]["app"]["deploy"]["placement"]
        self.assertNotIn("constraints", placement)
        self.assertEqual(placement["preferences"], [{"spread": "node.id"}])

    def test_live_pin_is_preserved(self):
        component = self.make_app()
        component.live_worker_pinned = lambda service=None: True
        placement = component.render()["services"]["app"]["deploy"]["placement"]
        self.assertEqual(placement["constraints"], ["node.role == worker"])

    def test_live_replicas_and_image_win_over_the_spec(self):
        component = self.make_app(replicas=2)
        component.live_replicas = lambda service=None: 7
        component.live_image = lambda service=None: "ghcr.io/you/app:sha-newer"
        service = component.render()["services"]["app"]
        self.assertEqual(service["deploy"]["replicas"], 7)
        self.assertEqual(service["image"], "ghcr.io/you/app:sha-newer")

    def test_fixed_app_is_still_discoverable_and_pinnable(self):
        labels = self.make_app().render()["services"]["app"]["deploy"]["labels"]
        self.assertEqual(labels["infra.workload"], "app")
        self.assertEqual(labels["autoscale.enabled"], "false")

    def test_autoscale_policy_reaches_the_labels(self):
        component = self.make_app(autoscale="true", min_replicas=3,
                                  max_replicas=9, slo_p95_ms=250)
        labels = component.render()["services"]["app"]["deploy"]["labels"]
        self.assertEqual(labels["autoscale.enabled"], "true")
        self.assertEqual(labels["autoscale.min_replicas"], "3")
        self.assertEqual(labels["autoscale.max_replicas"], "9")
        self.assertEqual(labels["autoscale.slo_p95_ms"], "250")
        self.assertEqual(labels["prometheus.port"], "8080")

    def test_no_metrics_path_means_no_scrape_labels(self):
        component = self.make_app()
        component.spec["metrics_path"] = None
        labels = component.render()["services"]["app"]["deploy"]["labels"]
        self.assertNotIn("prometheus.scrape", labels)

    def test_environment_file_is_the_whole_environment(self):
        """The platform injects nothing of its own into an application."""
        component = self.make_app()
        self.assertNotIn("environment", component.render()["services"]["app"])
        self.components.store.write_env("api", [{"key": "LOG_LEVEL", "value": "INFO"}])
        service = self.components.load("api").render()["services"]["app"]
        self.assertEqual(service["environment"], {"LOG_LEVEL": "INFO"})

    # --- redis rendering --------------------------------------------------

    def test_redis_password_is_generated_and_kept_out_of_the_spec(self):
        component = self.make_redis()
        password = component.password()
        self.assertEqual(len(password), 48)
        self.assertNotIn(password, str(component.as_dict()))
        mode = os.stat(self.components.store.path_for("cache", "secret.env")).st_mode
        self.assertEqual(mode & 0o777, 0o600)

    def test_redis_password_expands_at_runtime(self):
        """
        The command must go through a shell, and the $ must survive compose.

        Without `sh -c`, compose passes `--requirepass "$REDIS_PASSWORD"` to
        redis-server as a literal argument, so the server enforces the string
        while every client is handed the real password. That was a live defect
        in the stack file this replaces.
        """
        command = self.make_redis().render()["services"]["redis"]["command"]
        self.assertEqual(command[:2], ["sh", "-c"])
        self.assertIn('--requirepass "$$REDIS_PASSWORD"', command[2])
        self.assertTrue(command[2].startswith("exec "))

    def test_redis_is_manager_pinned_and_not_app_workload(self):
        """Stateful: it has a volume, so it must never move to a worker."""
        deploy = self.make_redis().render()["services"]["redis"]["deploy"]
        self.assertEqual(deploy["placement"]["constraints"], ["node.role == manager"])
        self.assertNotIn("infra.workload", deploy["labels"])

    def test_redis_exporter_is_optional_and_scraped(self):
        with_exporter = self.make_redis("c1").render()
        self.assertIn("redis-exporter", with_exporter["services"])
        labels = with_exporter["services"]["redis-exporter"]["deploy"]["labels"]
        self.assertEqual(labels["prometheus.port"], "9121")
        without = self.make_redis("c2", exporter="false").render()
        self.assertNotIn("redis-exporter", without["services"])
        self.assertEqual(without["services"]["redis"]["networks"], ["edge"])

    def test_redis_published_port_is_host_mode(self):
        rendered = self.make_redis(external_port=46379).render()
        self.assertEqual(rendered["services"]["redis"]["ports"], [{
            "target": 6379, "published": 46379, "protocol": "tcp", "mode": "host"}])

    def test_rotation_changes_the_password(self):
        component = self.make_redis()
        before = component.password()
        component.set_password("f" * 48)
        self.assertNotEqual(before, self.components.load("cache").password())

    # --- storage ----------------------------------------------------------

    def test_secrets_never_land_in_the_spec_file(self):
        self.make_redis()
        with open(self.components.store.path_for("cache", "component.json")) as fh:
            self.assertNotIn("REDIS_PASSWORD", fh.read())

    def test_components_are_isolated_from_each_other(self):
        """The bug this whole model exists to prevent: one file, two writers."""
        self.make_app("api")
        self.make_redis("cache")
        self.components.store.write_env("api", [{"key": "A", "value": "1"}])
        self.assertEqual(self.components.store.env_map("cache"), {})
        self.assertNotEqual(
            self.components.store.path_for("api", "env"),
            self.components.store.path_for("cache", "env"))

    def test_bulk_env_round_trip(self):
        pairs, problems = self.components.store.parse_bulk(
            "# note\n\nexport A=1\nB=has spaces\nC=\n")
        self.assertEqual(problems, [])
        self.assertEqual(pairs, [{"key": "A", "value": "1"},
                                 {"key": "B", "value": "has spaces"},
                                 {"key": "C", "value": ""}])
        self.assertEqual(self.components.store.parse_bulk("A=1\noops\n")[1],
                         ["Line 2 is not KEY=VALUE."])

    def test_yaml_is_parseable_and_quotes_survive(self):
        import yaml
        self.make_app()
        # A colon, a hash, quotes and a dollar — every character that breaks a
        # hand-rolled YAML emitter, and the reason this uses a real one.
        self.components.store.write_env(
            "api", [{"key": "MOTD", "value": 'a: b #c "d" $e'}])
        rendered = yaml.safe_load(self.components.load("api").stack_yaml())
        self.assertEqual(rendered["services"]["app"]["environment"]["MOTD"],
                         'a: b #c "d" $e')

    def test_load_survives_one_broken_component(self):
        self.make_app("good")
        os.makedirs(self.components.store.dir_for("broken"), exist_ok=True)
        with open(self.components.store.path_for("broken", "component.json"), "w") as fh:
            fh.write("{not json")
        found, problems = self.components.all_components()
        self.assertEqual([c.name for c in found], ["good"])
        self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
