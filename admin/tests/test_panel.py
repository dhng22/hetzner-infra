"""
Route tests for the panel.

    python3 -m unittest discover -s admin/tests -v

Runs the real Flask app against a temp INFRA_DIR and the preview fixtures in
place of Docker, so every route is exercised end to end — auth, CSRF, the
generic component pages, and the writes. The point is not coverage for its own
sake: the panel is a root console, so "does this route refuse what it should
refuse" is a correctness property worth pinning.
"""

import errno
import os
import importlib.machinery
import shutil
import sys
import tempfile
import re
import unittest

_ADMIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ADMIN)
# The repository ROOT as well, for `pki`. The panel ISSUES the TLS material a
# managed database mounts, so `admin/Dockerfile` copies `pki/` in from beside
# `admin/` — but this suite runs with `admin/` as the working directory both
# in the image and out of it, and that puts admin/ on the path and the root
# nowhere. Without this line every mongo render test dies on `import pki`.
sys.path.insert(1, os.path.dirname(_ADMIN))


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

    def running(self, name):
        """
        Put a component's services into the fixture cluster.

        A save does not start anything — `_deploy_if_needed` skips the deploy
        when the primary service does not exist — so a test about what a deploy
        touches has to say the component is actually deployed. Registered in
        the same table `fixtures.service` reads, and removed again afterwards
        so no test inherits another's cluster.
        """
        component = self.components.load(name)
        for service in component.services():
            self.panel.data._SERVICES[service] = self.panel.data._svc(
                service, "ghcr.io/you/app:sha-abc1234", 1, 1)
            self.addCleanup(self.panel.data._SERVICES.pop, service, None)
        return component

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
        for path in ("/", "/components", "/cluster", "/manager", "/alerts",
                     "/settings", "/api/topology", "/components/new",
                     # Grafana too. It has no hostname of its own any more, so
                     # this session is the only thing in front of every metric
                     # the cluster has ever recorded.
                     "/grafana/", "/grafana/d/abc/dash",
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

    def test_a_dotted_variable_name_is_not_rejected(self):
        """
        `MONGODB.DBNAME` is a name real applications read. The environment goes
        into a compose `environment:` mapping and then through execve, neither
        of which applies shell identifier rules, so refusing it was the panel
        inventing a limit the system does not have.
        """
        csrf, _ = self.create_app("dotted")
        page = self.client.post("/components/dotted/env", follow_redirects=True,
                                data={"csrf": csrf,
                                      "bulk": "MONGODB.DBNAME=drama\n"
                                              "spring.data.mongodb.uri=x\n"}
                                ).get_data(as_text=True)
        self.assertNotIn("variable name", page)
        self.assertEqual(self.components.store.env_map("dotted"),
                         {"MONGODB.DBNAME": "drama", "spring.data.mongodb.uri": "x"})

    def test_a_name_this_file_cannot_hold_is_still_refused(self):
        """The rule that is left is the format's, not the shell's."""
        problems = self.components.store.validate_env(
            [{"key": "A B", "value": "1"}, {"key": "#X", "value": "1"},
             {"key": "", "value": "1"}, {"key": "OK.NAME", "value": "1"}])
        self.assertEqual(len(problems), 3)

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
        The preview's dummy infra.env must not document a product that does not
        exist. `ROOT_DOMAIN=mydomain.com` is an example you replace, so the
        preview inventing `acme.dev` is correct — but a value that HAS a default
        must agree with it, and `MIN_WORKERS=1` in a fixture is a lie about a
        decision this project has made.

        It is checked against DEFAULTS rather than against the cloud-init now,
        because the cloud-init no longer carries fleet or database policy at
        all: those rows come from DEFAULTS on a real cluster too, which is the
        path this fixture is supposed to stand in for.
        """
        import settings_def

        # POLICY only. `Identity` and `External` describe THIS cluster — its
        # name, its domain, the address the outside reaches it on — and a
        # preview showing a plausible one is the same honest fiction as
        # `acme.dev`. Everything else is a decision this project has made, and a
        # fixture contradicting one of those is the lie this test is for.
        about_the_cluster = {key for title, keys in settings_def.GROUPS
                             if title in ("Identity", "External") for key in keys}
        for key, value in self.panel.PREVIEW_INFRA.items():
            if key in settings_def.DEFAULTS and key not in about_the_cluster:
                self.assertEqual(
                    value, settings_def.DEFAULTS[key],
                    f"the preview shows {key}={value!r} but the default is "
                    f"{settings_def.DEFAULTS[key]!r}")

    def test_the_cloud_init_ships_no_setting_that_already_has_a_default(self):
        """
        ONE HOME PER DEFAULT. The cloud-init is pasted into Hetzner by hand,
        never revised afterwards, and capped at 32 KB — so a scaling or database
        number written there is both a second copy that drifts from the repo's
        and a line of a budget better spent on the things only you can supply.
        infra.env carries this cluster's ANSWERS; settings_def carries the
        defaults, `_settings_groups` falls back to them, and Manager > Fleet and
        Manager > Databases are where they are changed.
        """
        import pathlib
        import re
        import settings_def

        root = pathlib.Path(__file__).resolve().parents[2]
        text = (root / "master-cloud-init.yaml").read_text()
        block = text[text.index("/etc/infra/infra.env"):text.index("bootstrap.sh")]
        shipped = {m.group(1) for m in
                   re.finditer(r"^\s{6}([A-Z][A-Z0-9_]*)=", block, re.M)}

        self.assertIn("APP_NAME", shipped, "the infra.env block did not parse")
        for key in settings_def.DEFAULTS:
            self.assertNotIn(
                key, shipped,
                f"{key} has a default in settings_def and is shipped in the "
                f"cloud-init as well — two copies of one number")

    def test_the_cloud_init_stays_under_the_hetzner_paste_limit(self):
        """
        Hetzner refuses user-data over 32 KB, and the failure is at server
        creation: no boot, no log, nothing to read. It grew to 38 KB by
        accretion, which is why the fleet and database blocks now live in
        DEFAULTS instead. The headroom is worth keeping.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        size = len((root / "master-cloud-init.yaml").read_bytes())
        self.assertLess(size, 32768, f"master-cloud-init.yaml is {size} bytes")

    def test_every_setting_the_panel_offers_reaches_the_process_that_reads_it(self):
        """
        The panel offering a knob and a process reading it are useless if the
        value never travels between them. It used to travel by being listed a
        third time in stacks/monitoring.yml, which is an edit with no visible
        consequence when it is missed: the row renders, the save succeeds, and
        the process goes on using its own default. The stack names
        /etc/infra/fleet.env instead, and that file is generated from this list.

        All three readers are scanned, because fleet.env now feeds the overseer
        AND dataguard — the file is one delivery to several processes, and a
        setting reaching only the one it used to belong to is exactly the failure
        this exists to catch.
        """
        import pathlib
        import re
        import settings_def

        root = pathlib.Path(__file__).resolve().parents[2]
        reads = set()
        for source in ("overseer/overseer.py", "autoscaler/autoscaler.py",
                       "dataguard/dataguard.py"):
            reads |= set(re.findall(r'_env\(\s*"([A-Z][A-Z0-9_]*)"',
                                    (root / source).read_text()))

        delivered = settings_def.autoscaler_env({})
        offered = [k for k in settings_def.DEFAULTS if k in reads]
        self.assertIn("MIN_WORKERS", offered, "nothing parsed — the assertion below is vacuous")
        for key in offered:
            self.assertIn(key, delivered,
                          f"{key} is offered by the panel and read by a process, "
                          f"but it is in no group fleet.env is built from")
            self.assertEqual(delivered[key], settings_def.DEFAULTS[key])

    def test_the_autoscaler_env_never_carries_a_credential(self):
        """
        fleet.env is an ALLOW-list — whole groups, minus SECRET — so a
        credential added to Access later cannot reach the autoscaler by having
        been overlooked. It is a root console's worth of secrets in that file:
        the admin password, the Cloudflare tunnel token, the alert bot token.
        HCLOUD_TOKEN is the one credential the autoscaler needs, and it arrives
        as a docker secret rather than through here.
        """
        import settings_def

        # A fully-populated infra.env, so nothing is missing merely by accident.
        every = {key: f"value-of-{key}" for key in settings_def.FIELDS}
        delivered = settings_def.autoscaler_env(every)

        for key in delivered:
            self.assertNotEqual(settings_def.describe(key)[0], settings_def.SECRET, key)
        for key in ("HCLOUD_TOKEN", "ADMIN_PASSWORD", "ADMIN_USER",
                    "CF_TUNNEL_TOKEN", "ALERT_TELEGRAM_BOT_TOKEN", "GHCR_TOKEN"):
            self.assertNotIn(key, delivered, f"{key} would reach the autoscaler's environment")

    def test_the_autoscaler_env_is_free_of_the_documentation(self):
        """
        infra.env is a documented file — nearly every line carries a trailing
        comment — and fleet.env is machine input. A parser that keeps the
        comment gives WORKER_IMAGE the value "ubuntu-24.04   # what a worker
        boots" and MAX_WORKERS the value "5   # a BUDGET cap", and the second of
        those is an int() that raises at import: the process crashloops on a config
        file that looks perfectly fine when you cat it. bin/render-fleet-env
        therefore reads through envstore, which already implements the shell
        rule, instead of splitting on "=" itself.
        """
        import envstore
        import settings_def

        path = os.path.join(self.tmp, "commented.env")
        with open(path, "w") as fh:
            fh.write("# a header comment\n"
                     "WORKER_IMAGE=ubuntu-24.04      # what a worker boots\n"
                     "MAX_WORKERS=5                  # a BUDGET cap\n"
                     "SCHEDULE_FLOOR=                # UTC; \"HH:MM-HH:MM=N\"\n"
                     "HCLOUD_SSH_KEY_NAME=me@host#1\n")
        real = envstore.INFRA_ENV
        envstore.INFRA_ENV = path
        try:
            delivered = settings_def.autoscaler_env(envstore.load_infra())
        finally:
            envstore.INFRA_ENV = real

        self.assertEqual(delivered["WORKER_IMAGE"], "ubuntu-24.04")
        self.assertEqual(delivered["MAX_WORKERS"], "5")
        self.assertEqual(delivered["SCHEDULE_FLOOR"], "")
        # No whitespace before the '#', so it is part of the value, not a comment.
        self.assertEqual(delivered["HCLOUD_SSH_KEY_NAME"], "me@host#1")

    def test_saving_a_setting_the_file_predates_appends_it(self):
        """
        The panel now offers a setting whose default lives in the repo, so the
        save path has to be able to write a key infra.env has never carried.
        Dropping it silently is the same failure one layer down: the row
        renders, the form submits, the flash says saved, and nothing changed.
        """
        import envstore

        path = os.path.join(self.tmp, "sparse.env")
        with open(path, "w") as fh:
            fh.write("MIN_WORKERS=0                  # the free floor\n")
        real = envstore.INFRA_ENV
        envstore.INFRA_ENV = path
        try:
            changed = envstore.save_infra({"MIN_WORKERS": "1", "WORKER_MAX_CORES": "4"})
            after = envstore.load_infra()
        finally:
            envstore.INFRA_ENV = real

        self.assertEqual(sorted(changed), ["MIN_WORKERS", "WORKER_MAX_CORES"])
        self.assertEqual(after["WORKER_MAX_CORES"], "4")
        self.assertEqual(after["MIN_WORKERS"], "1")
        # The documentation on the line that already existed survives.
        with open(path) as fh:
            self.assertIn("# the free floor", fh.read())

    def test_a_setting_saves_even_though_infra_env_cannot_be_renamed_over(self):
        """
        Every settings save was a 500, and the panel had never done otherwise.

            OSError: [Errno 16] Device or resource busy:
                '/etc/infra/infra.env.tmp' -> '/etc/infra/infra.env'

        `/etc/infra/infra.env` is bind-mounted into the panel container as a
        FILE, and you cannot rename onto a mount point. Every other file this
        panel writes lives under `/opt/infra`, which is mounted as a directory,
        so the temp-and-swap that is correct everywhere else is the one thing
        that can never work here — and nothing noticed, because a test writing
        to an ordinary file in a tmpdir renames perfectly happily.

        So the failure is what gets faked, not the file: `os.replace` refuses
        with the kernel's own errno, exactly as the mount does.
        """
        import envstore

        path = os.path.join(self.tmp, "mounted.env")
        with open(path, "w") as fh:
            fh.write("DATAGUARD_DRY_RUN=true         # do not act\n")

        def refuse(src, dst):
            raise OSError(errno.EBUSY, "Device or resource busy", src, None, dst)

        real_path, real_replace = envstore.INFRA_ENV, envstore.os.replace
        real_dir = envstore.INFRA_DIR
        envstore.INFRA_ENV = path
        envstore.INFRA_DIR = self.tmp
        envstore.os.replace = refuse
        try:
            changed = envstore.save_infra({"DATAGUARD_DRY_RUN": "false"})
            after = envstore.load_infra()
        finally:
            envstore.INFRA_ENV, envstore.INFRA_DIR = real_path, real_dir
            envstore.os.replace = real_replace

        self.assertEqual(changed, ["DATAGUARD_DRY_RUN"])
        self.assertEqual(after["DATAGUARD_DRY_RUN"], "false")
        with open(path) as fh:
            body = fh.read()
        # Written THROUGH the same file, not beside it, and no trailing wreckage
        # of the longer line it replaced.
        self.assertEqual(body, "DATAGUARD_DRY_RUN=false         # do not act\n")
        self.assertFalse(os.path.exists(f"{path}.tmp"))
        # And what was there before is somewhere that outlives the container.
        with open(os.path.join(self.tmp, "state", "infra.env.previous")) as fh:
            self.assertIn("DATAGUARD_DRY_RUN=true", fh.read())

    def test_ci_publishes_exactly_the_images_this_cluster_pulls(self):
        """
        THREE FILES HAVE TO AGREE ON ONE STRING, and none of them can import the
        others: GitHub Actions decides what to publish, bin/infra-images decides
        what the master pulls, and the stack files decide what Swarm runs. A
        disagreement is not a crash — the master pulls a tag that was never
        published, gets a 404 it correctly treats as "CI has not finished yet",
        and sits at "update pending" forever with nothing in any log that says
        the name was simply wrong.

        So the derivation is not read, it is RUN, against a repository URL whose
        answer is known, and compared against the name the workflow builds for
        the same repository.
        """
        import pathlib
        import re
        import subprocess

        import yaml

        root = pathlib.Path(__file__).resolve().parents[2]
        workflow = yaml.safe_load(
            (root / ".github" / "workflows" / "publish.yml").read_text())
        steps = workflow["jobs"]["publish"]["steps"]
        naming = next(s for s in steps if s.get("id") == "names")["run"]
        pushes = next(s for s in steps if s.get("name") == "Publish")["run"]

        # What the workflow would call each image for github.com/Owner/Repo at
        # commit abc123 — computed by running the workflow's own shell, with the
        # two expressions GitHub would have substituted already substituted.
        script = naming.replace("${{ github.repository }}", "Owner/Repo") \
                       .replace("${{ github.sha }}", "abc123")
        out = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True,
            env={"GITHUB_OUTPUT": "/dev/stdout", "PATH": "/usr/bin:/bin"})
        self.assertEqual(out.returncode, 0, out.stderr)
        published = dict(line.split("=", 1) for line in out.stdout.split() if "=" in line)
        self.assertTrue(published, f"the naming step produced nothing: {out.stdout!r}")

        # What the master would pull for the same repository and commit.
        pull = subprocess.run(
            ["bash", "-c",
             f'. "{root}/bin/infra-images"; infra_images abc123 || exit 1; infra_image_refs'],
            capture_output=True, text=True,
            env={"INFRA_REPO_URL": "https://github.com/Owner/Repo.git",
                 "PATH": "/usr/bin:/bin"})
        self.assertEqual(pull.returncode, 0, pull.stderr)
        pulled = pull.stdout.split()

        self.assertEqual(sorted(published.values()), sorted(pulled),
                         "GitHub Actions publishes one set of names and the master "
                         "pulls another")

        # Everything built is also pushed. A partial publish is the one outcome
        # the master cannot recover from on its own: it finds some of the images
        # at a commit and waits for the rest until the deadline.
        for name, ref in published.items():
            self.assertIn(f"steps.names.outputs.{name}", pushes,
                          f"the workflow builds {name} but never pushes it")

        # And everything published is referenced by a stack, under the variable
        # bin/infra-images exports. An image nobody deploys is dead weight; a
        # stack naming a variable nobody exports is an empty `image:` field.
        stacks = "\n".join((root / "stacks" / f).read_text()
                            for f in ("monitoring.yml", "admin.yml", "ingress.yml"))
        exported = set(re.findall(r"INFRA_IMAGE_[A-Z]+", (root / "bin" / "infra-images").read_text()))
        for name in published:
            var = f"INFRA_IMAGE_{name.upper()}"
            self.assertIn(var, exported, f"bin/infra-images never exports {var}")
            self.assertIn("${%s}" % var, stacks,
                          f"{name} is published and pulled but no stack runs it")

    def test_every_infrastructure_service_reserves_something(self):
        """
        A SERVICE WITH NO RESERVATION IS INVISIBLE TO THE CAPACITY ARITHMETIC.

        `node_free_for_apps` measures room as the node's real size minus the
        reservations of everything on it that is not an app. A service that
        declares none therefore contributes zero to that subtraction while
        consuming real memory, so the autoscaler places replicas into space that
        is already occupied — and the symptom is not a scheduling refusal, it is
        the kernel picking something to kill once the box is actually full.

        Cheap to get wrong, too: `resources:` is optional, so a service added
        without one deploys perfectly and reports healthy.
        """
        import pathlib

        import yaml

        root = pathlib.Path(__file__).resolve().parents[2]
        for name in ("monitoring.yml", "admin.yml", "ingress.yml"):
            spec = yaml.safe_load((root / "stacks" / name).read_text())
            for service, body in (spec.get("services") or {}).items():
                res = (((body.get("deploy") or {}).get("resources") or {})
                       .get("reservations") or {})
                self.assertTrue(
                    res.get("cpus") and res.get("memory"),
                    f"{name}: service '{service}' declares no CPU/memory reservation, "
                    f"so the autoscaler counts the space it occupies as free")

    def test_the_stack_and_the_renderer_agree_on_one_path(self):
        """
        bin/stack-deploy writes the file and stacks/monitoring.yml reads it. Two
        spellings of the path is an autoscaler deployed with an empty
        environment and no error anywhere.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        path = "/etc/infra/fleet.env"
        self.assertIn(path, (root / "stacks" / "monitoring.yml").read_text())
        self.assertIn(path, (root / "bin" / "stack-deploy").read_text())
        self.assertIn(path, (root / "bin" / "render-fleet-env").read_text())

    def test_every_page_survives_an_autoscaler_that_is_not_reporting(self):
        """
        Every gauge on the Autoscaler page is None when the autoscaler is down
        or has not been scraped yet — which is precisely when somebody opens it.
        The tiles were guarded one by one, the meter's comparison was not, and
        `None >= None` took the page down in production while the preview of the
        same template rendered perfectly, because the fixtures only ever
        described a healthy cluster.
        """
        import fixtures

        self.login()
        real = fixtures.autoscaler_state
        fixtures.autoscaler_state = fixtures.autoscaler_state_silent
        try:
            for path in ("/", "/components", "/cluster", "/manager", "/alerts",
                         "/settings", "/api/topology"):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200, f"{path} broke with no metrics")
            page = self.client.get("/manager").get_data(as_text=True)
            self.assertIn("not reporting", page)
        finally:
            fixtures.autoscaler_state = real

    def test_a_setting_the_cluster_has_never_heard_of_is_still_offered(self):
        """
        infra.env is written once by cloud-init and never gains a key, so a knob
        added later reached new clusters and no existing one. Skipping it made
        it invisible AND unsettable — the form is built from these rows — which
        is how the vertical-scaling ceilings could not be turned on from the
        panel of the cluster they were written for.
        """
        import envstore
        import settings_def

        real = envstore.load_infra
        envstore.load_infra = lambda: {"MIN_WORKERS": "2"}
        try:
            rows = {r["key"]: r["value"]
                    for group in self.panel._settings_groups() + \
                                 self.panel._settings_groups(only=self.panel.AUTOSCALER_GROUPS)
                    for r in group["rows"]}
        finally:
            envstore.load_infra = real
        # The cluster's own answer wins.
        self.assertEqual(rows["MIN_WORKERS"], "2")
        # Everything else the repo ships a default for is still offered.
        for key, default in settings_def.DEFAULTS.items():
            if key == "MIN_WORKERS":
                continue
            self.assertIn(key, rows, f"{key} is not offered on a cluster whose infra.env predates it")
            self.assertEqual(rows[key], default)

    def test_defaults_agree_everywhere(self):
        """
        A setting's default is written down three times — the panel's DEFAULTS,
        the reading process's own `_env` call and the cloud-init's infra.env
        block — because each is read by a different process and none can import
        the others. Three copies of one number is drift waiting to happen, so it
        is drift the test suite refuses rather than a convention.

        The READERS are the overseer, the autoscaler and dataguard, and which
        one owns a given setting moved once already: the fleet settings were the
        autoscaler's and are now the overseer's. Scanning all three rather than
        naming one is what stops the next move from silently emptying this test
        — an empty `reader` would make every assertion below vacuous, which is
        why the sentinel check exists.
        """
        import pathlib
        import re
        import settings_def

        root = pathlib.Path(__file__).resolve().parents[2]

        reader = {}
        for source in ("overseer/overseer.py", "autoscaler/autoscaler.py",
                       "dataguard/dataguard.py"):
            code = (root / source).read_text()
            reader.update(re.findall(
                r'_env\(\s*"([A-Z][A-Z0-9_]*)"\s*,\s*"([^"]*)"', code))

        block = (root / "master-cloud-init.yaml").read_text()
        block = block[block.index("/etc/infra/infra.env"):block.index("bootstrap.sh")]
        shipped = {}
        for line in block.splitlines():
            match = re.match(r"^\s{6}([A-Z][A-Z0-9_]*)=(.*)$", line)
            if match:
                shipped[match.group(1)] = match.group(2).split("#")[0].strip()

        self.assertIn("MIN_WORKERS", reader, "no config block parsed at all")
        for key, default in settings_def.DEFAULTS.items():
            self.assertIn(key, reader,
                          f"{key} is offered by the panel but nothing reads it")
            self.assertEqual(reader[key], default,
                             f"{key}: panel defaults to {default!r}, the reader to "
                             f"{reader[key]!r}")

    def test_dataguard_labels_agree_across_the_wire(self):
        """
        Every `dataguard.*` label a component writes is one dataguard reads, and
        every one it reads is one some component writes.

        These are two files that cannot import each other, joined by a string.
        A rename on either side does not fail anything, does not warn, and does
        not look different in the panel — the setting simply stops arriving, and
        the component keeps showing whatever the operator typed. Retention is
        the reason this test exists now: a `max_snapshots` that never reaches
        dataguard means snapshots accumulate forever while the form says 7.

        Deliberately not asserting a fixed list. A new label added to both sides
        should pass without editing this, and added to only one should not.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2]
        code = (root / "dataguard" / "dataguard.py").read_text()
        prefix = re.search(r'^DG\s*=\s*"([^"]*)"', code, re.M)
        self.assertTrue(prefix, "could not find dataguard's label prefix")
        read = {prefix.group(1) + name for name in
                re.findall(r'^L_[A-Z0-9_]+\s*=\s*DG\s*\+\s*"([^"]+)"', code, re.M)}
        self.assertIn("dataguard.max_snapshots", read,
                      "no label constants parsed at all")

        written = set()
        for source in ("mongo.py", "redis.py"):
            body = (root / "admin" / "components" / source).read_text()
            written.update(re.findall(r'"(dataguard\.[a-z0-9_]+)"\s*:', body))
        self.assertIn("dataguard.max_snapshots", written,
                      "no component labels parsed at all")

        # `set` and `enabled` are read from the service NAME and the presence of
        # the stack rather than through an L_ constant, so they are written
        # without being read here. Nothing may be READ without being written.
        for label in sorted(read - written):
            self.fail(f"dataguard reads {label} but no component ever writes it")

    def test_settings_refuses_a_key_it_does_not_manage(self):
        csrf = self.login()
        import envstore
        saved = (envstore.load_infra, envstore.save_infra, envstore.deploy_stack)
        envstore.load_infra = lambda: {"MIN_WORKERS": "1", "APP_NAME": "aichat"}
        written = {}
        envstore.save_infra = lambda updates: written.update(updates) or list(updates)
        envstore.deploy_stack = lambda name: (True, f"{name} redeployed")
        try:
            self.client.post("/settings", follow_redirects=True, data={
                "csrf": csrf, "key": ["MIN_WORKERS", "APP_NAME"],
                "value__MIN_WORKERS": "3", "value__APP_NAME": "hijacked"})
        finally:
            envstore.load_infra, envstore.save_infra, envstore.deploy_stack = saved
        # APP_NAME is BOOT-mode, so it must be ignored even when posted.
        self.assertEqual(written, {"MIN_WORKERS": "3"})

    # --- the Panel section: where this master updates itself from -----------

    def _save_settings(self, form, values, host=None):
        """POST /settings with infra.env and the host channel stubbed out."""
        import envstore
        import hostops
        csrf = self.login()
        saved = (envstore.load_infra, envstore.save_infra, envstore.deploy_stack,
                 hostops.available, hostops.repo_check)
        written, deployed = {}, []
        envstore.load_infra = lambda: dict(values)
        envstore.save_infra = lambda updates: written.update(updates) or list(updates)
        envstore.deploy_stack = lambda name: deployed.append(name) or (True, "ok")
        hostops.available = lambda: host is not None
        hostops.repo_check = lambda: host
        try:
            page = self.client.post("/settings", follow_redirects=True,
                                    data=dict(form, csrf=csrf)).get_data(as_text=True)
        finally:
            (envstore.load_infra, envstore.save_infra, envstore.deploy_stack,
             hostops.available, hostops.repo_check) = saved
        return written, deployed, page

    #: An infra.env with the repo settings a real cluster's cloud-init ships.
    REPO_ENV = {"INFRA_REPO_URL": "https://x-access-token:t0k@github.com/a/b.git",
                "INFRA_REPO_BRANCH": "master", "APP_NAME": "aichat"}

    def _settings_page(self, values):
        import envstore
        self.login()
        real = envstore.load_infra
        envstore.load_infra = lambda: dict(values)
        try:
            return self.client.get("/settings").get_data(as_text=True)
        finally:
            envstore.load_infra = real

    def test_the_repo_the_master_pulls_from_is_the_first_thing_settings_offers(self):
        """
        It governs whether any other setting on the page can ever be applied: a
        master that cannot pull is a master where every other save is the last
        one that will ever take effect.
        """
        page = self._settings_page(self.REPO_ENV)
        self.assertIn("INFRA_REPO_URL", page)
        self.assertIn("INFRA_REPO_BRANCH", page)
        self.assertLess(page.index(">Panel<"), page.index(">Identity<"))

    def test_the_repo_url_is_editable_and_still_behind_a_reveal(self):
        """
        It is the first setting that is both: a value you have to be able to
        type, carrying a credential you should not have to display to type it.
        Every masked field before this one was read-only, so `masked` was only
        ever reached on a branch that could not be edited.
        """
        page = self._settings_page(self.REPO_ENV)
        at = page.index("set-INFRA_REPO_URL")
        row = page[page.rindex('<div class="setting-row">', 0, at):
                   page.index("</p>", at)]
        self.assertIn('type="password"', row)
        self.assertIn('name="value__INFRA_REPO_URL"', row)
        self.assertIn("data-reveal", row)
        # The branch beside it is not a credential and is not hidden.
        self.assertIn('type="text" name="value__INFRA_REPO_BRANCH" value="master"',
                      page)

    def test_saving_the_repo_redeploys_nothing_and_asks_the_master_instead(self):
        """
        These settings have no stack: `bin/infra-update` re-reads infra.env on
        its own timer. That is also why the save is VERIFIED — with nothing to
        redeploy there is nothing that would report a wrong value back, so the
        first sign of one would be a cluster that quietly stopped updating.
        """
        written, deployed, page = self._save_settings(
            {"key": ["INFRA_REPO_URL"],
             "value__INFRA_REPO_URL": "https://x-access-token:t@github.com/a/b.git"},
            {"INFRA_REPO_URL": "https://old@github.com/a/b.git"},
            host=(True, "running abc -> upstream abc on master\n--check: stopping"))
        self.assertEqual(written, {
            "INFRA_REPO_URL": "https://x-access-token:t@github.com/a/b.git"})
        self.assertEqual(deployed, [])
        self.assertIn("Repo reachable", page)

    def test_a_repo_the_master_cannot_reach_is_reported_as_a_failure(self):
        """
        The value is still saved — refusing to write it would leave you unable
        to correct a URL from the page that shows it. What must not happen is
        the save reading as a success.
        """
        written, _, page = self._save_settings(
            {"key": ["INFRA_REPO_URL"], "value__INFRA_REPO_URL": "https://nope/x.git"},
            {"INFRA_REPO_URL": "https://old@github.com/a/b.git"},
            host=(False, "ERROR: cannot reach the repo: fatal: could not read Password"))
        self.assertEqual(written, {"INFRA_REPO_URL": "https://nope/x.git"})
        self.assertIn("Repo check FAILED", page)
        self.assertIn("could not read Password", page)
        self.assertIn("banner bad", page)

    def test_a_cluster_with_no_host_channel_is_told_so_not_told_it_worked(self):
        written, _, page = self._save_settings(
            {"key": ["INFRA_REPO_BRANCH"], "value__INFRA_REPO_BRANCH": "next"},
            {"INFRA_REPO_BRANCH": "master"}, host=None)
        self.assertEqual(written, {"INFRA_REPO_BRANCH": "next"})
        self.assertIn("Cannot check it from here", page)

    def test_a_master_whose_helper_predates_the_check_is_not_called_a_failure(self):
        """
        `repo-check` is a verb a master only has once it has UPDATED — and the
        first thing anyone uses this section for is a master that cannot
        update. Reading the forced command's refusal as a bad URL would send
        you back to correct a value you had just corrected.
        """
        _, _, page = self._save_settings(
            {"key": ["INFRA_REPO_URL"], "value__INFRA_REPO_URL": "https://a/b.git"},
            {"INFRA_REPO_URL": "https://old/b.git"},
            host=(False, "refused: the admin panel may only run: ufw-deny <port>"))
        self.assertIn("predates the check", page)
        self.assertNotIn("FAILED", page)
        self.assertNotIn("banner bad", page)

    def test_a_setting_the_file_predates_can_be_saved_through_the_route(self):
        """
        `envstore.save_infra` grew an append path for settings added after a
        cluster was built; the route in front of it went on refusing exactly
        those, because it required the key to already be in the file. The row
        rendered from the repo's default, the form submitted, the flash said
        nothing had changed, and the knob was unreachable on any cluster older
        than it was.
        """
        written, _, page = self._save_settings(
            {"key": ["WORKER_MAX_CORES"], "value__WORKER_MAX_CORES": "12"},
            {"MIN_WORKERS": "1"})          # infra.env predates the ceiling
        self.assertEqual(written, {"WORKER_MAX_CORES": "12"})
        self.assertNotIn("Nothing changed", page)

    def test_the_repo_url_never_reaches_the_fleet_env(self):
        """
        It carries a token, and fleet.env is handed wholesale to the overseer
        and to dataguard. The allow-list is by GROUP, so this is really a test
        that Panel was not added to AUTOSCALER_ENV_GROUPS by reflex.
        """
        import settings_def
        delivered = settings_def.autoscaler_env(
            {key: f"value-of-{key}" for key in settings_def.FIELDS})
        for key in ("INFRA_REPO_URL", "INFRA_REPO_BRANCH"):
            self.assertNotIn(key, delivered)

    def test_the_cloud_init_ships_every_key_the_panel_section_offers(self):
        """
        The Panel rows have no entry in DEFAULTS — there is no sane default for
        "which repo is this cluster", and inventing one would point a fresh
        master at somebody else's code. So they render only if infra.env
        carries them, which means a cloud-init that stopped shipping one would
        not leave the field blank: it would make the whole section vanish, on
        the one page that exists to set it.
        """
        import pathlib
        import re
        import settings_def

        root = pathlib.Path(__file__).resolve().parents[2]
        block = (root / "master-cloud-init.yaml").read_text()
        block = block[block.index("/etc/infra/infra.env"):block.index("bootstrap.sh")]
        shipped = {m.group(1) for m in
                   re.finditer(r"^\s{6}([A-Z][A-Z0-9_]*)=", block, re.M)}

        self.assertIn("APP_NAME", shipped, "the infra.env block did not parse")
        panel = next(keys for title, keys in settings_def.GROUPS if title == "Panel")
        for key in panel:
            self.assertIn(key, shipped,
                          f"{key} is offered by Settings > Panel but no cluster "
                          f"is built with it, so the row can never render")
            self.assertNotIn(key, settings_def.DEFAULTS,
                             f"{key} must have no invented default")

    # --- the tunnel connector ---------------------------------------------
    #
    # cloudflared is the only image this cluster runs that nobody here builds
    # and no commit of ours moves. Its version is therefore RESOLVED —
    # bin/cloudflared-version decides, the update timer refreshes it once a day
    # — and these pin the two things that resolution must never get wrong: it
    # must not go backwards past the release that made the tunnel work at all,
    # and it must not leave a fresh cluster a year behind.

    def _resolve(self, state=None, pin=None):
        """CLOUDFLARED_VERSION as bin/cloudflared-version resolves it."""
        import json
        import pathlib
        import subprocess
        import tempfile

        root = pathlib.Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as home:
            (pathlib.Path(home) / "state").mkdir()
            if state is not None:
                (pathlib.Path(home) / "state" / "cloudflared.json").write_text(
                    json.dumps(state))
            script = (f'set -euo pipefail\n'
                      f'INFRA_DIR={home}\n'
                      f'{"CLOUDFLARED_PIN=" + pin if pin else ""}\n'
                      f'. {root}/bin/cloudflared-version\n'
                      f'cloudflared_version\n'
                      f'printf "%s" "$CLOUDFLARED_VERSION"\n')
            done = subprocess.run(["bash", "-c", script],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout.strip()

    def _constant(self, name):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2]
        text = (root / "bin" / "cloudflared-version").read_text()
        found = re.search(rf'^{name}="([^"]+)"', text, re.M)
        self.assertIsNotNone(found, f"{name} is not defined any more")
        return found.group(1)

    def test_a_fresh_cluster_gets_a_recent_connector_not_the_floor(self):
        """
        A master created today, before its first upstream check — or one that
        cannot reach the release feed at all — must come up on something recent.
        Resolving to the FLOOR instead would be technically working and a year
        out of date on the one service that is the front door, which is the
        state this whole mechanism exists to end.
        """
        baseline = self._constant("CLOUDFLARED_BASELINE")
        self.assertEqual(self._resolve(state=None), baseline)
        self.assertNotEqual(baseline, self._constant("CLOUDFLARED_FLOOR"))

    def test_nothing_resolves_below_the_release_that_reads_the_token_file(self):
        """
        TUNNEL_TOKEN_FILE did not exist before 2025.4.0. Under it the connector
        ignores the variable, starts with NO token and never registers — and
        reports as perfectly healthy while doing it, because the only party that
        knows is Cloudflare. So the floor holds against every input: a state
        file somebody edited, and a pin somebody typed.
        """
        floor = self._constant("CLOUDFLARED_FLOOR")
        self.assertEqual(self._resolve(state={"version": "2024.10.1"}), floor)
        self.assertEqual(self._resolve(pin="2024.10.1"), floor)

    def test_an_unreadable_version_falls_back_instead_of_deploying_a_broken_tag(self):
        """
        `image: cloudflare/cloudflared:` is a compose error at deploy time, and
        an empty or malformed version is the only way to produce one. Garbage in
        either input resolves to the baseline rather than being passed through.
        """
        baseline = self._constant("CLOUDFLARED_BASELINE")
        self.assertEqual(self._resolve(state={"version": "latest"}), baseline)
        self.assertEqual(self._resolve(state={}), baseline)
        self.assertEqual(self._resolve(pin="not-a-version"), baseline)

    def test_a_pin_beats_what_upstream_last_said(self):
        """
        The escape hatch has to actually win: the morning a new connector is the
        problem, CLOUDFLARED_PIN in infra.env is how you stop the timer moving
        you onto it, and it is worthless if the recorded version outranks it.
        """
        self.assertEqual(
            self._resolve(state={"version": "2026.9.9"}, pin="2026.7.0"),
            "2026.7.0")

    def test_versions_compare_as_numbers_and_not_as_text(self):
        """
        Cloudflare's calendar versioning reaches double digits every year:
        2026.8.10 comes AFTER 2026.8.2, and string comparison says the
        opposite. Getting this backwards would make the daily check quietly
        refuse every release for a month.
        """
        import pathlib
        import subprocess

        root = pathlib.Path(__file__).resolve().parents[2]
        table = [("2026.8.10", "2026.8.2", True),
                 ("2026.8.2", "2026.8.10", False),
                 ("2026.1.0", "2025.12.9", True),
                 ("2025.4.0", "2025.4.0", True)]
        for left, right, expected in table:
            script = (f'. {root}/bin/cloudflared-version\n'
                      f'if _cf_ge "{left}" "{right}"; then echo yes; else echo no; fi\n')
            done = subprocess.run(["bash", "-c", script],
                                  capture_output=True, text=True)
            self.assertEqual(done.stdout.strip(),
                             "yes" if expected else "no",
                             f"{left} >= {right}")

    def test_the_stack_file_names_no_connector_version_of_its_own(self):
        """
        A number typed into stacks/ingress.yml is a second answer to "which
        connector", and the second answer is the one that rots. The stack
        REQUIRES the variable rather than defaulting it, so a deploy that
        skipped resolution fails loudly instead of silently picking a version.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2]
        text = (root / "stacks" / "ingress.yml").read_text()
        image = re.search(r"^\s*image:\s*(\S+)", text, re.M)
        self.assertIsNotNone(image, "cloudflared has no image line")
        self.assertEqual(image.group(1), "cloudflare/cloudflared:${CLOUDFLARED_VERSION}",
                         "the connector version must be the resolved variable, "
                         "with no literal tag and no default beside it")

    def test_no_stack_defaults_a_variable_with_a_dash_in_the_message(self):
        """
        `docker stack deploy` reads the FIRST `-` inside a `${VAR:?message}` as
        the `${VAR-default}` separator, so the message becomes the value:
        `${PV:?has-a-dash}` resolves to `a-dash` with PV set. Measured on
        docker 29.7.2.

        It cost an ingress deploy: a message mentioning `bin/stack-deploy` made
        the connector image `cloudflare/cloudflared:deploy ingress ...` and the
        failure said `invalid reference format`, naming nothing. The form is not
        worth using at all here — a shell script can hold a sentence — so this
        catches the next person who reaches for it.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2]
        for stack in sorted((root / "stacks").glob("*.yml")):
            # Values only. Interpolation happens after the YAML is parsed, so a
            # comment may say `${PV:?has-a-dash}` — this one does — without
            # anything ever substituting it.
            body = "\n".join(line for line in stack.read_text().splitlines()
                              if not line.lstrip().startswith("#"))
            for message in re.findall(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?[?]([^}]*)\}",
                                      body):
                self.assertNotIn(
                    "-", message,
                    f"{stack.name}: '{message}' contains a dash, which docker's "
                    f"interpolation will treat as a default separator")

    def test_the_connector_hands_over_rather_than_stopping_first(self):
        """
        At the free floor there is exactly ONE connector — the master's — and
        Swarm's default update order is stop-first, which makes every connector
        bump a real outage on the one service all inbound traffic arrives
        through. With workers it is invisible, which is worse: it would be found
        the first time somebody updated a cluster that had scaled to zero.
        """
        import pathlib
        import yaml

        root = pathlib.Path(__file__).resolve().parents[2]
        deploy = yaml.safe_load((root / "stacks" / "ingress.yml").read_text())
        deploy = deploy["services"]["cloudflared"]["deploy"]
        self.assertEqual(deploy["update_config"]["order"], "start-first")
        self.assertEqual(deploy["update_config"]["failure_action"], "rollback")
        self.assertEqual(deploy["mode"], "global")

    def test_every_path_that_deploys_the_tunnel_resolves_the_version(self):
        """
        Four callers deploy ingress — bootstrap, the updater, the panel's
        redeploy button and an operator at a prompt — and they all go through
        bin/stack-deploy, so that is where resolution belongs. If it moved into
        the updater instead, the panel's button would deploy an unset variable.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        deploy = (root / "bin" / "stack-deploy").read_text()
        self.assertIn("bin/cloudflared-version", deploy)
        self.assertIn("cloudflared_version", deploy)
        self.assertIn("bin/cloudflared-version",
                      (root / "config" / "required-files").read_text(),
                      "a tree without the resolver deploys ingress with an unset "
                      "variable, and required-files is what refuses it first")

    def test_the_updater_looks_at_the_connector_on_a_tick_that_changes_nothing(self):
        """
        The whole point: cloudflared does not arrive with our commits, so a
        cluster whose code is current still needs its connector looked at. If
        the check only ran on the apply path, a repo that had not moved in three
        months would mean a connector three months stale.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        text = (root / "bin" / "infra-update").read_text()

        # The up-to-date path is everything before the line that announces an
        # apply. The connector has to be dealt with on that side of it.
        head, sep, tail = text.partition('log "running ${running:0:12}')
        self.assertTrue(sep, "infra-update no longer has an up-to-date path")
        self.assertIn("connector_sync", head,
                      "nothing looks at the connector before the up-to-date exit, "
                      "so a repo that has not moved leaves the tunnel stale")

        # And on the apply side, the version is refreshed BEFORE ingress is
        # deployed — refreshing it afterwards would need a second deploy to take
        # effect, which is a rolled tunnel for no reason.
        deploy_at = tail.index("stack-deploy\" ingress")
        self.assertIn("cloudflared_check", tail[:deploy_at],
                      "ingress is deployed before the connector version is refreshed")

    def test_both_copies_of_the_credential_repair_agree(self):
        """
        The rule that moves a pasted token into the password position is
        written twice — in `bin/infra-update`, and again in the cloud-init,
        because that boot is what INSTALLS bin/infra-update and cannot source
        it. Two copies of one rule is the drift this repo keeps getting bitten
        by, so they are run side by side against the same table rather than
        trusted to stay in step.
        """
        import pathlib
        import subprocess

        root = pathlib.Path(__file__).resolve().parents[2]

        def block(text, marker):
            """The `case ... esac` starting at `marker`, by its own indent."""
            lines = text.splitlines()
            start = next(i for i, ln in enumerate(lines) if marker in ln)
            indent = len(lines[start]) - len(lines[start].lstrip())
            for i in range(start + 1, len(lines)):
                if (lines[i].strip() == "esac"
                        and len(lines[i]) - len(lines[i].lstrip()) == indent):
                    return "\n".join(lines[start:i + 1])
            self.fail(f"no esac closes {marker!r}")

        copies = (
            ("bin/infra-update", 'case "$INFRA_REPO_URL" in', "INFRA_REPO_URL"),
            ("master-cloud-init.yaml", 'case "$clone_url" in', "clone_url"),
        )
        table = [
            ("https://github_pat_XYZ@github.com/o/r.git",
             "https://x-access-token:github_pat_XYZ@github.com/o/r.git"),
            ("https://me:github_pat_XYZ@github.com/o/r.git",
             "https://me:github_pat_XYZ@github.com/o/r.git"),
            ("https://github.com/o/r.git", "https://github.com/o/r.git"),
            # An '@' in the PATH is not userinfo and must not be treated as one.
            ("https://github.com/o/we@ird.git", "https://github.com/o/we@ird.git"),
            # Not http(s): left alone, or the rewrite corrupts it.
            ("git@github.com:o/r.git", "git@github.com:o/r.git"),
            ("ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
        ]
        for source, marker, var in copies:
            code = block((root / source).read_text(), marker)
            for given, want in table:
                with self.subTest(source=source, url=given):
                    out = subprocess.run(
                        ["bash", "-c",
                         f'{var}="$1"\n{code}\nprintf "%s" "${var}"',
                         "sh", given],
                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(out.returncode, 0, out.stderr)
                    self.assertEqual(out.stdout, want)

    def test_the_host_verb_has_nothing_to_smuggle_a_repo_through(self):
        """
        `bin/panel-hostops` is the panel's only reach onto the master, and its
        own header says never to add a verb taking a free-form string. This one
        takes no argument at all: the URL comes from infra.env, which the panel
        writes through its own bind mount, so the verb widens a compromised
        panel by exactly one `git ls-remote`.
        """
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        script = (root / "bin" / "panel-hostops").read_text()
        body = script[script.index("repo-check)"):]
        body = body[:body.index(";;")]
        self.assertNotIn("$arg", body)
        self.assertIn("--check", body)

    def test_the_deploy_button_does_not_wait_for_the_rollout(self):
        """
        It called the ATTACHED update, which waits for the whole rollout —
        parallelism x (monitor + delay) x replicas, minutes even for two
        replicas. This panel is served through Cloudflare, whose 100-second
        origin timeout cannot be raised, so the button returned a 524 error
        page every time while the deploy it was reporting on went on to
        succeed. That is what "manual deploy is not working" was: it worked,
        and said it had not.

        The webhook was fixed for exactly this reason and the button was left
        behind, so this asserts the button takes the same path.
        """
        csrf, _ = self.create_app("deployer")
        called = []
        real_sync = self.panel.data.deploy_image
        real_async = self.panel.data.deploy_image_async
        self.panel.data.deploy_image = lambda service, image: (
            called.append(("blocking", service, image)) or (True, ""))
        self.panel.data.deploy_image_async = lambda service, image: (
            called.append(("detached", service, image)) or (True, "accepted"))
        try:
            r = self.client.post("/components/deployer/action", data={
                "csrf": csrf, "action": "deploy-image",
                "image": "ghcr.io/you/app:sha-beef123"})
        finally:
            self.panel.data.deploy_image = real_sync
            self.panel.data.deploy_image_async = real_async

        self.assertEqual(302, r.status_code)
        self.assertEqual([c[0] for c in called], ["detached"])
        self.assertEqual(called[0][2], "ghcr.io/you/app:sha-beef123")

    def test_an_accepted_deploy_is_recorded_as_pending_not_done(self):
        """
        A detached update returns as soon as Swarm accepts it. Recording DONE
        there would be a green tick for a rollout that can still roll back —
        the exact lie the webhook's status model exists to remove.
        """
        csrf, _ = self.create_app("pendings")
        real = self.panel.data.deploy_image_async
        self.panel.data.deploy_image_async = lambda service, image: (True, "accepted")
        try:
            self.client.post("/components/pendings/action", data={
                "csrf": csrf, "action": "deploy-image",
                "image": "ghcr.io/you/app:sha-cafe456"})
        finally:
            self.panel.data.deploy_image_async = real
        last = self.panel.state.history("pendings")[0]
        self.assertEqual(last["status"], self.panel.state.PENDING)
        self.assertEqual(last["source"], "panel")

    def test_the_logs_tab_asks_for_the_depth_you_picked(self):
        self.create_app("noisy")
        asked = []
        real = self.panel.data.log_events
        self.panel.data.log_events = lambda service, lines=200, since_ns=None, log_filter=None: (
            asked.append(lines) or ([{"at": "", "text": "a line", "level": ""}],
                                    None, ""))
        try:
            self.client.get("/components/noisy?tab=logs")
            self.client.get("/components/noisy?tab=logs&lines=1000")
            # Not on the list, and not a number at all: both fall back rather
            # than reaching Loki with whatever was in the query string.
            self.client.get("/components/noisy?tab=logs&lines=999999")
            self.client.get("/components/noisy?tab=logs&lines=drop+table")
        finally:
            self.panel.data.log_events = real
        self.assertEqual(asked, [200, 1000, 200, 200])

    def test_the_logs_tab_offers_every_depth_it_accepts(self):
        """A choice the form offers and the route refuses is a dead control."""
        self.create_app("optioned")
        real = self.panel.data.log_events
        self.panel.data.log_events = lambda service, lines=200, since_ns=None, log_filter=None: (
            [{"at": "", "text": "a line", "level": ""}], None, "")
        try:
            page = self.client.get(
                "/components/optioned?tab=logs&lines=500").get_data(as_text=True)
        finally:
            self.panel.data.log_events = real
        for choice in self.panel.LOG_LINE_CHOICES:
            self.assertIn(f'value="{choice}"', page)
        self.assertIn('value="500" selected', page)


class LivePanelTest(PanelTest):
    """
    The panels that re-render themselves, and the one rule they all obey.

    Everything on the Overview except the cluster map used to be stale until
    somebody pressed reload; these fragments are what fixed that. They are also
    the thing most likely to be extended carelessly, which is what the last test
    here is for.
    """

    def test_every_fragment_needs_a_session(self):
        for fragment in self.panel.LIVE_FRAGMENTS:
            with self.subTest(fragment=fragment):
                r = self.client.get(f"/live/{fragment}")
                self.assertEqual(r.status_code, 302)
                self.assertIn("/login", r.headers["Location"])

    def test_an_unknown_fragment_is_not_a_template_name(self):
        """
        The route takes a KEY, not a path. If it ever took the template name it
        would let a URL render `_component_detail.html`, which prints
        credentials.
        """
        self.login()
        for probe in ("_component_detail.html", "../base.html", "nope"):
            self.assertEqual(self.client.get(f"/live/{probe}").status_code, 404)

    def test_the_fragments_render_what_the_pages_render(self):
        self.create_app("live")
        for fragment, path, probe in (
                ("overview-summary", "/", "Breaching SLO"),
                ("overview-lists", "/", "Alert rules"),
                ("observability", "/", "obs-card"),
                ("cluster", "/cluster", "panel"),
                ("components", "/components", "live"),
                ("alert-rules", "/alerts", "Rules")):
            with self.subTest(fragment=fragment):
                page = self.client.get(path).get_data(as_text=True)
                piece = self.client.get(f"/live/{fragment}").get_data(as_text=True)
                self.assertIn(probe, piece)
                # Rendered into the page too, so the view is complete with JS
                # off and the fragment only keeps it current.
                self.assertIn(probe, page)

    def test_no_live_fragment_contains_a_form(self):
        """
        A fragment replaces its host's markup underneath the reader. Doing that
        to a half-typed field destroys the edit, which is why the settings form,
        the alert targets and the node controls stayed on their pages while the
        panels around them moved into partials.
        """
        self.create_app("formless")
        for fragment in self.panel.LIVE_FRAGMENTS:
            if fragment == "node-tasks":
                continue                     # needs a node id; covered below
            with self.subTest(fragment=fragment):
                body = self.client.get(f"/live/{fragment}").get_data(as_text=True)
                self.assertNotIn("<form", body)

    def test_the_node_fragment_refuses_a_node_that_is_not_there(self):
        self.login()
        self.assertEqual(self.client.get("/live/node-tasks").status_code, 404)
        self.assertEqual(
            self.client.get("/live/node-tasks?node=nosuch").status_code, 404)

    def test_the_logs_fragment_only_returns_what_is_new(self):
        self.create_app("tailed")
        asked = []
        real = self.panel.data.log_events
        self.panel.data.log_events = lambda service, lines=200, since_ns=None, log_filter=None: (
            asked.append(since_ns) or ([{"at": "10:00:00", "text": "hello",
                                         "level": "err"}], 42, ""))
        try:
            body = self.client.get(
                "/components/tailed/logs?since=41").get_data(as_text=True)
        finally:
            self.panel.data.log_events = real
        self.assertEqual(asked, [41])
        # The cursor rides in a comment the follower strips, so a browser with
        # no JS renders the lines and nothing else.
        self.assertTrue(body.startswith("<!--cursor:42-->"))
        self.assertIn('class="line lvl-err"', body)

    def test_the_filter_reaches_loki_rather_than_the_lines_on_screen(self):
        """
        The whole point. A filter applied to the answer would narrow the two
        hundred lines already fetched; this one narrows the query, so Loki
        searches everything it has kept.
        """
        self.create_app("searched")
        seen = []
        real = self.panel.data.log_events
        self.panel.data.log_events = (
            lambda service, lines=200, since_ns=None, log_filter=None: (
                seen.append(log_filter) or ([], None, "")))
        try:
            self.client.get("/components/searched"
                            "?tab=logs&q=timeout&exclude=healthz&level=warn")
        finally:
            self.panel.data.log_events = real
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].logql().count("`"), 6)
        self.assertIn("|= `timeout`", seen[0].logql())
        self.assertIn("!= `healthz`", seen[0].logql())

    def test_the_follower_is_narrowed_the_same_way_as_the_page(self):
        """
        Otherwise the pane fills up with exactly the lines the filter was
        excluding, which reads as the filter not working.
        """
        self.create_app("followed")
        page = self.client.get(
            "/components/followed?tab=logs&q=boom&level=err").get_data(as_text=True)
        stream = page.split('data-logs-url="')[1].split('"')[0]
        self.assertIn("q=boom", stream)
        self.assertIn("level=err", stream)

        seen = []
        real = self.panel.data.log_events
        self.panel.data.log_events = (
            lambda service, lines=200, since_ns=None, log_filter=None: (
                seen.append((since_ns, log_filter.contains)) or ([], None, "")))
        try:
            self.client.get(stream.replace("&amp;", "&") + "&since=5")
        finally:
            self.panel.data.log_events = real
        self.assertEqual(seen, [(5, "boom")])

    def test_a_broken_regex_shows_a_message_instead_of_a_stack_trace(self):
        self.create_app("badregex")
        page = self.client.get(
            "/components/badregex?tab=logs&q=a(&regex=on").get_data(as_text=True)
        self.assertIn("not a valid regex", page)

    def test_a_filter_that_is_on_offers_a_way_back_off(self):
        """A narrowing with no visible undo is a page people reload to escape."""
        self.create_app("clearable")
        page = self.client.get(
            "/components/clearable?tab=logs&q=x").get_data(as_text=True)
        self.assertIn(">Clear<", page)
        plain = self.client.get(
            "/components/clearable?tab=logs").get_data(as_text=True)
        self.assertNotIn(">Clear<", plain)

    def test_the_filter_survives_a_reload_because_it_is_in_the_url(self):
        self.create_app("bookmarked")
        page = self.client.get(
            "/components/bookmarked?tab=logs&q=needle&regex=on").get_data(as_text=True)
        self.assertIn('value="needle"', page)
        self.assertIn('name="regex" checked', page)

    def test_a_junk_cursor_is_a_first_page_not_an_error(self):
        self.create_app("junked")
        asked = []
        real = self.panel.data.log_events
        self.panel.data.log_events = lambda service, lines=200, since_ns=None, log_filter=None: (
            asked.append(since_ns) or ([], None, ""))
        try:
            for query in ("", "?since=", "?since=drop+table", "?since=-1"):
                self.client.get(f"/components/junked/logs{query}")
        finally:
            self.panel.data.log_events = real
        self.assertEqual(asked, [None, None, None, None])


