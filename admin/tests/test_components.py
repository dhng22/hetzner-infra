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

_ADMIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ADMIN)
# The repository ROOT as well, for `pki`. The panel ISSUES the TLS material a
# managed database mounts, so `admin/Dockerfile` copies `pki/` in from beside
# `admin/` — but this suite runs with `admin/` as the working directory both
# in the image and out of it, and that puts admin/ on the path and the root
# nowhere. Without this line every mongo render test dies on `import pki`.
sys.path.insert(1, os.path.dirname(_ADMIN))


class ComponentCase(unittest.TestCase):
    """Fixture only — a scratch INFRA_DIR, no docker, and the factories.

    Split from the tests so a second test class can build components without
    also re-running every assertion in the first one.
    """

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
        # No docker either. `base.run` and `base.docker_out` are the two shells
        # the renderer uses — for Swarm secrets and for reading a live spec back
        # — and stubbing them here keeps every test in this file pure. A test
        # that needed a daemon would be testing Docker, not the renderer.
        components.base.run = lambda argv, timeout=600, stdin=None: (True, "")
        components.base.docker_out = lambda argv: ""

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

    def make_mongo(self, name="docs", **spec):
        base = {"dataguard": True, "secondary_reads": True}
        base.update(spec)
        # A bool goes in as a real bool: `coerce` takes one straight through,
        # and an empty string would coerce to None and fall back to the default.
        component, problems = self.components.create("mongo", name, base)
        self.assertEqual(problems, [], f"unexpected problems: {problems}")
        return component


