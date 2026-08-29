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

    def test_redis_published_port_is_host_mode(self):
        rendered = self.make_redis(external_port=46379).render()
        self.assertEqual(rendered["services"]["redis-1"]["ports"], [{
            "target": 6379, "published": 46379, "protocol": "tcp", "mode": "host"}])

    # --- managed databases --------------------------------------------------

    def test_the_seed_list_names_every_member_including_the_absent_ones(self):
        """
        The property the whole design rests on. A driver ignores a seed it
        cannot resolve and discovers the set from the ones it can, so member 4
        can be a name that means nothing for six months and then mean a machine
        in Helsinki — with nothing that talks to the database changing.
        """
        component = self.make_mongo(replica_pool=3)
        url = component.connection_url()
        for index in range(1, 5):
            self.assertIn(f"docs_mongo-{index}:27017", url)
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
        self.assertEqual(labels["dataguard.pool"], "4")
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
        command = self.make_mongo(cache_mb=512).render()["services"]["mongo-1"]["command"][2]
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