class DeployDiffTest(PanelTest):
    """
    Saving something that changes nothing must not be a deploy.

    Swarm already left an unchanged service alone; what nobody could see was
    WHICH services a save would touch, so every save read as a whole-stack
    event and a save that changed nothing still ran one.
    """

    def test_an_identical_save_deploys_nothing(self):
        csrf, _ = self.create_app("steady")
        component = self.running("steady")
        component.write_stack()              # stand in for the deploy we stubbed
        self.deploys.clear()
        page = self.client.post("/components/steady/settings",
                                data=dict(self._spec_form(csrf, component)),
                                follow_redirects=True).get_data(as_text=True)
        self.assertEqual(self.deploys, [])
        self.assertIn("Nothing to redeploy", page)

    def test_a_real_change_deploys_and_names_what_moves(self):
        csrf, _ = self.create_app("moving")
        component = self.running("moving")
        component.write_stack()
        self.deploys.clear()
        form = dict(self._spec_form(csrf, component))
        form["memory_reservation_mb"] = "512"
        page = self.client.post("/components/moving/settings", data=form,
                                follow_redirects=True).get_data(as_text=True)
        self.assertEqual(self.deploys, ["moving"])
        self.assertIn("Rolling moving_app", page)

    def test_saving_a_stopped_component_does_not_start_it(self):
        """
        Save is not Deploy. A stopped component has no services to update, so
        `docker stack deploy` would CREATE them — pressing Save on the
        environment form used to bring the whole stack up. The change belongs
        on disk either way; applying it is the Deploy button's job.
        """
        csrf, _ = self.create_app("parked")          # never added to the cluster
        self.deploys.clear()
        page = self.client.post("/components/parked/env", follow_redirects=True,
                                data={"csrf": csrf, "bulk": "A=1\n"}).get_data(as_text=True)
        self.assertEqual(self.deploys, [])
        self.assertIn("not running", page)
        self.assertEqual(self.components.store.env_map("parked"), {"A": "1"})

    def test_a_removal_is_named_before_it_happens(self):
        """
        `--prune` DELETES a service the render no longer emits. That is the one
        outcome on this page worth reading twice, so it is stated first and is
        never abbreviated into "and 3 more".
        """
        base = self.components.base
        sentence = base.describe_changes(
            {"removed": ["c_redis-4"], "added": [], "changed": ["c_redis-1"],
             "unchanged": ["c_sentinel-1"]})
        self.assertTrue(sentence.startswith("Removing c_redis-4"))
        self.assertIn("Rolling c_redis-1", sentence)
        self.assertIn("Leaving c_sentinel-1 alone", sentence)

    def test_an_unknown_diff_is_never_read_as_nothing_to_do(self):
        """
        Skipping a deploy we could not prove was unnecessary would be a
        component that silently ignores your edit. Deploying anyway costs
        seconds.
        """
        base = self.components.base
        self.assertFalse(base.nothing_to_do(None))
        self.assertTrue(base.nothing_to_do(
            {"added": [], "removed": [], "changed": [], "unchanged": ["a"]}))

    def test_a_first_deploy_is_all_new_rather_than_all_unchanged(self):
        self.create_app("fresh")
        component = self.components.load("fresh")
        diff = component.pending_changes()
        self.assertEqual(diff["added"], ["fresh_app"])
        self.assertEqual(diff["unchanged"], [])

    def _spec_form(self, csrf, component):
        """The settings form as the browser would post it back, unchanged."""
        form = {"csrf": csrf}
        for field in type(component).fields():
            value = component.spec.get(field.name)
            if field.kind == "bool":
                if value:
                    form[field.name] = "on"
            elif value is not None:
                form[field.name] = str(value)
        return form