class ComponentTest(ComponentCase):
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
        self.assertNotIn("node.role == worker", placement["constraints"])
        self.assertNotIn("node.role == manager", placement["constraints"])
        self.assertEqual(placement["preferences"], [{"spread": "node.id"}])

    def test_an_app_is_kept_off_a_machine_leased_to_a_database(self):
        """
        Swarm has no taints, so this constraint IS the mechanism — and it is
        unconditional, because a leased node is an ordinary worker the moment an
        app service is unpinned. Without it, the first handover back to manager
        mode puts an API replica on the machine holding a mongod.
        """
        placement = self.make_app().render()["services"]["app"]["deploy"]["placement"]
        self.assertIn("node.labels.dedicated != true", placement["constraints"])

    def test_live_pin_is_preserved(self):
        component = self.make_app()
        component.live_worker_pinned = lambda service=None: True
        placement = component.render()["services"]["app"]["deploy"]["placement"]
        self.assertIn("node.role == worker", placement["constraints"])

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

    def test_a_password_can_be_chosen_at_create_time(self):
        """Blank generates; supplied is kept. Moving an existing database needs both."""
        component, problems = self.components.create(
            "redis", "mine", {"REDIS_PASSWORD": "carried-over-from-the-old-box"})
        self.assertEqual(problems, [])
        self.assertEqual(component.password(), "carried-over-from-the-old-box")

    def test_a_weak_password_is_refused_and_nothing_is_written(self):
        _, problems = self.components.create("redis", "weak", {"REDIS_PASSWORD": "abc"})
        self.assertTrue(any("at least" in p for p in problems))
        self.assertFalse(self.components.exists("weak"))

    def test_a_password_with_a_line_break_is_refused(self):
        _, problems = self.components.create(
            "redis", "multi", {"REDIS_PASSWORD": "line-one\nline-two"})
        self.assertTrue(any("line break" in p for p in problems))

    def test_a_password_can_be_changed_afterwards(self):
        component = self.make_redis()
        self.assertEqual(component.set_password("a-new-password-entirely"), [])
        self.assertEqual(self.components.load("cache").password(),
                         "a-new-password-entirely")

    def test_a_blank_submission_keeps_the_current_password(self):
        """The credentials form's empty field means 'unchanged', not 'wipe it'."""
        component = self.make_redis()
        before = component.password()
        self.assertEqual(component.apply_secrets({"REDIS_PASSWORD": ""}), [])
        self.assertEqual(self.components.load("cache").password(), before)

    def test_the_connection_url_escapes_the_password(self):
        """
        `redis://default:p@ss@host:6379` parses as a different host, and the
        client's error blames DNS. A user-chosen password needs encoding.
        """
        component = self.make_redis()
        component.set_password("p@ss:word/slash")
        url = self.components.load("cache").connection_url()
        self.assertIn("p%40ss%3Aword%2Fslash", url)
        # The SENTINELS, not the server: the client asks them who the primary is,
        # which is what lets a failover change the answer and never the address.
        self.assertTrue(url.startswith("redis+sentinel://"))
        self.assertIn("cache_sentinel-1:26379", url)
        self.assertTrue(url.endswith("/cache/0"))

    def test_redis_password_expands_at_runtime(self):
        """
        The command must go through a shell, and the $ must survive compose.

        Without `sh -c`, compose passes `--requirepass "$REDIS_PASSWORD"` to
        redis-server as a literal argument, so the server enforces the string
        while every client is handed the real password. That was a live defect
        in the stack file this replaces.
        """
        command = self.make_redis().render()["services"]["redis-1"]["command"]
        self.assertEqual(command[:2], ["sh", "-c"])
        self.assertIn('--requirepass "$$REDIS_PASSWORD"', command[2])
        self.assertTrue(command[2].startswith("exec "))

    def test_the_master_replica_is_pinned_and_is_not_an_app_workload(self):
        """
        Replica 1 is the master's copy and never moves — every state in
        dataguard's ladder is described relative to it. It carries no
        `infra.workload` label, which is what keeps the autoscaler away, and an
        explicit `infra.managed_by` so a mislabelled one is refused rather than
        merely unnoticed.
        """
        deploy = self.make_redis().render()["services"]["redis-1"]["deploy"]
        self.assertEqual(deploy["placement"]["constraints"], ["node.role == manager"])
        self.assertNotIn("infra.workload", deploy["labels"])
        self.assertEqual(deploy["labels"]["infra.managed_by"], "dataguard")

    def test_the_other_replicas_start_stopped(self):
        """
        Every member of the pool is a service from the first deploy — that is
        what makes its DNS name addressable and its seed entry meaningful — but
        only the master's copy runs until dataguard says otherwise.
        """
        rendered = self.make_redis().render()
        self.assertEqual(rendered["services"]["redis-1"]["deploy"]["replicas"], 1)
        for key in ("redis-2", "redis-3", "redis-4"):
            self.assertEqual(rendered["services"][key]["deploy"]["replicas"], 0)

    def test_the_sentinel_quorum_is_odd_and_does_not_hide_on_one_box(self):
        """
        The SENTINELS vote, not the replicas. A quorum that lives entirely on the
        master cannot survive the master, so nothing pins them there.
        """
        rendered = self.make_redis().render()
        sentinels = [k for k in rendered["services"] if k.startswith("sentinel-")]
        self.assertEqual(len(sentinels), 3)
        self.assertEqual(len(sentinels) % 2, 1)
        placement = rendered["services"]["sentinel-1"]["deploy"]["placement"]
        self.assertNotIn("node.role == manager", placement.get("constraints", []))

    def test_only_one_sentinel_runs_while_there_is_one_server(self):
        """
        Three sentinels were STARTING immediately, and on a single-node cluster
        all three landed on the master — three containers watching the machine
        they live on, which is one point of failure counted three times, for a
        server that had no replica to be promoted in its place anyway.

        They are declared from the first deploy and only the first one runs, the
        same live-count-wins contract the data members already had. Dataguard
        starts the other two when it starts the second server.
        """
        rendered = self.make_redis().render()
        self.assertEqual(rendered["services"]["sentinel-1"]["deploy"]["replicas"], 1)
        for key in ("sentinel-2", "sentinel-3"):
            self.assertEqual(rendered["services"][key]["deploy"]["replicas"], 0, key)

    def test_a_running_sentinel_is_not_stopped_by_an_unrelated_save(self):
        """
        The live count wins over this file's opinion, or saving a maxmemory
        change mid-failover would take the quorum away while it was voting.
        """
        component = self.make_redis()
        component.live_replicas = lambda service=None: 1
        rendered = component.render()
        for key in ("sentinel-1", "sentinel-2", "sentinel-3"):
            self.assertEqual(rendered["services"][key]["deploy"]["replicas"], 1, key)

    def test_a_sentinel_says_it_is_a_sentinel_and_a_member_says_it_is_a_member(self):
        """
        A sentinel carries the same component, type and member index as a data
        member does, so the index alone was never a key: dataguard read three
        running sentinels as three running data members and believed a
        single-server component was already a three-member set. The ROLE is what
        separates them.
        """
        rendered = self.make_redis().render()
        member = rendered["services"]["redis-2"]["deploy"]["labels"]
        sentinel = rendered["services"]["sentinel-2"]["deploy"]["labels"]
        self.assertEqual(member["dataguard.role"], "member")
        self.assertEqual(sentinel["dataguard.role"], "sentinel")
        # The collision itself, spelled out: everything else about them matches.
        for key in ("infra.component", "infra.type", "dataguard.member"):
            self.assertEqual(member[key], sentinel[key], key)

    def test_redis_exporter_is_optional_and_scraped(self):
        with_exporter = self.make_redis("c1").render()
        self.assertIn("redis-exporter", with_exporter["services"])
        labels = with_exporter["services"]["redis-exporter"]["deploy"]["labels"]
        self.assertEqual(labels["prometheus.port"], "9121")
        without = self.make_redis("c2", exporter="false").render()
        self.assertNotIn("redis-exporter", without["services"])
        self.assertEqual(without["services"]["redis-1"]["networks"], ["edge"])

    def test_nothing_is_published_on_a_host_port(self):
        """Not by a member, not by the gateway. The connector dials out, so
        there is no address on this side and no firewall rule to open."""
        rendered = self.make_redis(external_hostname="cache.example.com").render()
        for service in rendered["services"].values():
            self.assertNotIn("ports", service)

    # --- managed databases --------------------------------------------------

    def test_the_seed_list_names_one_host_however_big_the_set_gets(self):
        """
        The property the whole design rests on, and it used to be spelled the
        other way round: the string named every slot, so the slot count was
        frozen at creation and a set that outgrew it could not be helped.

        One alias instead. Swarm answers it with the members that are running,
        so member 4 can mean nothing for six months and then mean a machine in
        Helsinki — with neither the string nor a ceiling changing.
        """
        url = self.make_mongo().connection_url()
        self.assertIn("docs-mongo:27017", url)
        self.assertNotIn("docs_mongo-", url)
        self.assertIn("replicaSet=docs", url)

    def test_the_connection_string_requires_tls_from_the_first_day(self):
        """
        While the set is one member on the master nothing crosses the network —
        and that is exactly why it has to be right now rather than on the day a
        second machine appears, which is a day nobody would remember.
        """
        self.assertIn("tls=true", self.make_mongo().connection_url())

    def test_writes_are_majority_acknowledged_and_retried(self):
        """
        `w=majority` is what makes a write survive a failover; `retryWrites` is
        what stops every stepdown being an error your users see.
        """
        url = self.make_mongo().connection_url()
        self.assertIn("w=majority", url)
        self.assertIn("readConcernLevel=majority", url)
        self.assertIn("retryWrites=true", url)

    def test_secondary_reads_are_the_only_thing_that_changes_the_read_preference(self):
        """
        It is the one option that changes what the APPLICATION is allowed to
        assume, so it appears only when somebody has said yes to that.
        """
        self.assertIn("readPreference=secondaryPreferred",
                      self.make_mongo("withreads").connection_url())
        self.assertNotIn("readPreference",
                         self.make_mongo("noreads", secondary_reads=False).connection_url())

    def test_every_member_requires_tls_and_authenticates_with_x509(self):
        rendered = self.make_mongo().render()
        for index in range(1, 5):
            command = rendered["services"][f"mongo-{index}"]["command"][2]
            self.assertIn("--tlsMode requireTLS", command)
            self.assertIn("--clusterAuthMode x509", command)
            # preferTLS would carry plaintext the first time a client got its
            # options wrong, and nothing would say so.
            self.assertNotIn("preferTLS", command)

    def test_a_member_certificate_is_mounted_read_only_and_owned_by_mongod(self):
        """mongod refuses a key file it does not own, and blames the file."""
        secrets = self.make_mongo().render()["services"]["mongo-2"]["secrets"]
        member = next(s for s in secrets if s["target"] == "tls-member.pem")
        self.assertEqual(member["uid"], "999")
        self.assertEqual(member["mode"], 0o400)

    def test_the_authority_private_key_never_leaves_the_master(self):
        """
        The Credentials tab hands out the CA CERTIFICATE, which a client outside
        the cluster needs to verify a member. The key that signs them is not
        reachable from any route, and this is the assertion that keeps it so.
        """
        component = self.make_mongo()
        component.render()          # the authority is issued when it is needed
        self.assertIn("BEGIN CERTIFICATE", component.ca_certificate())
        self.assertNotIn("PRIVATE KEY", component.ca_certificate())

    def test_only_the_master_member_starts(self):
        """
        Every member is a service from the first deploy — that is what makes its
        DNS name addressable — but only the one on the master runs until
        dataguard says otherwise.
        """
        rendered = self.make_mongo().render()
        self.assertEqual(rendered["services"]["mongo-1"]["deploy"]["replicas"], 1)
        for index in (2, 3, 4):
            self.assertEqual(
                rendered["services"][f"mongo-{index}"]["deploy"]["replicas"], 0)

    def test_the_visualiser_has_no_published_port_and_starts_at_zero(self):
        """
        The whole security model. It is full access with no password of its own,
        so the only door is the panel's session — and it does not exist until
        somebody opens it.
        """
        viewer = self.make_mongo(visualizer=True).render()["services"]["viewer"]
        self.assertNotIn("ports", viewer)
        self.assertEqual(viewer["deploy"]["replicas"], 0)

    def test_the_sentinel_takes_no_password_because_no_client_can_send_one(self):
        """
        The sentinel had `requirepass` for exactly one deploy, on the theory that
        `redis+sentinel://default:<pw>@…` sends that password to the sentinel
        hosts. It does not. Lettuce, redis-py, ioredis and go-redis all read the
        userinfo password as the DATA NODE's and none of them offers a way to put
        the sentinel's in a URL, so the string this panel publishes had no place
        to carry one — and every client that honoured it was refused with
        "NOAUTH HELLO must be called with the client already authenticated"
        before it could ask where the primary was. Dataguard read the same
        refusal as "this component has no master".

        `auth-pass` is a different door and stays: that is how the sentinel
        reaches the master, and nothing about it is visible to a client.
        """
        component = self.make_redis("c2")
        conf = " ".join(component.render()["services"]["sentinel-1"]["command"])
        self.assertNotIn("requirepass", conf)
        self.assertIn("sentinel auth-pass", conf)
        self.assertTrue(component.connection_url().startswith(
            "redis+sentinel://default:"))

    def test_the_data_nodes_still_take_a_password(self):
        """
        The pair to the test above, and the reason it is safe. Dropping auth at
        the sentinel gave up the failover verbs, not the data: a member without
        `requirepass` would be an open Redis on a network every application can
        reach.
        """
        member = " ".join(self.make_redis("c2").render()["services"]["redis-1"]["command"])
        self.assertIn("--requirepass", member)
        self.assertIn("--masterauth", member)

    def test_the_mongo_visualiser_agrees_with_itself_about_tls(self):
        """
        mongo-express crash-looped on exit 1 with an empty log, and the panel
        reported a 502.

        Its own driver options carry an `ssl` key whether or not anybody set it,
        and the connection string this platform writes carries `tls=true`. The
        Mongo driver treats the pair as one setting and refuses to construct a
        client when they disagree, so the process died before it opened a port.
        The CA is a second, separate refusal waiting behind the first: the
        members present a certificate from this component's own authority, and
        the option mongo-express exposes for that (`sslCA`) has not been read by
        the driver since version 5 — so it goes to Node, which both of them use.
        """
        env = self.make_mongo(visualizer=True).render()[
            "services"]["viewer"]["environment"]
        self.assertIn("tls=true", env["ME_CONFIG_MONGODB_URL"])
        self.assertEqual(env["ME_CONFIG_MONGODB_SSL"], "true")
        # The literal path is not the point and pinning it here only made this
        # test fail when the path was deliberately changed. What must hold is
        # that the file the console is TOLD to read is the same one the
        # connection string names.
        self.assertIn(f"tlsCAFile={env['NODE_EXTRA_CA_CERTS']}",
                      env["ME_CONFIG_MONGODB_URL"])

    def test_the_mongo_visualiser_does_not_ask_for_a_password_it_never_set(self):
        """
        The console answered 401, and the only credentials that opened it were
        `admin:pass` — the mongo-express image's own shipped defaults.

        The renderer set `ME_CONFIG_BASICAUTHENABLED=false`, which is the name
        mongo-express used in 0.x and reads as nothing in 1.0; the image's
        Dockerfile sets `ME_CONFIG_BASICAUTH=true`, so the switch that was
        supposed to turn its login off never touched it. A setting that is
        ignored is worse than one that is wrong — it reports the thing it did
        not do.
        """
        env = self.make_mongo(visualizer=True).render()[
            "services"]["viewer"]["environment"]
        self.assertEqual(env["ME_CONFIG_BASICAUTH"], "false")
        self.assertNotIn("ME_CONFIG_BASICAUTHENABLED", env)

    def test_the_visualiser_is_given_the_authority_it_is_told_to_read(self):
        """The path above is a mount, and a mount that is not there is a crash."""
        viewer = self.make_mongo(visualizer=True).render()["services"]["viewer"]
        targets = [s["target"] for s in viewer["secrets"]]
        wanted = viewer["environment"]["NODE_EXTRA_CA_CERTS"]
        self.assertIn(wanted.rsplit("/", 1)[-1], targets)

    def test_the_view_button_can_actually_reach_the_visualiser(self):
        """
        `edge` alone was a 502 every time, and this test asserted it.

        The panel is on `monitoring` and nothing else; the visualiser was on
        `edge` and nothing else. So the proxy resolved a name that did not exist
        for it, `requests` raised, and the route rendered its 502 page with a DNS
        error nobody would connect to a missing network. It has to be on the
        network the PANEL is on, because the panel is its only client.

        Both types, and the top-level network list too — a service on a network
        the stack does not declare will not deploy at all, and with the exporter
        switched off nothing else was pulling `monitoring` in.
        """
        for component in (self.make_mongo("d1", visualizer=True, exporter=False),
                          self.make_redis("c1", visualizer=True, exporter=False)):
            rendered = component.render()
            viewer = rendered["services"]["viewer"]
            self.assertIn("monitoring", viewer["networks"], component.TYPE)
            self.assertIn("edge", viewer["networks"], component.TYPE)
            self.assertIn("monitoring", rendered["networks"], component.TYPE)

    def test_a_managed_database_carries_its_whole_policy_as_labels(self):
        """
        Nothing about a component lives in dataguard's configuration: it
        discovers by label and reads policy from the service, exactly as the
        autoscaler does. A second database is a create form, not an edit.
        """
        labels = self.make_mongo().render()["services"]["mongo-2"]["deploy"]["labels"]
        self.assertEqual(labels["infra.managed_by"], "dataguard")
        self.assertEqual(labels["dataguard.member"], "2")
        self.assertEqual(labels["dataguard.pool"],
                         str(self.components.base.MEMBER_SLOTS))
        self.assertEqual(labels["dataguard.enabled"], "true")
        self.assertEqual(labels["dataguard.secondary_reads"], "true")

    def test_turning_dataguard_off_leaves_the_labels_saying_so(self):
        """
        Discovered either way, managed only when asked — the same shape as an
        application that is discovered but not autoscaled.
        """
        rendered = self.make_mongo("unmanaged", dataguard=False).render()
        # ONE service, and the unsuffixed name. With the switch off the panel
        # promises "one server, one volume", and `--prune` means the difference
        # between `mongo` and `mongo-1` is the difference between keeping a live
        # database and deleting it on the next redeploy.
        self.assertEqual(["mongo"], [k for k in rendered["services"]
                                     if k.startswith("mongo")
                                     and "exporter" not in k])
        labels = rendered["services"]["mongo"]["deploy"]["labels"]
        self.assertEqual(labels["dataguard.enabled"], "false")

    def test_redis_says_it_cannot_hide_a_replica_rather_than_pretending(self):
        """
        Mongo can take a lagging secondary out of read rotation. Redis cannot —
        a sentinel-aware client picks its own — so the label says false instead
        of claiming a control that does not exist.
        """
        labels = self.make_redis().render()["services"]["redis-2"]["deploy"]["labels"]
        self.assertEqual(labels["dataguard.secondary_reads"], "false")

    def test_a_redis_backup_target_needs_persistence(self):
        _component, problems = self.components.create(
            "redis", "nopersist", {"appendonly": "", "backup_target": "s3main"})
        self.assertTrue(any("nothing to back up" in p for p in problems), problems)

    def test_rotation_changes_the_password(self):
        component = self.make_redis()
        before = component.password()
        component.rotate_secrets()
        after = self.components.load("cache").password()
        self.assertNotEqual(before, after)
        self.assertEqual(len(after), 48)

    def test_an_application_has_no_credentials(self):
        """Only a type that declares SECRETS gets a credentials tab or a rotate."""
        self.assertEqual(type(self.make_app()).SECRETS, ())
        self.assertEqual(self.make_app("api2").apply_secrets({"anything": "x"}), [])

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

    # --- actions ------------------------------------------------------------

    def test_stop_and_deploy_are_never_offered_at_the_same_time(self):
        """
        `when` is what makes them a pair. Show both and one of the two is a
        button that cannot do anything, which is how a Deploy on a running stack
        becomes a Redeploy nobody meant to press.
        """
        for component in (self.make_app(), self.make_redis(), self.make_mongo()):
            actions = component.actions()
            self.assertEqual(actions["stop"]["when"], "running")
            self.assertEqual(actions["start"]["when"], "stopped")
            running = [v for v in actions.values() if v["when"] != "stopped"]
            stopped = [v for v in actions.values() if v["when"] != "running"]
            self.assertNotIn(actions["start"], running)
            self.assertNotIn(actions["stop"], stopped)
            # Whatever a type adds, the panel can style it without knowing the
            # verb: every action carries its own weight.
            for verb, act in actions.items():
                self.assertIn(act["tone"], ("", "primary", "danger"), verb)

    def test_stop_keeps_the_component_on_disk(self):
        """Stop is `stack rm`; the files are the component, not the stack."""
        component = self.make_redis()
        calls = []
        self.components.base.run = lambda argv, **kw: (calls.append(argv), (True, ""))[1]
        ok, out = component.stop()
        self.assertTrue(ok)
        self.assertEqual(calls[0][:3], ["docker", "stack", "rm"])
        self.assertTrue(self.components.exists("cache"))
        self.assertIn("volumes are kept", out)

    def test_purge_refuses_when_the_container_is_not_on_this_node(self):
        """
        The panel holds the master's socket and nothing else. Saying so beats
        `docker exec` failing with "no such container" against an empty id.
        """
        component = self.make_redis()
        component._local_container = lambda: ""
        ok, out = component.purge()
        self.assertFalse(ok)
        self.assertIn("on this node", out)

    def test_purge_rewrites_the_aof_only_when_persistence_is_on(self):
        component = self.make_redis(appendonly="true")
        component._local_container = lambda: "abc123"
        sent = []
        component._redis_cli = lambda cid, *args: (sent.append(args), (True, ""))[1]
        self.assertTrue(component.purge()[0])
        self.assertEqual(sent, [("FLUSHALL",), ("BGREWRITEAOF",)])

        cache = self.make_redis("nopersist", appendonly="false")
        cache._local_container = lambda: "abc123"
        sent = []
        cache._redis_cli = lambda cid, *args: (sent.append(args), (True, ""))[1]
        self.assertTrue(cache.purge()[0])
        self.assertEqual(sent, [("FLUSHALL",)])

    def test_a_failed_aof_rewrite_is_not_reported_as_success(self):
        """FLUSHALL alone leaves a file that puts every key back on restart."""
        component = self.make_redis()
        component._local_container = lambda: "abc123"
        component._redis_cli = lambda cid, *args: (True, "") if args == ("FLUSHALL",) else (False, "boom")
        ok, out = component.purge()
        self.assertFalse(ok)
        self.assertIn("BGREWRITEAOF", out)

    # --- mongo --------------------------------------------------------------

    def test_mongo_member_one_is_manager_pinned_and_not_an_app_workload(self):
        """It has a volume, so the autoscaler must never move it to a worker."""
        deploy = self.make_mongo().render()["services"]["mongo-1"]["deploy"]
        self.assertEqual(deploy["placement"]["constraints"], ["node.role == manager"])
        self.assertNotIn("infra.workload", deploy["labels"])
        self.assertEqual(deploy["labels"]["infra.managed_by"], "dataguard")

    def test_mongo_pins_the_wiredtiger_cache(self):
        """
        Unset, WiredTiger sizes itself from the HOST's memory and ignores the
        container limit entirely — which is an OOM kill, not a slow query.
        """
        # The reservation is stated rather than inherited: `validate()` requires
        # it to exceed the cache, so a test that leans on whatever the default
        # happens to be breaks the day the default is tuned — which is what it
        # did.
        command = self.make_mongo(cache_mb=512, memory_reservation_mb=768
                                  ).render()["services"]["mongo-1"]["command"][2]
        self.assertIn("--wiredTigerCacheSizeGB 0.5", command)

    def test_mongo_cache_must_leave_headroom(self):
        _, problems = self.components.create(
            "mongo", "tight", {"cache_mb": 768, "memory_reservation_mb": 768})
        self.assertTrue(any("below the memory reservation" in p for p in problems))

    def test_mongo_password_is_generated_and_kept_out_of_the_spec(self):
        component = self.make_mongo()
        component.render()
        self.assertTrue(component.password())
        spec = self.components.store.read_spec("docs")
        self.assertNotIn("MONGO_PASSWORD", str(spec))

    def test_the_mongo_connection_url_escapes_the_password(self):
        component = self.make_mongo()
        component.apply_secrets({"MONGO_PASSWORD": "p@ss/word:1"})
        url = component.connection_url()
        self.assertIn("p%40ss%2Fword%3A1", url)
        self.assertIn("authSource=admin", url)

    def test_mongo_exporter_is_optional_and_scraped(self):
        rendered = self.make_mongo("m1").render()
        labels = rendered["services"]["mongo-exporter"]["deploy"]["labels"]
        self.assertEqual(labels["prometheus.port"], "9216")
        without = self.make_mongo("m2", exporter="false").render()
        self.assertNotIn("mongo-exporter", without["services"])

    def test_mongo_rotation_changes_the_server_not_just_the_file(self):
        """
        MONGO_INITDB_ROOT_PASSWORD is read on first start only. A rotation that
        merely rewrites it and redeploys reports success while every client keeps
        working on the old password.
        """
        component = self.make_mongo()
        component._local_container = lambda: ""
        ok, out = component.rotate_password()
        self.assertFalse(ok)
        self.assertIn("first start only", out)

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


