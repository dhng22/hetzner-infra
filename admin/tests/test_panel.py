"""
Route tests for the panel.

    python3 -m unittest discover -s admin/tests -v

Runs the real Flask app against a temp INFRA_DIR and the preview fixtures in
place of Docker, so every route is exercised end to end — auth, CSRF, the
generic component pages, and the writes. The point is not coverage for its own
sake: the panel is a root console, so "does this route refuse what it should
refuse" is a correctness property worth pinning.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class PanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="panel-test-")
        os.environ.update(
            PREVIEW="", INFRA_DIR=cls.tmp, STATE_DIR=os.path.join(cls.tmp, "state"),
            ADMIN_PASSWORD="dev", ADMIN_USER="admin", SESSION_SECRET="test-secret",
            APP_NAME="aichat", ROOT_DOMAIN="acme.dev", COOKIE_SECURE="0",
            MASTER_PRIVATE_IP="10.0.0.2",
        )
        for module in [m for m in list(sys.modules)
                       if m.split(".")[0] in ("components", "app", "swarm", "fixtures",
                                              "shape", "state", "envstore", "auth")]:
            del sys.modules[module]

        import fixtures
        sys.modules["swarm"] = fixtures      # stand in for the docker socket
        import app as panel
        import components

        cls.panel = panel
        cls.components = components
        # Nothing is really deployed; record what would have been.
        cls.deploys = []
        components.base.Component.deploy = lambda self: (
            cls.deploys.append(self.name) or (True, f"{self.name}: converged"))
        # remove() really removes the files — only the `docker stack rm` is
        # stubbed out, because that is the only part that needs a cluster.
        components.base.Component.remove = lambda self: (
            components.store.delete_dir(self.name) or (True, f"removed {self.name}"))
        components.base.Component.live_replicas = lambda self, service=None: None
        components.base.Component.live_image = lambda self, service=None: None
        components.base.Component.live_worker_pinned = lambda self, service=None: False

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.client = self.panel.app.test_client()
        self.deploys.clear()

    def login(self):
        self.client.post("/login", data={"username": "admin", "password": "dev"})
        page = self.client.get("/components/new?type=app").get_data(as_text=True)
        return page.split('name="csrf" value="')[1].split('"')[0]

    def create_app(self, name="api", **extra):
        csrf = self.login()
        form = {"csrf": csrf, "type": "app", "name": name,
                "image": "ghcr.io/you/app:sha-abc1234", "port": "8080",
                "cpu_reservation": "0.5", "memory_reservation_mb": "384",
                "cpu_limit": "1.0", "memory_limit_mb": "768", "replicas": "2",
                "start_period": "60", "stop_grace": "30",
                "min_replicas": "2", "max_replicas": "8", "slo_p95_ms": "500",
                "up_p95_ratio": "0.8", "down_p95_ratio": "0.4",
                "up_cpu_pct": "70", "down_cpu_pct": "30",
                "sustain_up_seconds": "90", "sustain_down_seconds": "900",
                "up_factor": "0.5", "cooldown_seconds": "60", "priority": "100",
                "health_path": "/health", "metrics_path": "/metrics",
                # The form's checkbox is checked by default, so a browser posts this.
                "deploy_now": "1"}
        form.update(extra)
        return csrf, self.client.post("/components", data=form, follow_redirects=True)

    # --- auth ---------------------------------------------------------------

    def test_every_page_requires_a_session(self):
        for path in ("/", "/components", "/cluster", "/autoscaler", "/alerts",
                     "/settings", "/api/topology", "/components/new"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"], path)

    def test_healthz_is_open(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_writes_require_csrf(self):
        self.login()
        for path in ("/components", "/settings", "/registry", "/cluster/stack"):
            r = self.client.post(path, data={"name": "x"})
            self.assertEqual(r.status_code, 400, path)

    def test_logout_requires_csrf(self):
        """A third-party page should not be able to sign you out."""
        self.login()
        self.assertEqual(self.client.post("/logout").status_code, 400)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_login_rejects_an_open_redirect(self):
        r = self.client.post("/login?next=//evil.example",
                             data={"username": "admin", "password": "dev"})
        self.assertEqual(r.headers["Location"], "/")

    # --- component lifecycle ------------------------------------------------

    def test_create_shows_the_component(self):
        _, r = self.create_app("api")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("api", body)
        self.assertIn("api", self.deploys)
        self.assertTrue(self.components.exists("api"))

    def test_create_refuses_a_bad_image_without_writing(self):
        csrf, r = self.create_app("nope", image="ghcr.io/you/app:latest")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Refusing `latest`", r.get_data(as_text=True))
        self.assertFalse(self.components.exists("nope"))
        # And what was typed comes back, rather than an empty form.
        self.assertIn("ghcr.io/you/app:latest", r.get_data(as_text=True))

    def test_unknown_component_is_404_not_500(self):
        self.login()
        self.assertEqual(self.client.get("/components/ghost").status_code, 404)
        self.assertEqual(self.client.post(
            "/components/ghost/action", data={"csrf": "x"}).status_code, 400)

    def test_tabs_come_from_the_component(self):
        self.create_app("api2")
        body = self.client.get("/components/api2").get_data(as_text=True)
        for label in ("Environment", "Deployments", "Settings", "Logs"):
            self.assertIn(label, body)
        # An application has no credentials tab; a database has no environment.
        csrf = self.login()
        self.client.post("/components", data={"csrf": csrf, "type": "redis",
                                              "name": "cache", "maxmemory_mb": "512",
                                              "memory_reservation_mb": "640",
                                              "cpu_reservation": "0.2",
                                              "version": "7.4-alpine",
                                              "maxmemory_policy": "allkeys-lru"})
        redis_body = self.client.get("/components/cache").get_data(as_text=True)
        self.assertIn("Credentials", redis_body)
        self.assertNotIn('data-panel="environment"', redis_body)

    def test_history_route_does_not_500(self):
        """swarm.history() did not exist while the detail page called it."""
        self.create_app("api3")
        r = self.client.get("/components/api3?tab=deployments")
        self.assertEqual(r.status_code, 200)
        self.assertIn("History", r.get_data(as_text=True))

    def test_environment_editor_saves_both_encodings(self):
        csrf, _ = self.create_app("api4")
        self.client.post("/components/api4/env", follow_redirects=True,
                         data={"csrf": csrf, "bulk": "A=1\nexport B=2\n# note\n"})
        self.assertEqual(self.components.store.env_map("api4"), {"A": "1", "B": "2"})

        from werkzeug.datastructures import MultiDict
        self.client.post("/components/api4/env", follow_redirects=True,
                         data=MultiDict([("csrf", csrf), ("key", "ONLY"), ("value", "yes")]))
        self.assertEqual(self.components.store.env_map("api4"), {"ONLY": "yes"})

    def test_a_bad_env_paste_writes_nothing(self):
        csrf, _ = self.create_app("api5")
        self.components.store.write_env("api5", [{"key": "KEEP", "value": "me"}])
        r = self.client.post("/components/api5/env", follow_redirects=True,
                             data={"csrf": csrf, "bulk": "KEEP=me\nnot an assignment\n"})
        self.assertIn("Line 2 is not KEY=VALUE", r.get_data(as_text=True))
        self.assertEqual(self.components.store.env_map("api5"), {"KEEP": "me"})

    def test_one_component_cannot_write_another(self):
        """The bug this model exists to prevent, asserted at the route level."""
        csrf, _ = self.create_app("one")
        self.create_app("two")
        self.components.store.write_env("two", [{"key": "TWO", "value": "kept"}])
        self.client.post("/components/one/env", follow_redirects=True,
                         data={"csrf": csrf, "bulk": "ONE=set\n"})
        self.assertEqual(self.components.store.env_map("two"), {"TWO": "kept"})

    def test_delete_needs_the_name_typed(self):
        csrf, _ = self.create_app("api6")
        self.client.post("/components/api6/delete", data={"csrf": csrf, "confirm": "wrong"},
                         follow_redirects=True)
        self.assertTrue(self.components.exists("api6"))
        self.client.post("/components/api6/delete", data={"csrf": csrf, "confirm": "api6"},
                         follow_redirects=True)
        self.assertFalse(self.components.exists("api6"))

    def test_credentials_can_be_set_and_regenerated(self):
        csrf = self.login()
        self.client.post("/components", data={"csrf": csrf, "type": "redis", "name": "db2",
                                              "maxmemory_mb": "512",
                                              "memory_reservation_mb": "640",
                                              "cpu_reservation": "0.2",
                                              "version": "7.4-alpine",
                                              "maxmemory_policy": "allkeys-lru"})
        generated = self.components.load("db2").password()
        self.assertEqual(len(generated), 48)

        self.client.post("/components/db2/credentials", follow_redirects=True,
                         data={"csrf": csrf, "REDIS_PASSWORD": "a-chosen-password"})
        self.assertEqual(self.components.load("db2").password(), "a-chosen-password")

        self.client.post("/components/db2/credentials", follow_redirects=True,
                         data={"csrf": csrf, "regenerate": "1"})
        rotated = self.components.load("db2").password()
        self.assertNotEqual(rotated, "a-chosen-password")
        self.assertEqual(len(rotated), 48)

    def test_a_weak_password_is_refused_by_the_route(self):
        csrf = self.login()
        self.client.post("/components", data={"csrf": csrf, "type": "redis", "name": "db3",
                                              "maxmemory_mb": "512",
                                              "memory_reservation_mb": "640",
                                              "cpu_reservation": "0.2",
                                              "version": "7.4-alpine",
                                              "maxmemory_policy": "allkeys-lru"})
        before = self.components.load("db3").password()
        r = self.client.post("/components/db3/credentials", follow_redirects=True,
                             data={"csrf": csrf, "REDIS_PASSWORD": "abc"})
        self.assertIn("at least", r.get_data(as_text=True))
        self.assertEqual(self.components.load("db3").password(), before)

    def test_an_application_has_no_credentials_route(self):
        csrf, _ = self.create_app("plain")
        r = self.client.post("/components/plain/credentials",
                             data={"csrf": csrf, "X": "y"})
        self.assertEqual(r.status_code, 404)

    def test_the_new_menu_offers_one_entry_per_group(self):
        self.login()
        body = self.client.get("/components").get_data(as_text=True)
        self.assertIn("+ New", body)
        self.assertIn(">Application<", body)
        self.assertIn(">Database<", body)
        # The old per-type create cards are gone from the grid.
        self.assertNotIn("card-new", body)

    # --- deploy webhook -----------------------------------------------------

    def test_webhook_rejects_a_bad_token(self):
        self.create_app("hooked")
        r = self.client.post("/hooks/deploy/hooked",
                             headers={"X-Deploy-Token": "wrong"},
                             json={"image": "ghcr.io/you/app:sha-1"})
        self.assertEqual(r.status_code, 401)

    def test_webhook_rejects_a_floating_tag(self):
        self.create_app("hooked2")
        import state
        token = state.token_for("hooked2")
        r = self.client.post("/hooks/deploy/hooked2",
                             headers={"X-Deploy-Token": token},
                             json={"image": "ghcr.io/you/app:prod-latest"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("moving tag", r.get_json()["error"])

    def test_webhook_deploys_with_a_good_token(self):
        self.create_app("hooked3")
        import state
        token = state.token_for("hooked3")
        r = self.client.post("/hooks/deploy/hooked3",
                             headers={"X-Deploy-Token": token},
                             json={"image": "ghcr.io/you/app:sha-abc1234"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["service"], "hooked3_app")

    def test_webhook_on_a_database_is_404(self):
        csrf = self.login()
        self.client.post("/components", data={"csrf": csrf, "type": "redis", "name": "db",
                                              "maxmemory_mb": "512",
                                              "memory_reservation_mb": "640",
                                              "cpu_reservation": "0.2",
                                              "version": "7.4-alpine",
                                              "maxmemory_policy": "allkeys-lru"})
        r = self.client.post("/hooks/deploy/db", json={"image": "redis:7.4-alpine"})
        self.assertEqual(r.status_code, 404)

    # --- infrastructure -----------------------------------------------------

    def test_only_the_three_stacks_may_be_redeployed(self):
        csrf = self.login()
        for stack in ("monitoring", "ingress", "admin"):
            r = self.client.post("/cluster/stack", data={"csrf": csrf, "stack": stack})
            self.assertEqual(r.status_code, 302, stack)
        for stack in ("api", "app", "anything"):
            r = self.client.post("/cluster/stack", data={"csrf": csrf, "stack": stack})
            self.assertEqual(r.status_code, 400, stack)

    def test_preview_infra_matches_the_shipped_defaults(self):
        """
        The preview's dummy infra.env must agree with the one the cloud-init
        actually writes — for the values that ARE defaults.

        Scoped to the Fleet group on purpose. `ROOT_DOMAIN=mydomain.com` is an
        example you replace, so the preview inventing `acme.dev` is correct.
        `MIN_WORKERS=0` is a decision this project has made, so a preview
        showing 1 is documenting a product that does not exist — which is what
        it did, and is the third time a fixture has quietly disagreed with
        reality during this refactor.
        """
        import pathlib
        import re
        import settings_def

        root = pathlib.Path(__file__).resolve().parents[2]
        block = (root / "master-cloud-init.yaml").read_text()
        block = block[block.index("/etc/infra/infra.env"):block.index("bootstrap.sh")]
        shipped = {}
        for line in block.splitlines():
            match = re.match(r"^\s{6}([A-Z][A-Z0-9_]*)=(.*)$", line)
            if match:
                key, raw = match.groups()
                shipped[key] = raw.split("#")[0].strip()

        self.assertIn("MIN_WORKERS", shipped, "the infra.env block did not parse")
        policy = next(keys for title, keys in settings_def.GROUPS if title == "Fleet")
        for key in policy:
            self.assertIn(key, shipped, f"{key} is offered by the panel but not shipped")
            self.assertEqual(
                self.panel.PREVIEW_INFRA.get(key), shipped[key],
                f"the preview shows {key}={self.panel.PREVIEW_INFRA.get(key)!r} but "
                f"the cloud-init ships {shipped[key]!r}")

    def test_settings_refuses_a_key_it_does_not_manage(self):
        csrf = self.login()
        import envstore
        envstore.load_infra = lambda: {"MIN_WORKERS": "1", "APP_NAME": "aichat"}
        written = {}
        envstore.save_infra = lambda updates: written.update(updates) or list(updates)
        envstore.deploy_stack = lambda name: (True, f"{name} redeployed")
        self.client.post("/settings", follow_redirects=True, data={
            "csrf": csrf, "key": ["MIN_WORKERS", "APP_NAME"],
            "value__MIN_WORKERS": "3", "value__APP_NAME": "hijacked"})
        # APP_NAME is BOOT-mode, so it must be ignored even when posted.
        self.assertEqual(written, {"MIN_WORKERS": "3"})


if __name__ == "__main__":
    unittest.main()