class DatabaseHarness:
    """
    What a database test needs, without what other database tests assert.

    A mixin rather than a base class with tests in it: subclassing
    `DatabasePanelTest` to reuse `make_mongo` re-runs its entire suite under the
    subclass's name, which is a hundred duplicated tests per new class and a
    genuinely confusing failure report when one of the INHERITED ones breaks.
    """

    def setUp(self):
        super().setUp()
        # No docker daemon here, and the database renderer shells out for Swarm
        # secrets. Stubbing the two shells keeps these tests about the panel.
        from components import base
        self._shells = (base.run, base.docker_out)
        base.run = lambda argv, timeout=600, stdin=None: (True, "")
        base.docker_out = lambda argv: ""

    def tearDown(self):
        from components import base
        base.run, base.docker_out = self._shells
        super().tearDown()

    def make_mongo(self, name="docs", expect=200, **spec):
        """
        A managed mongo through the real route.

        `_form_spec` reads a bool as "was this key on the form at all", so a
        field is turned OFF by being ABSENT — passing an empty string would post
        the key and read as True, which is the trap that makes a test claim a
        visualiser is off while the component has one.
        """
        csrf = self.login()
        bools = {"exporter": True, "visualizer": True,
                 "dataguard": True, "secondary_reads": True}
        for key in list(bools):
            if key in spec:
                bools[key] = bool(spec.pop(key))
        form = {"csrf": csrf, "type": "mongo", "name": name,
                "version": "7.0",
                "username": "root", "cache_mb": "256", "cpu_reservation": "0.3",
                "memory_reservation_mb": "768", "placement_mode": "auto",
                "lag_budget_seconds": "10", "max_replica_lag_alert": "60",
                "backup_interval_hours": "24",
                "__bool__": list(bools)}
        form.update({k: "true" for k, on in bools.items() if on})
        form.update(spec)
        r = self.client.post("/components", data=form, follow_redirects=True)
        # `expect` so a test can assert the form REFUSES something. A helper that
        # can only express success cannot test a validation rule at all.
        self.assertEqual(r.status_code, expect)
        return csrf

    def make_redis_component(self, name="cache", **spec):
        """A managed redis with a visualiser, through the real route."""
        csrf = self.login()
        form = {"csrf": csrf, "type": "redis", "name": name,
                "version": "7.4-alpine",
                "maxmemory_mb": "256", "maxmemory_policy": "allkeys-lru",
                "placement_mode": "auto", "lag_budget_seconds": "10",
                "__bool__": ["exporter", "visualizer", "dataguard", "appendonly"],
                "visualizer": "true", "dataguard": "true"}
        form.update(spec)
        r = self.client.post("/components", data=form, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        return self.components.load(name)

    # --- the invariant ----------------------------------------------------

class DatabasePanelTest(DatabaseHarness, PanelTest):
    """
    The surfaces the database work added: the placement/manager invariant, the
    Dataguard tab, Storage, and the visualiser proxy.
    """

    def test_pinning_the_placement_turns_the_manager_off(self):
        """
        The two are one decision wearing two hats: dataguard's whole first move
        is putting a replica on another machine, so a component pinned to the
        master is one it cannot manage. Saving both would be the form promising
        something the next loop breaks.
        """
        csrf = self.make_mongo("pinned")
        self.client.post("/components/pinned/settings", follow_redirects=True, data={
            "csrf": csrf, "placement_mode": "master",
            "__bool__": ["dataguard"], "dataguard": "true"})
        spec = self.components.load("pinned").spec
        self.assertEqual(spec["placement_mode"], "master")
        self.assertFalse(spec["dataguard"])

    def test_turning_the_manager_on_sets_the_placement_back_to_auto(self):
        """The other direction, and it must be the other direction: whichever
        of the two you just touched is the more recent intent."""
        csrf = self.make_mongo("swung", placement_mode="master", dataguard=False)
        self.client.post("/components/swung/settings", follow_redirects=True, data={
            "csrf": csrf, "__bool__": ["dataguard"], "dataguard": "true"})
        spec = self.components.load("swung").spec
        self.assertEqual(spec["placement_mode"], "auto")
        self.assertTrue(spec["dataguard"])

    def test_the_cli_gets_the_same_rule(self):
        """
        `bin/component` imports the same package, so the rule has to live below
        the route or the two surfaces disagree about what was saved.
        """
        self.make_mongo("viacli")
        component, problems = self.components.update(
            "viacli", {"placement_mode": "master"})
        self.assertEqual(problems, [])
        self.assertFalse(component.spec["dataguard"])

    # --- the form ---------------------------------------------------------

    def test_the_database_groups_render_on_both_surfaces(self):
        """
        The create form and the Settings tab are one partial for exactly this
        reason. A group that appeared on one of them is the drift that made the
        autoscale policy show twelve live-looking inputs on a component whose
        autoscaling was off.
        """
        self.make_mongo("bothways")
        create = self.client.get("/components/new?type=mongo").get_data(as_text=True)
        settings = self.client.get("/components/bothways?tab=settings").get_data(as_text=True)
        for marker in ("Observability", "Dataguard", "f-visualizer",
                       "f-secondary_reads", "data-placement-sync"):
            self.assertIn(marker, create, marker)
            self.assertIn(marker, settings, marker)

    def test_the_exporter_is_a_switch_now_and_not_a_checkbox(self):
        self.login()
        create = self.client.get("/components/new?type=mongo").get_data(as_text=True)
        self.assertIn('id="f-exporter"', create)
        chunk = create[create.index('id="f-exporter"') - 400:create.index('id="f-exporter"') + 200]
        self.assertIn("switch-track", chunk)

    # --- the dataguard tab ------------------------------------------------

    def test_the_manager_page_has_a_dataguard_tab_with_its_own_policy(self):
        self.login()
        page = self.client.get("/manager?tab=dataguard").get_data(as_text=True)
        self.assertIn('data-tab="dataguard"', page)
        self.assertIn("TOPOLOGY_COOLDOWN_SECONDS", page)
        # And the reason a database is not growing, which is what people open
        # this page to find out.
        self.assertIn("What is being held back", page)

    def test_dataguard_policy_is_not_also_on_the_settings_page(self):
        """Two editors for one value is two values that can disagree."""
        self.login()
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertNotIn('name="value__TOPOLOGY_COOLDOWN_SECONDS"', page)

    # --- storage ----------------------------------------------------------

    def test_storage_is_in_the_nav_between_alerts_and_settings(self):
        self.login()
        page = self.client.get("/storage").get_data(as_text=True)
        self.assertIn("Storage", page)
        rail = page[page.index('class="rail"'):page.index("rail-foot")]
        self.assertLess(rail.index('data-nav="alerts"'), rail.index('data-nav="storage"'))
        self.assertLess(rail.index('data-nav="storage"'), rail.index('data-nav="settings"'))

    def test_a_storage_credential_never_lands_in_the_definition_file(self):
        """
        The file is 0600 and it is still the wrong place: a backup agent runs on
        a machine the panel cannot write to, so the credential has to be a Swarm
        secret — and once it is, keeping a copy here is a copy to leak.
        """
        import storage as storage_store
        problems = storage_store.save(
            {"name": "s3main", "kind": "s3", "bucket": "b", "endpoint": "https://x"},
            "AKIAEXAMPLE", "supersecret")
        self.assertEqual(problems, [])
        with open(storage_store.PATH) as fh:
            body = fh.read()
        self.assertNotIn("supersecret", body)
        self.assertNotIn("AKIAEXAMPLE", body)

    def test_a_target_a_component_still_uses_cannot_be_removed(self):
        import storage as storage_store
        storage_store.save({"name": "s3main", "kind": "s3", "bucket": "b"})
        problems = storage_store.remove("s3main", used_by=["docs"])
        self.assertTrue(problems)
        self.assertIn("docs", problems[0])
        self.assertIn("s3main", storage_store.names())

    def test_a_plaintext_endpoint_is_called_out(self):
        import storage as storage_store
        problems = storage_store.check(
            {"name": "s3main", "kind": "s3", "bucket": "b", "endpoint": "http://x"})
        self.assertTrue(any("clear text" in p for p in problems))

    def test_a_copy_inside_this_cluster_is_not_offered_as_a_backup(self):
        """
        It survives a container and a disk; it does not survive a mistake, a
        deleted project or a compromised account — which are the three things
        people mean when they say they have backups. Offering it would be the
        panel implying otherwise.
        """
        import storage as storage_store
        self.assertEqual(storage_store.KINDS, ("s3",))
        self.assertTrue(storage_store.check(
            {"name": "spare", "kind": "node", "constraint": "node.labels.backup == true"}))

    # --- alert targets ----------------------------------------------------

    def test_alert_destinations_are_a_list_not_two_settings_rows(self):
        """
        They were two rows on the Settings page, which made "who gets alerted" a
        property of the cluster rather than a thing you have any of: no second
        channel, no deliberate none, and a second KIND meant hand-editing YAML.
        """
        import settings_def
        self.assertNotIn("ALERT_TELEGRAM_BOT_TOKEN", settings_def.FIELDS)
        self.assertNotIn("ALERT_TELEGRAM_CHAT_ID", settings_def.FIELDS)
        self.login()
        page = self.client.get("/alerts").get_data(as_text=True)
        # Above the rules, because a rule with nowhere to go is the state this
        # page exists to make visible, and you should meet it before the list of
        # things that will not reach you.
        self.assertIn("<h2>Targets</h2>", page)
        self.assertLess(page.index("<h2>Targets</h2>"), page.index("<h2>Rules</h2>"))

    def test_a_target_with_no_token_is_refused(self):
        import alerttargets
        self.assertTrue(alerttargets.check(
            {"name": "team", "kind": "telegram", "chat_id": "-100"}))

    def test_the_form_does_not_second_guess_the_credential(self):
        """
        The token is whatever BotFather issued, and nothing here second-guesses
        it. There was a regex doing so and it rejected correctly pasted tokens,
        which is a worse failure than anything it prevented: "that does not look
        like a token" when it plainly is one leaves the operator nowhere to go.

        The YAML safety that regex was justified by belongs to the renderer's
        quoting, which covers every input rather than the predicted ones. The
        chat id is the one exception and it is not a guess — see CHAT_RE.
        """
        import alerttargets
        for chat in ("-1001234567890", "12345"):
            self.assertEqual([], alerttargets.check(
                {"name": "team", "kind": "telegram",
                 "bot_token": "8140000000:AAF-whatever-botfather-said",
                 "chat_id": chat}), chat)

    def test_a_name_is_not_forced_into_a_slug(self):
        """
        The name never reaches the generated config — it is a label on a page.
        Demanding lowercase-and-dashes of it was a rule with nothing behind it.
        """
        import alerttargets
        self.assertEqual([], alerttargets.check(
            {"name": "Team Alerts (on call)", "kind": "telegram",
             "bot_token": "8140000000:AAF-whatever", "chat_id": "-100"}))

    def test_the_token_can_be_read_back(self):
        """
        The panel stored this for you; being unable to read back what you pasted
        makes "is this the right token" unanswerable without a shell on the
        master. It is behind the same click-to-reveal as every other secret the
        panel shows.
        """
        import alerttargets
        token = "8140000000:AAF-this-is-the-secret-part"
        self.assertEqual([], alerttargets.save(
            {"name": "team", "kind": "telegram", "bot_token": token,
             "chat_id": "-100"}))
        [row] = alerttargets.described()
        self.assertEqual(token, row["token"])

    # --- proving it works, now rather than tomorrow -----------------------

    def _adding_a_target(self, reply, status=200, raises=None):
        """
        Add a target through the real route with Telegram's reply faked.

        Returns (flash messages, what was POSTed to Telegram).
        """
        import alerttargets
        import envstore
        sent = {}

        class Reply:
            status_code = status

            @staticmethod
            def json():
                if isinstance(reply, Exception):
                    raise reply
                return reply

        def post(url, json=None, timeout=None):
            sent["url"] = url
            sent["json"] = json
            if raises:
                raise raises
            return Reply()

        saved = (alerttargets.requests.post, envstore.deploy_stack)
        alerttargets.requests.post = post
        envstore.deploy_stack = lambda name: (True, f"{name} redeployed")
        self.addCleanup(alerttargets.remove, "Team Alerts")
        csrf = self.login()
        try:
            page = self.client.post(
                "/alerts/targets",
                data={"csrf": csrf, "name": "Team Alerts", "kind": "telegram",
                      "bot_token": "8140000000:AAF-the-secret-part",
                      "chat_id": "-1001234567890"},
                follow_redirects=True).get_data(as_text=True)
        finally:
            alerttargets.requests.post, envstore.deploy_stack = saved
        return page, sent

    def test_the_form_does_not_reimpose_the_rule_in_the_browser(self):
        """
        `pattern="[a-z][a-z0-9-]{1,31}"` was sitting on this input, which is the
        browser-side half of the name rule that was removed from `check`. With
        it still here the server-side fix is invisible: "Team Alerts" never gets
        as far as being accepted.
        """
        self.login()
        page = self.client.get("/alerts").get_data(as_text=True)
        form = page.split('action="/alerts/targets"')[1]
        self.assertNotIn("pattern=", form.split("</form>")[0])

    def test_adding_a_target_sends_it_a_message_immediately(self):
        """
        Alertmanager already sends a Watchdog, and its 24h repeat means "did I
        paste the right token" was up to a day from being answered — a day of
        believing you are covered. This asks Telegram directly, on the way past.
        """
        page, sent = self._adding_a_target({"ok": True})
        self.assertIn("8140000000:AAF-the-secret-part", sent["url"])
        self.assertEqual(sent["json"]["chat_id"], "-1001234567890")
        self.assertIn("Watchdog", sent["json"]["text"])
        self.assertIn("Team Alerts", sent["json"]["text"])
        self.assertIn("Test message sent to Team Alerts", page)
        # And it is still saved and deployed, not replaced by the test.
        import alerttargets
        self.assertIn("Team Alerts", alerttargets.names())

    def test_a_target_telegram_rejects_is_kept_and_the_reason_is_shown(self):
        """
        `chat not found` is the whole diagnosis and it is Telegram's own words —
        no wrapper of ours improves on it. The target stays: somebody meant to
        create it, and throwing away a pasted credential over one API reply is
        the same second-guessing that made this page unusable before.
        """
        page, _ = self._adding_a_target({"ok": False, "description": "chat not found"})
        self.assertIn("did NOT go out", page)
        self.assertIn("chat not found", page)
        import alerttargets
        self.assertIn("Team Alerts", alerttargets.names())

    def test_the_bot_token_never_reaches_the_error_message(self):
        """
        The token is IN the URL for this API, and `requests` puts the URL in its
        exception messages — so an unreachable api.telegram.org would print the
        bot token into a flash message, the browser, and the panel's log. It is
        redacted on the way out.
        """
        token = "8140000000:AAF-the-secret-part"
        page, _ = self._adding_a_target(
            None, raises=RuntimeError(
                f"HTTPSConnectionPool: /bot{token}/sendMessage failed"))
        # The banner only. The token IS elsewhere on this page, behind the same
        # click-to-reveal as every other secret the panel shows — that is
        # deliberate and is a different test. What must not happen is it leaking
        # into a message that also goes to the log.
        banner = page.split('<div class="banner bad">')[1].split("</div>")[0]
        self.assertIn("could not reach Telegram", banner)
        self.assertNotIn(token, banner)
        self.assertIn("&lt;token&gt;", banner)


    # --- the database map --------------------------------------------------

    def test_a_database_map_is_coloured_by_role_not_by_image_tag(self):
        """
        Every member runs the identical image, so a tag would be the same word
        on every block while the thing worth seeing — which machine takes
        writes — would be nowhere on the page.
        """
        import shape
        topo = {"nodes": [
            {"hostname": "master", "tasks": [
                {"service": "docs_mongo-1", "tag": "7.0", "id": "a"}]},
            {"hostname": "db-1", "tasks": [
                {"service": "docs_mongo-2", "tag": "7.0", "id": "b"}]},
        ]}
        built = shape.component_map(
            topo, ["docs_mongo-1", "docs_mongo-2"],
            roles={"docs_mongo-1": "SECONDARY", "docs_mongo-2": "PRIMARY"})
        self.assertTrue(built["by_role"])
        keys = {t["tag"]: t["key"] for t in built["tags"]}
        self.assertEqual(keys["PRIMARY"], "primary")
        self.assertEqual(keys["SECONDARY"], "secondary")
        # Primary FIRST however few there are of it: "which one is primary" is
        # the whole question, and ranking by count would bury it under three
        # secondaries.
        self.assertEqual(built["tags"][0]["tag"], "PRIMARY")

    def test_a_member_nobody_reported_on_is_unknown_not_a_secondary(self):
        """
        Calling it a secondary would draw a set that looks healthier than it is,
        and "no primary" and "nobody is watching" are different problems.
        """
        import shape
        topo = {"nodes": [{"hostname": "db-1", "tasks": [
            {"service": "docs_mongo-3", "tag": "7.0", "id": "c"}]}]}
        built = shape.component_map(topo, ["docs_mongo-3"], roles={})
        self.assertEqual(built["tags"][0]["tag"], "unknown")

    def test_an_application_map_still_colours_by_tag(self):
        import shape
        topo = {"nodes": [{"hostname": "w1", "tasks": [
            {"service": "api_app", "tag": "sha-abc", "id": "a"},
            {"service": "api_app", "tag": "sha-def", "id": "b"}]}]}
        built = shape.component_map(topo, ["api_app"])
        self.assertFalse(built["by_role"])
        self.assertEqual({t["tag"] for t in built["tags"]}, {"sha-abc", "sha-def"})

    def test_every_database_type_has_a_map_tab(self):
        self.make_mongo("mapped")
        page = self.client.get("/components/mapped?tab=map").get_data(as_text=True)
        self.assertIn('data-tab="map"', page)


    # --- the visualiser ---------------------------------------------------

    def test_the_viewer_proxy_is_refused_when_the_component_has_none(self):
        """
        404, not 403: a component without a visualiser has no such URL at all,
        and saying "forbidden" would confirm the path exists for the ones that do.
        """
        self.make_mongo("noviewer", visualizer=False)
        self.assertEqual(self.client.get("/components/noviewer/viewer/").status_code, 404)

    def test_the_viewer_proxy_needs_a_session(self):
        self.client.get("/logout")
        with self.client.session_transaction() as sess:
            sess.clear()
        r = self.client.get("/components/docs/viewer/")
        self.assertIn(r.status_code, (302, 401, 404))

    def test_the_panel_session_cookie_is_not_forwarded_to_the_visualiser(self):
        """
        The visualiser is a third-party image with unrestricted access to a
        database, reached through a page you are signed in to — so the browser
        attaches the panel's session cookie to every request on this origin,
        including these. Forwarding that upstream hands a data console the
        bearer token for the console that runs the cluster.

        Its OWN cookies still have to reach it or it cannot keep a session.
        """
        import app as panel
        self.make_mongo("proxied")

        seen = {}

        class Upstream:
            status_code = 200
            headers = {"Content-Type": "text/plain"}

            @staticmethod
            def iter_content(chunk_size=0):
                return iter([b"ok"])

        def fake_request(method, url, **kw):
            seen.update(kw.get("headers") or {})
            seen["_url"] = url
            return Upstream()

        real_request = panel.requests.request
        real_ensure = panel.data.ensure_viewer
        panel.requests.request = fake_request
        panel.data.ensure_viewer = lambda service: (True, "")
        try:
            self.client.set_cookie("theirs", "redisinsight-session")
            r = self.client.get("/components/proxied/viewer/api/info")
        finally:
            panel.requests.request = real_request
            panel.data.ensure_viewer = real_ensure

        self.assertEqual(200, r.status_code)
        forwarded = {k.lower(): v for k, v in seen.items() if k != "_url"}
        name = panel.app.config["SESSION_COOKIE_NAME"]
        self.assertNotIn(f"{name}=", forwarded.get("cookie", ""))
        self.assertNotIn("authorization", forwarded)
        # ...and theirs still arrives, or the console cannot hold a session.
        self.assertIn("theirs=redisinsight-session", forwarded.get("cookie", ""))

        # The case the rebuild alone does NOT cover: when the panel session is
        # the only cookie there is nothing to rebuild the header from, so it has
        # to have been dropped rather than replaced.
        seen.clear()
        self.client.delete_cookie("theirs")
        panel.requests.request = fake_request
        panel.data.ensure_viewer = lambda service: (True, "")
        try:
            self.client.get("/components/proxied/viewer/api/info")
        finally:
            panel.requests.request = real_request
            panel.data.ensure_viewer = real_ensure
        alone = {k.lower(): v for k, v in seen.items() if k != "_url"}
        self.assertNotIn("cookie", alone)

    def test_the_console_is_asked_for_the_path_it_was_told_it_lives_at(self):
        """
        The prefix is not ours to strip.

        Both consoles are configured to serve UNDER this path — RedisInsight by
        `RI_PROXY_PATH`, mongo-express by `ME_CONFIG_SITE_BASEURL` — because
        that is what makes their asset URLs land back here instead of colliding
        with the panel's own `/static`. Forwarding the stripped remainder asked
        RedisInsight for `/` and got `{"message":"Cannot GET /"}`; the same
        container answers 200 at the full path. A redirect comes back carrying
        that prefix already, so the rewrite drops the origin and keeps the path
        rather than prepending it a second time.
        """
        import app as panel
        self.make_mongo("prefixed")
        seen = {}

        class Upstream:
            status_code = 302
            headers = {"Location": "http://prefixed_viewer:8081"
                                   "/components/prefixed/viewer/db/admin"}

            @staticmethod
            def iter_content(chunk_size=0):
                return iter([b""])

        real_request, real_ensure = panel.requests.request, panel.data.ensure_viewer
        panel.requests.request = lambda method, url, **kw: (
            seen.update(url=url) or Upstream())
        panel.data.ensure_viewer = lambda service: (True, "")
        try:
            r = self.client.get("/components/prefixed/viewer/api/info")
        finally:
            panel.requests.request, panel.data.ensure_viewer = real_request, real_ensure

        self.assertTrue(seen["url"].endswith("/components/prefixed/viewer/api/info"),
                        seen["url"])
        self.assertEqual("/components/prefixed/viewer/db/admin",
                         r.headers["Location"])

    def test_the_open_button_goes_where_grafana_actually_is(self):
        """
        The Open button kept pointing at `https://grafana-<app>.<root>` after
        Grafana moved behind the panel, so removing that tunnel hostname — the
        entire point of the move — turned the button into a dead link.

        The catalog's own comment says an Open button that leads nowhere is
        worse than saying a service is not published, and a hostname somebody
        has to remember to create in a dashboard is exactly that: the button
        pointed at it whether or not it existed. So the assertion is not that
        the URL reads a certain way, it is that the panel WILL SERVE IT — the
        button and the route have to be the same fact.
        """
        import app as panel
        entry = next(e for e in panel.catalog.SYSTEM if e["key"] == "grafana")
        access = panel.system_access(entry)
        self.assertTrue(access["reachable"])
        self.assertEqual(access["url"], "/grafana/")

        self.login()
        reached = []

        class Upstream:
            status_code = 200
            headers = {}

            @staticmethod
            def iter_content(chunk_size=0):
                return iter([b"grafana"])

        real = panel.requests.request
        panel.requests.request = lambda method, url, **kw: (
            reached.append(url) or Upstream())
        try:
            r = self.client.get(access["url"])
        finally:
            panel.requests.request = real

        self.assertEqual(r.status_code, 200)
        self.assertEqual(reached, ["http://grafana:3000/grafana/"])

    def test_no_infrastructure_ui_promises_a_hostname_nobody_creates(self):
        """
        Every remaining tunnel link has to be one the setup actually tells you
        to add. Only the panel's own hostname qualifies now.
        """
        import app as panel
        tunnelled = [e["key"] for e in panel.catalog.SYSTEM
                     if (e.get("ui") or {}).get("kind") == panel.catalog.UI_TUNNEL]
        self.assertEqual(tunnelled, ["admin"], tunnelled)

    def test_a_login_through_the_proxy_keeps_every_cookie_it_was_given(self):
        """
        Grafana said "logged in" and the next page was the login page again.

        A login sets more than one cookie — `grafana_session` and
        `grafana_session_expiry` — and each is its own `Set-Cookie` header.
        `requests` exposes response headers as a mapping, which folds repeats
        into one comma-joined value, and RFC 6265 forbids exactly that: a
        cookie value may contain a comma, so nothing downstream can split them
        back apart. The browser read one cookie whose attributes were the
        second cookie's text, kept nothing usable, and asked to log in again.

        So the proxy reads the raw headers, where each occurrence is separate,
        and ADDS them rather than assigning — assigning keeps only the last.
        """
        import app as panel
        self.login()
        sent = ["grafana_session=abc; Path=/grafana; HttpOnly; SameSite=Lax",
                "grafana_session_expiry=1788000541; Path=/grafana; SameSite=Lax"]

        class Raw:
            # What urllib3 hands back: repeats preserved, one item each.
            class headers:
                @staticmethod
                def items():
                    return [("Content-Type", "application/json")] + [
                        ("Set-Cookie", c) for c in sent]

        class Upstream:
            status_code = 200
            raw = Raw
            # The FOLDED view, which is what the bug read. If the proxy ever
            # goes back to this, the test fails rather than quietly passing.
            headers = {"Content-Type": "application/json",
                       "Set-Cookie": ", ".join(sent)}

            @staticmethod
            def iter_content(chunk_size=0):
                return iter([b"{}"])

        real = panel.requests.request
        panel.requests.request = lambda method, url, **kw: Upstream()
        try:
            r = self.client.post("/grafana/login", json={"user": "admin"})
        finally:
            panel.requests.request = real

        # The panel refreshes its own session cookie on the way out too; the
        # upstream's are the ones under test.
        got = [c for c in r.headers.getlist("Set-Cookie")
               if not c.startswith(panel.app.config["SESSION_COOKIE_NAME"] + "=")]
        self.assertEqual(len(got), 2, got)
        self.assertTrue(any(c.startswith("grafana_session=") for c in got), got)
        self.assertTrue(any(c.startswith("grafana_session_expiry=") for c in got), got)

    def test_grafana_is_reachable_through_the_panel_session(self):
        """
        Grafana used to be reachable one way only — a `grafana-<app>.<root>`
        hostname on the tunnel, which is a second public login page guarding
        every metric this cluster has recorded. It now goes through the same
        proxy the database consoles use, at its own full path, because Grafana
        is told it serves under that prefix.
        """
        import app as panel
        self.login()
        seen = {}

        class Upstream:
            status_code = 200
            headers = {"Content-Type": "text/html"}

            @staticmethod
            def iter_content(chunk_size=0):
                return iter([b"<html>"])

        real = panel.requests.request
        panel.requests.request = lambda method, url, **kw: (
            seen.update(url=url, headers=kw.get("headers") or {}) or Upstream())
        try:
            r = self.client.get("/grafana/d/abc/dashboard")
        finally:
            panel.requests.request = real

        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen["url"], "http://grafana:3000/grafana/d/abc/dashboard")

    def test_the_panel_session_cookie_never_reaches_grafana(self):
        """
        Same rule as the database consoles, and the same reason: the browser
        attaches this origin's cookie to every request on this route, and that
        cookie is the key to the console that runs the cluster. Grafana's own
        cookie has to survive, or it cannot hold a login.
        """
        import app as panel
        self.login()
        seen = {}

        class Upstream:
            status_code = 200
            headers = {}

            @staticmethod
            def iter_content(chunk_size=0):
                return iter([b""])

        self.client.set_cookie("grafana_session", "theirs")
        real = panel.requests.request
        panel.requests.request = lambda method, url, **kw: (
            seen.update(kw.get("headers") or {}) or Upstream())
        try:
            self.client.get("/grafana/api/org")
        finally:
            panel.requests.request = real

        cookie = {k.lower(): v for k, v in seen.items()}.get("cookie", "")
        # By NAME. `grafana_session=` contains `session=`, so a substring check
        # here passes whether or not the panel's cookie was actually dropped —
        # which is the one thing this test exists to prove.
        names = [part.split("=", 1)[0].strip() for part in cookie.split(";")]
        self.assertIn("grafana_session", names)
        self.assertNotIn(panel.app.config["SESSION_COOKIE_NAME"], names)
        self.assertIn("grafana_session=theirs", cookie)

    def test_grafana_is_told_the_prefix_the_panel_serves_it_at(self):
        """
        THE TWO HAVE TO AGREE. The route forwards the full path, so Grafana has
        to be the kind of Grafana that serves there — `serve_from_sub_path`
        without a matching `root_url` (or either one alone) gives a page whose
        assets resolve to `/public/...`, which is the panel's origin and a 404.
        """
        with open(os.path.join(os.path.dirname(_ADMIN), "stacks", "monitoring.yml")) as fh:
            monitoring = fh.read()
        self.assertIn('GF_SERVER_SERVE_FROM_SUB_PATH: "true"', monitoring)
        self.assertIn("/grafana/", monitoring.split("GF_SERVER_ROOT_URL:")[1]
                      .splitlines()[0])

    def test_opening_a_redis_console_registers_the_database_for_you(self):
        """
        RedisInsight opened on an empty "add a database" form.

        `RI_REDIS_HOST`, `RI_REDIS_PORT` and `RI_REDIS_PASSWORD` were set on the
        service and are read by NOTHING in that image — the only bootstrap it
        implements is `RI_REDIS_STACK_DATABASE_*`, which applies to the bundled
        Redis Stack build and has no password field, so it cannot describe a
        server with `requirepass`. Its REST API can, and that is what the
        console's own form uses.

        Landing request only, and only when it has nothing yet, so reopening it
        does not accumulate duplicates of the same server.
        """
        import app as panel
        component = self.make_redis_component("consoled")
        calls = []

        class Upstream:
            status_code = 200
            headers = {"Content-Type": "text/html"}

            @staticmethod
            def iter_content(chunk_size=0):
                return iter([b"<html>"])

        real = (panel.requests.request, panel.requests.get, panel.requests.post,
                panel.data.ensure_viewer)
        panel.requests.request = lambda method, url, **kw: Upstream()
        panel.requests.get = lambda url, **kw: type(
            "R", (), {"json": staticmethod(lambda: [])})()
        panel.requests.post = lambda url, **kw: calls.append(kw.get("json")) or Upstream()
        panel.data.ensure_viewer = lambda service: (True, "")
        try:
            self.client.get("/components/consoled/viewer/")
            landing = list(calls)
            calls.clear()
            # An asset request must NOT re-run it.
            self.client.get("/components/consoled/viewer/assets/index.js")
        finally:
            (panel.requests.request, panel.requests.get, panel.requests.post,
             panel.data.ensure_viewer) = real

        self.assertEqual(1, len(landing), landing)
        self.assertEqual("consoled_redis-1", landing[0]["host"])
        self.assertEqual(6379, landing[0]["port"])
        self.assertEqual(component.password(), landing[0]["password"])
        self.assertEqual([], calls)

    def test_a_console_that_already_has_the_database_is_left_alone(self):
        """Reopening it must not add the same server a second time."""
        import app as panel
        self.make_redis_component("consoled2")
        posted = []
        real = (panel.requests.request, panel.requests.get, panel.requests.post,
                panel.data.ensure_viewer)
        panel.requests.request = lambda method, url, **kw: type(
            "U", (), {"status_code": 200, "headers": {},
                      "iter_content": staticmethod(lambda chunk_size=0: iter([b""]))})()
        panel.requests.get = lambda url, **kw: type(
            "R", (), {"json": staticmethod(lambda: [{"id": "already-there"}])})()
        panel.requests.post = lambda url, **kw: posted.append(url)
        panel.data.ensure_viewer = lambda service: (True, "")
        try:
            self.client.get("/components/consoled2/viewer/")
        finally:
            (panel.requests.request, panel.requests.get, panel.requests.post,
             panel.data.ensure_viewer) = real
        self.assertEqual([], posted)

    def test_a_mongo_console_needs_no_registration(self):
        """mongo-express is handed its URL and connects itself."""
        self.make_mongo("selfconnecting")
        self.assertEqual(
            [], self.components.load("selfconnecting").viewer_databases())

    def test_the_view_button_only_appears_when_the_visualiser_is_on(self):
        self.make_mongo("withviewer")
        page = self.client.get("/components/withviewer").get_data(as_text=True)
        self.assertIn("View data", page)
        self.make_mongo("noviewer2", visualizer=False)
        page = self.client.get("/components/noviewer2").get_data(as_text=True)
        self.assertNotIn("View data", page)


    def test_a_spec_written_before_dataguard_existed_is_not_managed(self):
        """
        The most expensive default in the codebase, caught on a live cluster.

        `dataguard` defaults to ON, which is right for a component created
        through the form where the switch is in front of you. Inherited by a
        component.json written before the field existed, it reclassified a
        running single-instance redis as a replica set — and because components
        deploy with `--prune`, the next redeploy of ANY kind would have rendered
        members and sentinels while deleting the one service holding the data,
        changing the connection string in the same breath. A password rotation
        would have done it.

        Absent means older than the feature: `create()` always writes the key.
        """
        old = self.components.redis.RedisComponent("legacy", {"spec": {
            "version": "7.4", "port": 6379, "memory_mb": 512}})
        self.assertFalse(old.spec["dataguard"])
        # One server, one volume, the name it has always had -- and no sentinels
        # to point a connection string at.
        self.assertIn("legacy_redis", old.services())
        self.assertEqual([], [s for s in old.services() if "sentinel" in s])
        self.assertEqual([], [s for s in old.services()
                              if s.startswith("legacy_redis-") and "exporter" not in s])
        self.assertIn("legacy_redis:6379", old.connection_url())

    def test_a_new_component_still_defaults_to_managed(self):
        """The fix must not turn the feature off for everything."""
        fresh = self.components.redis.RedisComponent("fresh")
        self.assertTrue(fresh.spec["dataguard"])

    def test_a_spec_that_says_managed_is_still_believed(self):
        on = self.components.redis.RedisComponent("kept", {"spec": {
            "version": "7.4", "dataguard": True}})
        self.assertTrue(on.spec["dataguard"])

    def test_the_pool_can_be_filled_right_up_to_its_edge(self):
        """The clamp is off-by-one in neither direction."""
        self.make_mongo("atedge")