class TlsTrustTest(ComponentCase):
    """
    An application handed this platform's connection string must be able to
    OPEN it. That is one property and it spans two components, so it is tested
    where both are in scope rather than inside either one.
    """

    def test_an_application_can_verify_the_database_it_is_handed(self):
        """
        The panel published `tls=true` and mounted the authority into the
        database's own containers and nowhere else. Every application that
        pasted the string got `self-signed certificate in certificate chain`,
        because a per-component authority is in no public root store and the
        app had no copy of it.
        """
        url = self.make_mongo(name="docs").connection_url()
        self.components.base.docker_out = lambda argv: "docs-tls-ca-v1\ndocs-tls-1-v1\n"

        rendered = self.make_app().render()
        service = rendered["services"]["app"]
        mounts = {s["target"]: s["source"] for s in service["secrets"]}

        # Whatever path the string names, the app has a file there.
        named = [o.split("=", 1)[1] for o in url.split("?", 1)[1].split("&")
                 if o.startswith("tlsCAFile=")]
        self.assertEqual(len(named), 1, f"no tlsCAFile in {url}")
        self.assertEqual(named[0], "/run/secrets/docs-ca.crt")
        self.assertIn("docs-ca.crt", mounts)

        # And the stack declares the secret it mounts, or the deploy fails.
        self.assertEqual(rendered["secrets"],
                         {"docs-tls-ca-v1": {"external": True}})

    def test_an_application_mounts_only_authorities_and_not_member_keys(self):
        """
        A CA certificate is public. A member's certificate carries its PRIVATE
        key, and there is no reason on earth for an application to hold one.
        """
        self.components.base.docker_out = lambda argv: "docs-tls-ca-v1\ndocs-tls-1-v1\n"
        service = self.make_app().render()["services"]["app"]
        self.assertEqual([s["source"] for s in service["secrets"]],
                         ["docs-tls-ca-v1"])

    def test_an_application_with_no_database_mounts_nothing(self):
        """An empty `secrets:` key is a deploy error, not an empty list."""
        rendered = self.make_app().render()
        self.assertNotIn("secrets", rendered["services"]["app"])
        self.assertNotIn("secrets", rendered)

    def test_the_newest_authority_wins_a_renewal(self):
        """Dataguard issues v2 and retires v1; a stack naming v1 will not deploy."""
        self.components.base.docker_out = (
            lambda argv: "docs-tls-ca-v1\ndocs-tls-ca-v2\n")
        service = self.make_app().render()["services"]["app"]
        self.assertEqual([s["source"] for s in service["secrets"]],
                         ["docs-tls-ca-v2"])

    def test_the_external_url_asks_the_client_for_no_certificate_at_all(self):
        """The gateway holds that session, so neither option belongs in the
        string an outside client is handed."""
        mongo = self.make_mongo(name="docs")
        external = mongo.connection_url("127.0.0.1", 27017)
        self.assertNotIn("tlsCAFile", external)
        self.assertNotIn("tls=true", external)
        # The in-cluster string is untouched: it still gets both.
        internal = mongo.connection_url()
        self.assertIn("tls=true", internal)
        self.assertIn("tlsCAFile", internal)


