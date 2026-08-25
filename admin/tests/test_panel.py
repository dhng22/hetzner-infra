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
import re
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
        import shape

        cls.panel = panel
        cls.components = components
        cls.shape = shape
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
                     "/settings", "/api/topology", "/components/new",
                     "/cluster/nodes/k39dl2mzq018"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"], path)

    def test_healthz_is_open(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_writes_require_csrf(self):
        self.login()
        for path in ("/components", "/settings", "/registry", "/cluster/stack",
                     "/cluster/nodes/k39dl2mzq018"):
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

    def test_webhook_accepts_and_returns_where_to_poll(self):
        """
        202, not 200: the rollout has started, not finished.

        Waiting for it would take parallelism x (monitor + delay) x replicas —
        minutes — and Cloudflare's origin timeout is 100 seconds, so the
        attached version returned 524 to the pipeline while the deploy it was
        reporting on succeeded anyway.
        """
        self.create_app("hooked3")
        import state
        token = state.token_for("hooked3")
        r = self.client.post("/hooks/deploy/hooked3",
                             headers={"X-Deploy-Token": token},
                             json={"image": "ghcr.io/you/app:sha-abc1234"})
        self.assertEqual(r.status_code, 202)
        body = r.get_json()
        self.assertEqual(body["service"], "hooked3_app")
        self.assertEqual(body["status"], "pending")
        # Without this the caller has no way to learn the outcome, and 202
        # would just be a deploy with the verdict thrown away.
        self.assertIn("/hooks/deploy/hooked3/status", body["status_url"])

    def test_webhook_status_needs_the_same_token(self):
        self.create_app("hooked4")
        r = self.client.get("/hooks/deploy/hooked4/status",
                            headers={"X-Deploy-Token": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_webhook_status_reports_the_running_image(self):
        """
        After a rollback the status is `failed` and the image is the PREVIOUS
        one, which is how a pipeline tells "my build is not live" from "my build
        is live".
        """
        self.create_app("hooked5")
        import state
        token = state.token_for("hooked5")
        r = self.client.get("/hooks/deploy/hooked5/status",
                            headers={"X-Deploy-Token": token})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn("status", body)
        self.assertIn("image", body)

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

    # --- nodes --------------------------------------------------------------

    NODE = "k39dl2mzq018"

    def test_a_node_page_shows_what_is_on_it(self):
        self.login()
        body = self.client.get(f"/cluster/nodes/{self.NODE}").get_data(as_text=True)
        self.assertIn("aichat-master", body)
        self.assertIn("Labels", body)
        self.assertIn("Availability", body)
        # The tasks placed here, with the tag each one runs.
        self.assertIn("cache_redis", body)
        self.assertIn("7.4-alpine", body)

    def test_an_unknown_node_is_404_not_500(self):
        self.login()
        self.assertEqual(self.client.get("/cluster/nodes/nope").status_code, 404)
        self.assertEqual(self.client.post("/cluster/nodes/nope",
                                          data={"csrf": "x"}).status_code, 400)

    def test_a_node_refuses_an_availability_it_does_not_have(self):
        csrf = self.login()
        r = self.client.post(f"/cluster/nodes/{self.NODE}",
                             data={"csrf": csrf, "node_action": "availability",
                                   "availability": "delete"})
        self.assertEqual(r.status_code, 400)

    def test_a_label_that_cannot_be_a_constraint_is_refused(self):
        """
        Docker would take `my disk`; no placement constraint can ever match it,
        so it is a label you can set and never use.
        """
        csrf = self.login()
        r = self.client.post(f"/cluster/nodes/{self.NODE}",
                             data={"csrf": csrf, "node_action": "labels",
                                   "key": "my disk", "value": "ssd"},
                             follow_redirects=True)
        self.assertIn("not a usable label name", r.get_data(as_text=True))

    def test_the_owner_label_cannot_be_set_from_the_form(self):
        """
        `managedby` is a permission, not a note: putting it on a node the
        autoscaler did not create hands that node to the reaper. The form never
        offers it and the route refuses it if it arrives anyway.
        """
        csrf = self.login()
        r = self.client.post(f"/cluster/nodes/{self.NODE}",
                             data={"csrf": csrf, "node_action": "labels",
                                   "key": "managedby", "value": "autoscaler"},
                             follow_redirects=True)
        self.assertIn("is set by that manager", r.get_data(as_text=True))

    def test_saving_labels_cannot_drop_the_owner(self):
        """
        The label map is REPLACED, not merged. A form that simply omits the
        reserved key would therefore delete it on every save — and a worker with
        no owner is a server nothing will ever remove.
        """
        merged = self.shape.merge_labels({"managedby": "autoscaler", "zone": "eu"},
                                    [{"key": "zone", "value": "us"}])
        self.assertEqual(merged, {"zone": "us", "managedby": "autoscaler"})

    def test_a_submitted_owner_never_reaches_the_node(self):
        merged = self.shape.merge_labels({}, [{"key": "managedby", "value": "autoscaler"}])
        self.assertEqual(merged, {})

    def test_availability_is_locked_on_a_node_someone_else_owns(self):
        """
        The autoscaler rewrites availability every loop on its own nodes, so an
        editable control there is one that silently loses. Faded and disabled.
        """
        self.login()
        worker = "w1af02c9be47"          # carries managedby=autoscaler
        body = self.client.get(f"/cluster/nodes/{worker}").get_data(as_text=True)
        self.assertIn("is-locked", body)
        self.assertIn("in control of this node", body)
        # ...and not on the master, which nothing manages.
        master = self.client.get(f"/cluster/nodes/{self.NODE}").get_data(as_text=True)
        self.assertNotIn("is-locked", master)

    def test_the_node_page_goes_back_where_you_came_from(self):
        self.login()
        from_overview = self.client.get(
            f"/cluster/nodes/{self.NODE}?from=overview").get_data(as_text=True)
        self.assertIn("&larr; Overview", from_overview)
        default = self.client.get(f"/cluster/nodes/{self.NODE}").get_data(as_text=True)
        self.assertIn("&larr; Cluster", default)
        # An origin nobody offers falls back rather than rendering it.
        junk = self.client.get(
            f"/cluster/nodes/{self.NODE}?from=evil").get_data(as_text=True)
        self.assertIn("&larr; Cluster", junk)

    def test_the_create_form_shows_what_settings_shows(self):
        """
        Both are built from the same partial now. The pair drifted the moment
        either changed: create offered every autoscale field with no switch above
        it, and Settings had grown a grouping create never got.
        """
        self.login()
        create = self.client.get("/components/new?type=app").get_data(as_text=True)
        settings = self.client.get("/components/api?tab=settings").get_data(as_text=True)
        for field in ("f-slo_p95_ms", "f-up_p95_ratio", "f-priority", "f-placement_mode"):
            self.assertIn(field, create)
            self.assertIn(field, settings)
        # The autoscale switch owns the section on both, rather than being the
        # first of thirteen inputs on one of them.
        self.assertIn("data-toggle-master", create)
        self.assertIn("data-toggle-master", settings)

    def test_the_create_form_still_asks_for_the_managed_seeds(self):
        """
        Settings hides `managed` fields — CI and the autoscaler own them. At
        create time nothing has run, so there is no live value to preserve and
        the image has to be typed.
        """
        self.login()
        create = self.client.get("/components/new?type=app").get_data(as_text=True)
        self.assertIn('name="image"', create)
        settings = self.client.get("/components/api?tab=settings").get_data(as_text=True)
        self.assertNotIn('name="image"', settings)

    def test_the_header_reads_a_digest_pinned_image_as_live(self):
        """
        Swarm pins `@sha256:...` onto anything it can resolve against a registry,
        so the running service and the deploy that asked for it are never equal
        as strings. Plain equality called an image that had been serving for days
        "not live yet — done", and only for registry-backed images — which is
        every real application, and none of the fixtures until now.
        """
        self.login()
        body = self.client.get("/components/api").get_data(as_text=True)
        self.assertIn("live", body)
        self.assertNotIn("not live yet", body)

    def test_same_image_ignores_the_digest_and_nothing_else(self):
        same = self.shape.same_image
        bare = "ghcr.io/you/app:main-9db4e08"
        self.assertTrue(same(bare + "@sha256:abc", bare))
        self.assertTrue(same(bare, bare))
        # A different tag is a different image, digest or not.
        self.assertFalse(same(bare + "@sha256:abc", "ghcr.io/you/app:main-0000000"))
        # Missing either side is not a match — it is an unknown.
        self.assertFalse(same("", bare))
        self.assertFalse(same(bare, None))

    def test_the_static_urls_carry_a_version_stamp(self):
        """
        Cloudflare replaces the origin's `no-cache` on static extensions with its
        own browser TTL, so after a self-update the new HTML runs against the old
        app.js. The stamp makes a changed file a different URL, which no cache
        keyed on the old one can answer. Every reference has to carry it — the
        login page is served before there is a session, so it counts too.
        """
        for path, expect in (("/login", ("fonts.css", "style.css")),
                             ("/", ("fonts.css", "style.css", "app.js"))):
            if path == "/":
                self.login()
            page = self.client.get(path).get_data(as_text=True)
            for asset in expect:
                m = re.search(r'/static/' + re.escape(asset) + r'(\?v=\d+)?', page)
                self.assertIsNotNone(m, f"{asset} missing from {path}")
                self.assertIsNotNone(m.group(1), f"{asset} unstamped on {path}")

    def test_the_map_tab_refreshes_itself(self):
        """
        The Map tab draws different blocks from the same data as the Overview
        map, so it returns the server's own markup rather than growing a second
        painter that could disagree about how a block is drawn.
        """
        self.login()
        page = self.client.get("/components/api?tab=map").get_data(as_text=True)
        self.assertIn("data-live-html", page)
        frag = self.client.get("/components/api/map")
        self.assertEqual(frag.status_code, 200)
        body = frag.get_data(as_text=True)
        self.assertIn("Rollout map", body)
        # A fragment, not a page.
        self.assertNotIn("<html", body)

    def test_the_map_tab_colours_replicas_by_tag(self):
        """
        The rollout view: two tags on one component is an update in flight, and
        that is the whole thing the tab exists to show.
        """
        self.login()
        body = self.client.get("/components/api?tab=map").get_data(as_text=True)
        self.assertIn('data-panel="map"', body)

    # --- the header's two images ---------------------------------------------

    def test_the_header_shows_the_newest_image_asked_for(self):
        """
        `running` is read off the service, so it is a success by construction —
        a failed deploy leaves the previous image up and the header looked fine.
        """
        self.login()
        if not self.components.exists("api"):
            self.create_app("api")
        newest = self.panel._newest_deploy("api")
        self.assertEqual(newest["image_short"], "aichat-api:sha-9f3ac21")
        body = self.client.get("/components/api").get_data(as_text=True)
        self.assertIn("newest", body)
        self.assertIn("aichat-api:sha-9f3ac21", body)

    def test_a_component_with_no_history_shows_only_what_is_running(self):
        """No request has ever been recorded, so there is no second line to draw."""
        self.create_app("nohistory")
        self.assertIsNone(self.panel._newest_deploy("nohistory"))

    def test_every_component_type_gets_a_colour_band_and_a_short_name(self):
        """
        The chart keys on a component's CATEGORY, which every type declares.
        Keyed on TYPE it was a second registry: a type added to `TYPES` and not
        to that table went silently grey on every chart and lost its short name,
        which is exactly what a new database type did.
        """
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "swarm.py")
        spec = importlib.util.spec_from_file_location("swarm_real", path)
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        for type_name, cls in self.components.TYPES.items():
            self.assertIn(cls.CATEGORY, real._CATEGORY_BANDS, type_name)
            self.assertEqual(real.short_service(f"demo_{type_name}"), "demo", type_name)

    # --- lockout --------------------------------------------------------------

    def test_three_failures_lock_the_address_out_and_the_wait_doubles(self):
        auth = self.panel.auth
        auth._attempts.clear()
        auth._global.update(fails=0, until=0.0)
        try:
            for _ in range(3):
                self.assertFalse(auth.verify("admin", "wrong", "1.2.3.4"))
            first = auth.retry_after("1.2.3.4")
            self.assertTrue(0 < first <= auth.LOCKOUT_BASE_SECONDS)
            # The lock is real: the right password does not get in during it.
            self.assertFalse(auth.verify("admin", "dev", "1.2.3.4"))

            auth._attempts["1.2.3.4"]["until"] = 0        # serve the sentence
            for _ in range(3):
                auth.verify("admin", "wrong", "1.2.3.4")
            self.assertGreater(auth.retry_after("1.2.3.4"), first)
        finally:
            auth._attempts.clear()
            auth._global.update(fails=0, until=0.0)

    def test_a_success_clears_the_ladder(self):
        auth = self.panel.auth
        auth._attempts.clear()
        try:
            auth.verify("admin", "wrong", "5.6.7.8")
            auth.verify("admin", "wrong", "5.6.7.8")
            self.assertTrue(auth.verify("admin", "dev", "5.6.7.8"))
            self.assertEqual(auth.retry_after("5.6.7.8"), 0)
            self.assertNotIn("5.6.7.8", auth._attempts)
        finally:
            auth._attempts.clear()

    def test_the_lockout_is_capped(self):
        auth = self.panel.auth
        self.assertEqual(auth._lockout_seconds(3 * 40), auth.LOCKOUT_MAX_SECONDS)

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

    def test_every_fleet_setting_reaches_the_autoscaler(self):
        """
        A setting is only real once it arrives in the container that reads it.
        The panel offering a key, the cloud-init shipping it and the stack
        passing it are three separate edits, and the third has no visible
        consequence when it is forgotten: `docker stack deploy` substitutes an
        unset variable with the empty string, `_env` falls back to its
        compiled-in default, and the feature is simply off while every page and
        every log line says the update succeeded. Hard vertical scaling shipped
        that way — two of the three edits made, on a cluster whose panel
        reported the right commit.
        """
        import pathlib
        import settings_def

        root = pathlib.Path(__file__).resolve().parents[2]
        stack = (root / "stacks" / "monitoring.yml").read_text()
        fleet = next(keys for title, keys in settings_def.GROUPS if title == "Fleet")
        for key in fleet:
            self.assertIn(
                "${%s}" % key, stack,
                f"{key} is offered by the panel but stacks/monitoring.yml never "
                f"passes it to the autoscaler, so setting it does nothing")

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