class ViewerWakeTest(DatabaseHarness, PanelTest):
    """
    The View button, and the one property that made it usable.

    It used to scale the visualiser 0 -> 1 and then block for up to a minute
    waiting for a task, on top of a `docker service update` allowed another
    minute — all inside the HTTP request. This panel is served through
    Cloudflare, whose 100-second origin timeout is not configurable, so the
    button returned a 524 for something that was in fact starting perfectly
    well. Pressing it three times "worked" only because by the third press the
    container was up.
    """

    def test_a_cold_viewer_answers_at_once_instead_of_holding_the_request(self):
        import time
        self.make_mongo("cold")
        real_ensure = self.panel.data.ensure_viewer
        real_running = self.panel.data.viewer_running
        # What a stopped visualiser looks like: the wake was accepted, nothing
        # is running yet, and NOBODY WAITED to find that out.
        self.panel.data.ensure_viewer = lambda service: (False, "")
        self.panel.data.viewer_running = lambda service: False
        try:
            started = time.monotonic()
            r = self.client.get("/components/cold/viewer/")
            elapsed = time.monotonic() - started
        finally:
            self.panel.data.ensure_viewer = real_ensure
            self.panel.data.viewer_running = real_running
        self.assertEqual(r.status_code, 503)
        self.assertLess(elapsed, 2.0)
        page = r.get_data(as_text=True)
        # And it comes back by itself, which is what removes the ritual.
        self.assertIn("data-viewer-wait", page)
        self.assertIn("/components/cold/viewer-status", page)

    def test_the_status_endpoint_answers_the_one_question_the_page_asks(self):
        self.make_mongo("polled")
        real = self.panel.data.viewer_running
        try:
            for running in (False, True):
                self.panel.data.viewer_running = lambda service, r=running: r
                r = self.client.get("/components/polled/viewer-status")
                self.assertEqual(r.get_json(), {"ready": running})
        finally:
            self.panel.data.viewer_running = real

    def test_the_status_endpoint_is_not_inside_the_proxied_prefix(self):
        """
        A static segment under `/viewer/` would shadow any upstream path of the
        same name, and both consoles are third-party — which paths they serve is
        theirs to change, not ours to reserve.
        """
        rules = [str(r) for r in self.panel.app.url_map.iter_rules()]
        self.assertIn("/components/<name>/viewer-status", rules)
        self.assertNotIn("/components/<name>/viewer/status", rules)

    def test_it_needs_a_session_and_a_visualiser(self):
        self.make_mongo("guarded", visualizer=False)
        self.assertEqual(
            self.client.get("/components/guarded/viewer-status").status_code, 404)
        self.client.get("/logout")
        with self.panel.app.test_client() as anon:
            r = anon.get("/components/guarded/viewer-status")
            self.assertEqual(r.status_code, 302)