class CredentialsAgreeTest(ComponentCase):
    """
    The Credentials tab and the "How to reach it" panel describe one component.
    When they are built from different expressions they drift, and the page
    then states two different answers to the same question.
    """

    def hosts_in(self, url):
        body = url.split("@", 1)[1].split("/", 1)[0]
        return [h.split(":")[0] for h in body.split(",")]

    def test_mongo_lists_the_same_members_everywhere(self):
        """The Host row named only member 1 while the URL beside it named four."""
        mongo = self.make_mongo(name="docs")
        creds = mongo.credentials()
        from_url = self.hosts_in(creds["internal_url"])
        from_row = [h.strip() for h in creds["internal_host"].split(",")]
        from_panel = [h.split(":")[0] for h in mongo.access()["target"].split(",")]
        self.assertEqual(from_row, from_url)
        self.assertEqual(from_panel, from_url)
        # One, now that the seed list is an alias — and the point of the test is
        # unchanged: whatever the string names, the Host row and the Overview
        # panel name exactly that and nothing else.
        self.assertEqual(from_url, ["docs-mongo"])

    def test_no_reach_panel_puts_a_password_on_the_overview_tab(self):
        """
        THE REASON the two strings are not identical, and it is the one reason.
        The Credentials tab hides the URL's password behind a reveal; the
        Overview tab has no reveal, so its box carries the hosts and nothing
        else. If that ever stops being true the boxes should just be merged.
        """
        for component in (self.make_mongo(name="docs"),
                          self.make_redis(name="cache", dataguard=True)):
            target = component.access()["target"]
            self.assertNotIn(component.password(), target)
            self.assertNotIn("@", target)
            self.assertNotIn("://", target)
            # ...and it still names exactly what the string names.
            self.assertEqual(
                [h.split(":")[0] for h in target.split(",")],
                self.hosts_in(component.credentials()["internal_url"]))

    def test_the_reach_panel_says_where_the_real_string_is(self):
        """It was asked twice why these differ, which means the page never said."""
        for component in (self.make_mongo(name="docs"),
                          self.make_redis(name="cache", dataguard=True)):
            self.assertIn("Credentials", component.access()["note"])

    def test_redis_lists_the_same_sentinels_everywhere(self):
        """And the Host row does not repeat the port the Port row already gives."""
        redis = self.make_redis(name="cache", dataguard=True)
        creds = redis.credentials()
        from_url = self.hosts_in(creds["internal_url"])
        from_row = [h.strip() for h in creds["internal_host"].split(",")]
        self.assertEqual(from_row, from_url)
        self.assertNotIn(":", creds["internal_host"])
        self.assertEqual(creds["internal_port"], "26379")


class MemberSlotTest(ComponentCase):
    """
    How big a set may get is not a question the form asks any more.

    It used to ask twice — `replica_pool` and `max_members` — and freeze the
    first at creation, because Mongo's seed list named every slot. These tests
    pin the two facts that replaced that: the slot count is a constant, and the
    connection string does not mention it.
    """

    def test_neither_ceiling_is_on_the_form(self):
        for cls in (self.components.RedisComponent, self.components.MongoComponent):
            names = {f.name for f in cls.fields()}
            self.assertNotIn("replica_pool", names, cls.__name__)
            self.assertNotIn("max_members", names, cls.__name__)

    def test_every_slot_is_rendered_and_all_but_one_is_stopped(self):
        # Growth is `docker service scale`, never `stack deploy` — so the slots
        # have to exist before dataguard wants one, and cost nothing until then.
        import yaml
        mongo = self.make_mongo(name="docs")
        services = yaml.safe_load(mongo.stack_yaml())["services"]
        members = [k for k in services if k[6:].isdigit() and k.startswith("mongo-")]
        self.assertEqual(len(members), self.components.base.MEMBER_SLOTS)
        # The slot count must stay ABOVE the fleet, so that what limits a set is
        # machines and read pressure rather than a number rendered into a file.
        # `MAX_WORKERS` defaults to 5, so the master plus the fleet is 6.
        self.assertGreater(self.components.base.MEMBER_SLOTS, 6)
        self.assertEqual(services["mongo-1"]["deploy"]["replicas"], 1)
        for key in members:
            if key != "mongo-1":
                self.assertEqual(services[key]["deploy"]["replicas"], 0, key)

    def test_the_mongo_string_names_one_host_and_never_a_slot(self):
        # The whole reason the ceiling could never be raised: a longer seed list
        # is a different string, and applications were holding the old one.
        mongo = self.make_mongo(name="docs")
        url = mongo.connection_url()
        self.assertIn("docs-mongo:27017", url)
        self.assertNotIn("docs_mongo-1", url)
        self.assertNotIn("docs_mongo-2", url)

    def test_every_member_answers_to_that_one_host(self):
        # Swarm resolves a shared alias to the members that are RUNNING, and
        # leaves out a service at zero replicas — verified on the cluster. That
        # is what makes one name safe as a seed list.
        import yaml
        mongo = self.make_mongo(name="docs")
        services = yaml.safe_load(mongo.stack_yaml())["services"]
        members = [k for k in services if k[6:].isdigit() and k.startswith("mongo-")]
        self.assertTrue(members)
        for key in members:
            self.assertEqual(services[key]["networks"]["edge"]["aliases"],
                             ["docs-mongo"], key)

    def test_a_member_certificate_covers_the_name_clients_dial(self):
        # A client dialling the alias checks the certificate against it. Without
        # the alias in the SAN the set works internally and stops being reachable
        # by its own connection string.
        import pki
        mongo = self.make_mongo(name="docs")
        mongo.ensure_tls()
        with open(self.components.store.path_for(
                "docs", "tls/member-2.pem"), "rb") as fh:
            pem = fh.read()
        self.assertTrue(pki.covers(pem, ["docs-mongo"]))
        self.assertTrue(pki.covers(pem, ["docs_mongo-2"]))

    def test_a_certificate_without_that_name_is_reissued(self):
        # The migration for sets that already exist: their certificates predate
        # the alias, and nothing else would notice until a client failed.
        import pki
        mongo = self.make_mongo(name="docs")
        mongo.ensure_tls()
        path = self.components.store.path_for("docs", "tls/member-2.pem")
        with open(path, "rb") as fh:
            stale = fh.read()
        self.assertFalse(pki.covers(stale, ["docs-mongo", "somewhere-else"]))
        key, crt = pki.ensure_ca(mongo.tls_dir(), mongo.cluster_name(), "docs")
        narrow = pki.issue_member(key, crt, mongo.cluster_name(), "docs",
                                  ["docs_mongo-2"])
        with open(path, "wb") as fh:
            fh.write(narrow)
        mongo.ensure_tls()
        with open(path, "rb") as fh:
            self.assertTrue(pki.covers(fh.read(), ["docs-mongo"]))

    def test_the_redis_url_never_depended_on_the_slot_count(self):
        # The premise the old restriction rested on. There are always exactly
        # SENTINEL_COUNT sentinels and the URL names those, so growing a Redis
        # set never altered the string and never needed a frozen ceiling.
        redis = self.make_redis(name="cache", dataguard=True)
        url = redis.connection_url()
        for i in (1, 2, 3):
            self.assertIn(f"cache_sentinel-{i}:26379", url)
        self.assertNotIn("cache_redis-1", url)
        self.assertNotIn("sentinel-4", url)

    def test_the_switch_is_the_only_way_to_ask_for_one_server(self):
        # What `replica_pool = 0` was going to mean, said by the control that
        # already meant it.
        import yaml
        redis = self.make_redis(name="cache", dataguard=False)
        services = yaml.safe_load(redis.stack_yaml())["services"]
        self.assertIn("redis", services)
        self.assertEqual([k for k in services if k.startswith("sentinel")], [])
        self.assertNotIn("redis-2", services)
        self.assertNotIn("cache-redis", redis.connection_url())