class MigrateTest(DatabaseHarness, PanelTest):
    """
    Moving a database to or from Atlas. Both directions REPLACE the
    destination, so both are guarded like a restore.
    """

    def test_the_section_appears_only_for_something_that_can_migrate(self):
        self.make_mongo("movable")
        page = self.client.get(
            "/components/movable?tab=backups").get_data(as_text=True)
        self.assertIn("Migrate", page)
        self.create_app("notadb")
        page = self.client.get(
            "/components/notadb?tab=backups").get_data(as_text=True)
        self.assertNotIn("Start the migration", page)

    def test_the_atlas_uri_never_lands_in_the_spec_file(self):
        """
        It is a full credential for somebody else's cluster. `component.json` is
        0640 and is not the place for one; `secret.env` is 0600 and is.
        """
        self.make_mongo("secretive")
        component = self.components.load("secretive")
        self.assertNotIn("ATLAS_URI", component.spec)
        self.assertNotIn("ATLAS_URI", str(component.as_dict()))
        self.assertIn("ATLAS_URI", [s.key for s in type(component).SECRETS])

    def test_it_is_asked_for_beside_the_migration_and_not_on_credentials(self):
        """
        The Credentials tab answers "how do I connect to this component". A
        credential for a cluster that is NOT this one is not an answer to that
        question, and putting it there implies this component uses it for
        something — it does not. It is read by one operation, so it is asked for
        where that operation is.
        """
        self.make_mongo("placed")
        creds = self.client.get(
            "/components/placed?tab=credentials").get_data(as_text=True)
        self.assertNotIn("ATLAS_URI", creds)
        backups = self.client.get(
            "/components/placed?tab=backups").get_data(as_text=True)
        self.assertIn("ATLAS_URI", backups)
        self.assertIn("MongoDB Atlas connection string", backups)

    def test_the_create_form_does_not_ask_for_it_either(self):
        """Nothing about creating a database needs somewhere to migrate it to."""
        self.login()
        page = self.client.get(
            "/components/new?type=mongo").get_data(as_text=True)
        self.assertNotIn("ATLAS_URI", page)
        self.assertIn("MONGO_PASSWORD", page)

    def test_the_credentials_form_cannot_set_a_key_it_does_not_show(self):
        """
        Not a privilege boundary — everything here is already root — but the
        difference between a form that means what it shows and one that does
        not. It also stops a credentials save from silently clearing it.
        """
        csrf = self.login()
        self.make_mongo("scoped")
        component = self.components.load("scoped")
        component.apply_secrets({"ATLAS_URI": "mongodb+srv://kept@atlas/"},
                                tab="migrate", generate_missing=False)
        self.client.post("/components/scoped/credentials",
                         data={"csrf": csrf, "MONGO_PASSWORD": "a-new-password",
                               "ATLAS_URI": "mongodb+srv://sneaky@elsewhere/"},
                         follow_redirects=True)
        fresh = self.components.load("scoped")
        self.assertEqual(fresh.secret("MONGO_PASSWORD"), "a-new-password")
        self.assertEqual(fresh.secret("ATLAS_URI"), "mongodb+srv://kept@atlas/")

    def test_the_migrate_form_stores_it_and_blank_reuses_what_is_there(self):
        csrf = self.login()
        self.make_mongo("storing")
        component_cls = type(self.components.load("storing"))
        real = component_cls.start_migration
        component_cls.start_migration = lambda self, d, overwrite=False: (True, "ok")
        try:
            self.client.post("/components/storing/migrate",
                             data={"csrf": csrf, "direction": "export",
                                   "confirm": "storing",
                                   "ATLAS_URI": "mongodb+srv://one@atlas/"},
                             follow_redirects=True)
            self.assertEqual(self.components.load("storing").secret("ATLAS_URI"),
                             "mongodb+srv://one@atlas/")
            # Blank means "the one you already have", as every other secret
            # input on this panel means.
            self.client.post("/components/storing/migrate",
                             data={"csrf": csrf, "direction": "import",
                                   "confirm": "storing", "ATLAS_URI": ""},
                             follow_redirects=True)
            self.assertEqual(self.components.load("storing").secret("ATLAS_URI"),
                             "mongodb+srv://one@atlas/")
        finally:
            component_cls.start_migration = real

    def test_a_rotate_does_not_invent_an_atlas_string(self):
        """
        `generated=False`. A random value here would not be a weak secret, it
        would be a wrong one — it would look configured and fail every use.
        """
        self.make_mongo("rotated")
        component = self.components.load("rotated")
        component.apply_secrets({"ATLAS_URI": "mongodb+srv://real@atlas/"},
                                tab="migrate", generate_missing=False)
        before = component.secret("MONGO_PASSWORD")
        component.rotate_secrets()
        fresh = self.components.load("rotated")
        self.assertNotEqual(fresh.secret("MONGO_PASSWORD"), before)
        self.assertEqual(fresh.secret("ATLAS_URI"), "mongodb+srv://real@atlas/")

    def test_a_wrong_confirmation_starts_nothing(self):
        csrf = self.login()
        self.make_mongo("typed")
        component = self.components.load("typed")
        started = []
        component_cls = type(component)
        real = component_cls.start_migration
        component_cls.start_migration = lambda self, d, overwrite=False: (
            started.append(d) or (True, "off we go"))
        try:
            self.client.post("/components/typed/migrate",
                             data={"csrf": csrf, "direction": "export",
                                   "confirm": "typo"}, follow_redirects=True)
            self.assertEqual(started, [])
            self.client.post("/components/typed/migrate",
                             data={"csrf": csrf, "direction": "export",
                                   "confirm": "typed"}, follow_redirects=True)
            self.assertEqual(started, ["export"])
        finally:
            component_cls.start_migration = real

    def test_it_refuses_a_direction_it_does_not_know(self):
        self.make_mongo("directed")
        component = self.components.load("directed")
        ok, why = component.start_migration("sideways")
        self.assertFalse(ok)
        self.assertIn("direction", why.lower())

    def test_it_refuses_without_an_atlas_connection_string(self):
        self.make_mongo("stringless")
        component = self.components.load("stringless")
        ok, why = component.start_migration("export")
        self.assertFalse(ok)
        self.assertIn("Atlas", why)

    def test_neither_connection_string_is_ever_an_argument(self):
        """
        Container arguments show up in the master's own process table, and `ps`
        is readable by anything else on the box. Both URIs arrive as mounted
        secrets and are written into files the tools read with `--config`.
        """
        script = self.components.load.__globals__  # noqa: F841 — readability only
        import components.mongo as mongo_mod
        body = mongo_mod.MongoComponent._MIGRATE_SCRIPT
        self.assertIn("--config", body)
        self.assertNotIn("--uri", body)
        self.assertIn("/run/secrets/migrate-here.uri", body)
        self.assertIn("/run/secrets/migrate-there.uri", body)
        # mongosh has no --config, so its URI goes into the script file instead.
        self.assertIn("mongosh --quiet --nodb --file", body)

    def test_the_job_verifies_and_fails_loudly_on_a_mismatch(self):
        import components.mongo as mongo_mod
        body = mongo_mod.MongoComponent._MIGRATE_SCRIPT
        self.assertIn("VERIFIED", body)
        self.assertIn("MISMATCH", body)
        # A migration that reports success for an incomplete copy is worse than
        # one that fails, so the mismatch branch exits non-zero.
        self.assertRegex(body, r"MISMATCH[\s\S]*exit 1")

    def test_the_job_refuses_a_non_empty_destination_it_was_not_told_about(self):
        import components.mongo as mongo_mod
        body = mongo_mod.MongoComponent._MIGRATE_SCRIPT
        self.assertIn('[ "$OVERWRITE" != "yes" ]', body)
        # BEFORE the dump, not after: a full destination is the one preflight
        # answer that means somebody is about to lose data.
        self.assertLess(body.index("OVERWRITE"), body.index("mongodump"))


class PbmConfigTest(DatabaseHarness, PanelTest):
    """
    `pbm config` had no call site anywhere in this repo, and the Storage tab's
    credential secret was mounted nowhere. PBM therefore had no idea where to
    put a backup, so the whole feature was a design with its last wire loose.
    """

    def test_a_component_with_no_target_asks_pbm_for_nothing(self):
        self.make_mongo("untargeted", backup_target="")
        component = self.components.load("untargeted")
        self.assertIsNone(component.pbm_storage_config())
        self.assertEqual(component.configure_backups(), (True, ""))

    def test_the_config_turns_pitr_on(self):
        """Point-in-time restore is the reason PBM is here rather than a
        mongodump loop; leaving it off would make the Backups tab's promise of
        restoring to a second untrue."""
        component = self._targeted("pitred")
        self.assertEqual(component.pbm_storage_config()["pitr"], {"enabled": True})

    def test_the_bucket_keys_are_never_in_the_config_we_build(self):
        component = self._targeted("keyless")
        rendered = str(component.pbm_storage_config())
        self.assertNotIn("access", rendered.lower())
        self.assertNotIn("secret", rendered.lower())

    def _targeted(self, name):
        import storage as storage_store
        real_by_name, real_secret = storage_store.by_name, storage_store.secret_name
        self.addCleanup(setattr, storage_store, "by_name", real_by_name)
        self.addCleanup(setattr, storage_store, "secret_name", real_secret)
        storage_store.by_name = lambda n: {"name": n, "bucket": "b",
                                           "region": "eu-central-1", "sse": True}
        storage_store.secret_name = lambda n: f"storage-{n}-v1"
        self.make_mongo(name, backup_target="vault")
        return self.components.load(name)