class ExternalEndpointTest(ComponentCase):
    """
    How a client OUTSIDE the cluster reaches a database. Two pieces, because
    there are two questions: WHICH COPY is answered by a proxy that asks every
    member who is in charge, and WHICH MACHINE by the tunnel, which is how
    every application here already survives losing a node.
    """

    def gateway_conf(self, component):
        """The HAProxy configuration as it will reach the container."""
        return component.render()["services"]["gateway"]["command"][2]

    def published_redis(self, name="c1", **spec):
        return self.make_redis(name, external_hostname="cache.example.com", **spec)

    def published_mongo(self, name="d1", **spec):
        return self.make_mongo(name, external_hostname="docs.example.com", **spec)

    # --- it exists only when asked for --------------------------------------

    def test_no_hostname_means_no_gateway(self):
        for component in (self.make_redis("c1"), self.make_mongo("d1")):
            rendered = component.render()
            self.assertNotIn("gateway", rendered["services"], component.TYPE)
            self.assertNotIn(f"{component.stack}_gateway", component.services())

    def test_the_gateway_is_global_and_unpinned(self):
        """One per node, like the tunnel connector: losing a machine loses one
        of them rather than the only one."""
        for component in (self.published_redis(), self.published_mongo()):
            deploy = component.render()["services"]["gateway"]["deploy"]
            self.assertEqual(deploy["mode"], "global", component.TYPE)
            self.assertNotIn("placement", deploy, component.TYPE)
            self.assertNotIn("replicas", deploy, component.TYPE)
            self.assertIn(f"{component.stack}_gateway", component.services())

    def test_the_gateway_is_on_the_edge_network_and_opens_no_port(self):
        """
        The connector reaches it by service DNS on `edge`, from inside. That is
        what makes the firewall irrelevant rather than merely closed.
        """
        for component in (self.published_redis(), self.published_mongo()):
            service = component.render()["services"]["gateway"]
            self.assertEqual(service["networks"], ["edge"], component.TYPE)
            self.assertNotIn("ports", service, component.TYPE)

    def test_the_gateway_carries_no_manager_label(self):
        """
        It is not a member and nothing may treat it as one. Dataguard discovers
        by `dataguard.role`, so a gateway carrying one would be counted into the
        set — the exact collision the sentinels caused.
        """
        for component in (self.published_redis(), self.published_mongo()):
            labels = component.render()["services"]["gateway"]["deploy"]["labels"]
            self.assertEqual(set(labels), {"infra.component", "infra.type"},
                             component.TYPE)

    # --- what it forwards to ------------------------------------------------

    def test_every_member_slot_is_a_backend(self):
        """
        Including the ones that do not exist yet. A slot at `replicas: 0` is a
        name that does not resolve, which `init-addr none` turns into a server
        that is simply down until dataguard starts it — so growth needs no
        reconfiguration, exactly as it needs none inside the cluster.
        """
        conf = self.gateway_conf(self.published_mongo("docs"))
        for index in range(1, self.components.base.MEMBER_SLOTS + 1):
            self.assertIn(f"server mongo-{index} docs_mongo-{index}:27017", conf)
        self.assertIn("init-addr none", conf)
        self.assertIn("resolvers swarm", conf)

    def test_an_unmanaged_redis_declares_its_one_server_once(self):
        """
        `member_key` collapses to the bare type when nothing manages the
        component, so looping over eight slots would declare the same backend
        eight times — and HAProxy refuses a duplicate server name outright,
        which is a database that will not start rather than one that misbehaves.
        """
        conf = self.gateway_conf(self.published_redis(dataguard=False))
        self.assertEqual(conf.count("\n    server "), 1)
        self.assertIn("server redis c1_redis:6379", conf)

    def test_a_demoted_primary_has_its_connections_closed(self):
        """
        Otherwise a client stays pinned to a server that has just become a
        secondary and refuses its writes, for as long as the connection lives.
        """
        for component in (self.published_redis(), self.published_mongo()):
            self.assertIn("on-marked-down shutdown-sessions",
                          self.gateway_conf(component), component.TYPE)

    def test_the_configuration_carries_no_compose_variable(self):
        """
        It is written into the service's command, and `docker stack deploy`
        interpolates `${...}` on the way past. A `$` in here would be eaten
        before docker ever read the file — which is also why the Redis password
        is sent as hex rather than as text.
        """
        for component in (self.published_redis(), self.published_mongo()):
            self.assertNotIn("$", self.gateway_conf(component), component.TYPE)

    # --- how each engine is asked "are you the primary" ---------------------

    def test_redis_is_asked_for_its_replication_role(self):
        redis = self.published_redis()
        conf = self.gateway_conf(redis)
        self.assertIn("tcp-check expect string role:master", conf)
        password = redis.password()
        auth = self.components.redis._resp("AUTH", "default", password)
        self.assertIn(f"tcp-check send-binary {auth}", conf)
        # Hex of the RESP array form, which is the one that survives a password
        # with a space in it — and the one compose cannot interpolate away.
        self.assertEqual(
            bytes.fromhex(auth),
            f"*3\r\n$4\r\nAUTH\r\n$7\r\ndefault\r\n"
            f"${len(password)}\r\n{password}\r\n".encode())

    def test_mongo_is_asked_with_the_one_message_every_version_answers(self):
        """
        OP_QUERY is removed from MongoDB for everything except the handshake,
        which is what makes it the message a health check can rely on — and it
        needs no authentication, so the proxy holds no credential at all.
        """
        import struct
        mongo = self.components.mongo
        raw = bytes.fromhex(mongo._ISMASTER_QUERY)
        length, _request, response_to, opcode = struct.unpack("<iiii", raw[:16])
        self.assertEqual(length, len(raw), "the header must state its own length")
        self.assertEqual(opcode, 2004, "OP_QUERY")
        self.assertEqual(response_to, 0)
        self.assertIn(b"admin.$cmd\x00", raw)
        self.assertIn(b"isMaster", raw)
        # `\x08ismaster\x00\x01` — the BSON boolean that means "primary". A
        # secondary sends the same element ending \x00 and does not match.
        self.assertEqual(bytes.fromhex(mongo._ISMASTER_TRUE),
                         b"\x08ismaster\x00\x01")
        self.assertIn(f"tcp-check expect binary {mongo._ISMASTER_TRUE}",
                      self.gateway_conf(self.published_mongo()))

    def test_the_gateway_holds_the_tls_session_to_the_member(self):
        """
        It is what lets an external client install no certificate — so it has to
        verify the member properly on that client's behalf: the chain against
        this component's authority, the name against the alias every member
        carries.
        """
        service = self.published_mongo("docs").render()["services"]["gateway"]
        conf = service["command"][2]
        self.assertIn("ssl verify required", conf)
        self.assertIn("ca-file /run/secrets/docs-ca.crt", conf)
        self.assertIn("verifyhost docs-mongo", conf)
        # `verify none` would make the check pass against anything at all.
        self.assertNotIn("verify none", conf)
        # ...and the authority has to actually be mounted for that to work.
        self.assertEqual([x["target"] for x in service["secrets"]], ["docs-ca.crt"])

    def test_redis_needs_no_certificate_anywhere(self):
        self.assertNotIn("secrets",
                         self.published_redis().render()["services"]["gateway"])

    # --- what the page hands you --------------------------------------------

    def test_the_page_gives_the_tunnel_target_the_command_and_the_url(self):
        """
        Three things, and the third one surprises people: the application does
        NOT connect to the hostname. The helper holds the tunnel open and
        listens locally, so the hostname belongs to the command and the driver
        gets a loopback address.
        """
        creds = self.published_mongo("docs").credentials()
        self.assertEqual(creds["external_hostname"], "docs.example.com")
        self.assertEqual(creds["external_target"], "tcp://docs_gateway:27017")
        self.assertIn("--hostname docs.example.com", creds["external_command"])
        self.assertIn("--url 127.0.0.1:27017", creds["external_command"])
        self.assertIn("@127.0.0.1:27017/", creds["external_url"])
        self.assertNotIn("docs.example.com", creds["external_url"])

    def test_the_mongo_external_url_does_not_ask_the_driver_to_discover(self):
        """`replicaSet=` would make the driver connect to overlay names it
        cannot resolve. `directConnection=true` is what stops that."""
        url = self.published_mongo("docs").credentials()["external_url"]
        self.assertIn("directConnection=true", url)
        self.assertNotIn("replicaSet", url)
        # No path into a container the client will never run in, and no read
        # preference for a secondary it has no address for.
        self.assertNotIn("tlsCAFile", url)
        self.assertNotIn("readPreference", url)
        # No TLS options either — the gateway holds that session.
        self.assertNotIn("tls", url)
        # ...and the write concern IS still there, because that is a guarantee
        # about the data rather than about the connection.
        self.assertIn("w=majority", url)

    def test_the_internal_mongo_url_still_discovers(self):
        """The change above must not have leaked into the in-cluster string."""
        url = self.published_mongo("docs").connection_url()
        self.assertIn("replicaSet=docs", url)
        self.assertNotIn("directConnection", url)
        self.assertIn("tlsCAFile", url)

    def test_the_redis_external_url_needs_no_sentinel_support(self):
        redis = self.published_redis("cache", dataguard=True)
        creds = redis.credentials()
        self.assertEqual(creds["external_url"],
                         f"redis://default:{redis.password()}@127.0.0.1:6379")
        self.assertTrue(creds["internal_url"].startswith("redis+sentinel://"))

    def test_no_hostname_means_no_url_rather_than_a_wrong_one(self):
        """A copyable URL that cannot work is worse than a blank: it sends
        somebody debugging their own firewall."""
        for component in (self.make_redis("c1"), self.make_mongo("d1")):
            creds = component.credentials()
            self.assertEqual(creds["external_url"], "", component.TYPE)
            self.assertEqual(creds["external_command"], "", component.TYPE)
            self.assertEqual(creds["external_hostname"], "", component.TYPE)

    def test_the_hostname_is_a_bare_host_or_nothing(self):
        """
        It is copied out of a dashboard, so it arrives with a scheme on the
        front and a path on the end about as often as it arrives clean.
        """
        base = self.components.base
        for given, wanted in (
                ("docs.example.com", "docs.example.com"),
                ("  DOCS.example.com  ", "docs.example.com"),
                ("https://docs.example.com/", "docs.example.com"),
                ("docs.example.com:27017", "docs.example.com"),
                # Not a hostname: a bare label has no zone, and an address is
                # what the old design asked for and this one never wants.
                ("localhost", ""),
                ("203.0.113.10", ""),
                ("not a host", ""),
                ("", "")):
            self.assertEqual(base.clean_hostname(given), wanted, given)

    # --- the certificate that makes the mongo URL usable --------------------

    def test_the_members_carry_the_names_they_are_dialled_by(self):
        """Its service name, the alias the connection string uses, and the
        loopback pair mongod's own health check needs."""
        import pki
        mongo = self.published_mongo("docs")
        mongo.render()
        with open(self.components.store.path_for("docs", "tls/member-3.pem"),
                  "rb") as fh:
            pem = fh.read()
        self.assertTrue(pki.covers(pem, ["127.0.0.1", "localhost", "docs-mongo",
                                         "docs_mongo-3"]))

    def test_the_hostname_never_reaches_a_certificate(self):
        """It is the tunnel's name, not the database's. In the SAN it would tie
        the certificates to where the client happens to be."""
        import pki
        mongo = self.published_mongo("docs")
        mongo.render()
        with open(self.components.store.path_for("docs", "tls/member-1.pem"),
                  "rb") as fh:
            self.assertFalse(pki.covers(fh.read(), ["docs.example.com"]))

    def test_a_hostname_that_does_not_parse_is_refused_rather_than_ignored(self):
        """
        Silently blanking it would mean typing a hostname, pressing save, and
        watching nothing happen — with the External panel still saying
        "in-cluster only" and no reason given anywhere.
        """
        for index, type_name in enumerate(("redis", "mongo")):
            _component, problems = self.components.create(
                type_name, f"x{index}", {"external_hostname": "203.0.113.10"})
            self.assertTrue(any("not a tunnel hostname" in p for p in problems),
                            f"{type_name}: {problems}")

    def test_every_step_says_what_happens_if_you_skip_it(self):
        """
        A step that only says what to type is a step people skip, and one of
        these is the difference between a database behind a door and a database
        on the internet with a password.
        """
        for component in (self.published_redis(), self.published_mongo()):
            steps = component.credentials()["external_steps"]
            self.assertGreaterEqual(len(steps), 3, component.TYPE)
            for index, step in enumerate(steps, 1):
                where = f"{component.TYPE} step {index}"
                self.assertTrue(step["title"], where)
                self.assertGreater(len(step["note"]), 40, where)
            # The one that is security rather than plumbing, said in the step
            # itself rather than in a note underneath the whole panel.
            self.assertTrue(any("ACCESS POLICY" in s["note"] for s in steps),
                            component.TYPE)

    def test_the_last_step_says_why_it_is_a_local_address(self):
        """`127.0.0.1` in a connection string looks like a mistake until the
        reason is beside it."""
        for component in (self.published_redis(), self.published_mongo()):
            last = component.credentials()["external_steps"][-1]
            self.assertIn("127.0.0.1", last["code"], component.TYPE)
            self.assertIn("helper", last["note"], component.TYPE)

    def test_neither_engine_asks_the_client_to_install_anything(self):
        """Three steps, both engines, and no certificate among them."""
        for component in (self.published_redis(), self.published_mongo()):
            steps = component.credentials()["external_steps"]
            self.assertEqual(len(steps), 3, component.TYPE)
            for step in steps:
                self.assertNotIn("file", step, component.TYPE)
                self.assertNotIn("tlsCAFile", step["code"], component.TYPE)

    def test_an_unpublished_database_offers_no_steps(self):
        for component in (self.make_redis("c1"), self.make_mongo("d1")):
            self.assertEqual(component.credentials()["external_steps"], [],
                             component.TYPE)