class AlertmanagerRenderTest(unittest.TestCase):
    """
    The generated Alertmanager config, including from a state file nobody sane
    wrote.

    `bin/render-alertmanager` is the last thing between `alert_targets.json` and
    a file Alertmanager has to parse, and it is NOT always the panel that wrote
    that file — an operator editing JSON on the master, or a panel older than
    the validation, both produce input the panel's own checks never saw. A
    target that cannot be rendered into YAML meaning what it says is dropped and
    named on stderr, because the alternative is a monitoring stack that will not
    start and a message about a parse error on line 41.
    """

    GOOD = {"name": "team", "kind": "telegram",
            "bot_token": "8140000000:AAF-this-is-a-plausible-token",
            "chat_id": "-1001234567890"}

    def setUp(self):
        import importlib.util
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_loader(
            "render_alertmanager",
            importlib.machinery.SourceFileLoader(
                "render_alertmanager", str(root / "bin" / "render-alertmanager")))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.mkdtemp(prefix="render-test-")
        os.makedirs(os.path.join(self.tmp, "state"))
        self.mod.TARGETS = os.path.join(self.tmp, "state", "alert_targets.json")
        self.out = os.path.join(self.tmp, "alertmanager.yml")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def render(self, targets):
        import io
        import json
        from contextlib import redirect_stderr, redirect_stdout
        with open(self.mod.TARGETS, "w") as fh:
            json.dump(targets, fh)
        noise, out = io.StringIO(), io.StringIO()
        with redirect_stderr(noise), redirect_stdout(out):
            sys.argv = ["render-alertmanager", self.out]
            self.mod.main()
        with open(self.out) as fh:
            return fh.read(), noise.getvalue()

    def parsed(self, text):
        import yaml
        return yaml.safe_load(text)

    def test_a_good_target_becomes_one_config_in_each_receiver(self):
        text, _ = self.render([self.GOOD])
        got = self.parsed(text)
        receivers = {r["name"]: r for r in got["receivers"]}
        self.assertEqual(["default", "heartbeat"], sorted(receivers))
        entry = receivers["default"]["telegram_configs"][0]
        self.assertEqual(self.GOOD["bot_token"], entry["bot_token"])
        self.assertEqual(-1001234567890, entry["chat_id"])

    def test_a_token_with_a_newline_cannot_break_the_file(self):
        """
        NEUTRALISED, NOT REFUSED, and that is the stronger property.

        The failure being prevented is an unparseable alertmanager.yml, which
        stops the monitoring stack deploying and takes alerting down with it.
        This used to be prevented by dropping any token that did not match a
        guessed pattern — which also dropped valid ones, and left a correctly
        pasted credential rejected with no recourse.

        Quoting handles it for every input instead: the target is KEPT, the
        injected `bot_token:` line becomes part of a string value rather than a
        second config entry, and the file still parses. Two targets in, two
        configs out, nothing dropped and nothing escaped into the structure.
        """
        evil = '1234:x"\n      - bot_token: "y'
        text, _ = self.render([
            self.GOOD,
            {"name": "evil", "kind": "telegram", "chat_id": "1",
             "bot_token": evil},
        ])
        got = self.parsed(text)
        configs = {r["name"]: r for r in got["receivers"]}["default"]["telegram_configs"]
        self.assertEqual(2, len(configs))
        self.assertIn(evil, [c["bot_token"] for c in configs])

    def test_a_chat_id_that_is_not_a_number_is_dropped(self):
        text, _ = self.render([
            self.GOOD,
            dict(self.GOOD, name="odd", chat_id="9; rm -rf /"),
        ])
        receivers = {r["name"]: r for r in self.parsed(text)["receivers"]}
        self.assertEqual(1, len(receivers["default"]["telegram_configs"]))

    def test_no_targets_renders_receivers_that_drop_everything(self):
        """
        Deliberately, and visibly. A config Alertmanager refuses to load would
        take the whole monitoring stack down over a cluster nobody has wired to
        a chat yet.
        """
        text, _ = self.render([])
        receivers = {r["name"]: r for r in self.parsed(text)["receivers"]}
        self.assertEqual(["default", "heartbeat"], sorted(receivers))
        self.assertIsNone(receivers["default"].get("telegram_configs"))


if __name__ == "__main__":
    unittest.main()
