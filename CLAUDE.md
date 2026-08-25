# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Infrastructure-as-config for a self-managing Docker Swarm on Hetzner Cloud. It is **not** an
application repo: the deliverable is a bundle of files copied to `/opt/infra` on a manually-created
master node, where `bootstrap.sh` (embedded in `master-cloud-init.yaml`) turns them into a running
cluster.

The master boots **infrastructure only** — swarm, monitoring, the tunnel connector, the admin panel.
No application, no database, nothing of yours. Those are **components**, created afterwards from the
panel or with `bin/component`, one independent Docker stack each.

Read `README.md` first — it holds the operational rationale. This file covers what the README
doesn't: how the pieces are wired, and which of the wires are load-bearing.

## Working on this repo

There are real tests now, and they cover the parts where a bug is silent:

```bash
# the component library: rendering, validation, isolation, credentials
python3 -m unittest discover -s admin/tests -v

# the autoscaler's decision logic: policy parsing, the packer, admission, fleet arithmetic
python3 -m unittest discover -s autoscaler/tests -v

# alerting — run after ANY alerts.yml change
promtool test rules config/alerts_test.yml
promtool check rules config/alerts.yml
amtool check-config config/alertmanager.yml

# syntax
python3 -m py_compile autoscaler/autoscaler.py admin/*.py admin/components/*.py
bash -n bin/*
docker build -t autoscaler:test autoscaler/

# the panel, offline, against dummy data — no docker, no cluster
PREVIEW=1 ADMIN_PASSWORD=dev python3 admin/app.py     # http://localhost:3000
python3 admin/preview_build.py                        # -> admin/preview/index.html

# a component's rendered stack, without a cluster
bin/component render api | docker compose -f - config
```

Match tool versions to the deployed ones (`promtool` 2.x, `amtool` 0.27.0) — `api_url_file` and the
`matchers` route syntax are version-sensitive. The bootstrap script is embedded in
`master-cloud-init.yaml` as a `write_files` entry, so syntax-check it by extracting the YAML value
and running `bash -n` on it; editing it in place is easy to get subtly wrong.

`DRY_RUN=true` in `infra.env` makes the autoscaler log the Hetzner create/delete calls it would make
without issuing them. That is the safe way to exercise a scaling change against live metrics.

## Deployment flow

1. Fill the `VARIABLES` block at the top of `master-cloud-init.yaml`. Every `REPLACE_ME` must be set.
   Nothing in it names an application.
2. Create a Hetzner CPX31 on the private network with that cloud-config. `runcmd` expects the bundle
   to already be in `/opt/infra` — the clone line is commented out on purpose.
3. `bootstrap.sh` preflight-checks the required variables, firewalls, installs Docker,
   `docker swarm init`s on the private IP, creates the docker secrets, creates
   `/opt/infra/components`, creates the `edge` overlay, renders the templates, builds the
   `autoscaler` and `admin` images, and deploys **monitoring → ingress → admin**.
4. `docker service ls` then shows infrastructure and nothing else. That is the finished state of a
   boot, not a half-finished one.
5. Components are created afterwards, and each deploys itself.

## Architecture invariants

These constraints span multiple files. Breaking one fails silently rather than loudly.

**One component, one stack, one directory, one writer.** A component owns
`/opt/infra/components/<name>/` (`component.json`, `env`, `secret.env`, `stack.yml`) and the Swarm
stack `<name>`, whose services are `<name>_<type>` plus that type's sidecars (`<name>_app`,
`<name>_redis` + `<name>_redis-exporter`, `<name>_mongo` + `<name>_mongo-exporter`). Nothing is
shared between components, which is the whole point: the previous design had one
`config/app-<env>.env` written by both the application's environment editor and the database's
credentials form, so a page rendered before someone else's save wrote their change back out. Two
writers on one file is the bug class this model exists to remove — do not reintroduce a shared file.

**Discovery is by label, never by name.** The autoscaler manages every service carrying
`infra.workload=app`, and reads that service's entire scaling policy from its own `autoscale.*`
deploy labels. There is no `APP_SERVICE`. Anything without the label is overhead: never pinned,
never scaled, its reservations simply subtracted from whatever node it sits on. The opt-in is
deliberately one-directional — a forgotten label leaves an app on the master, correctly accounted
and visible; a label on a stateful service moves a volume onto a worker that later gets deleted.
**A database therefore carries no `infra.workload` label** and keeps `node.role == manager`; both
Redis and MongoDB set `KEEPS_VOLUME`, which is what makes the delete form say the volume survives
without naming a type.

**The renderer never invents placement, replicas or the image.** `Component.render()` reads the live
image (CI owns it), the live replica count (the autoscaler owns it) and the live `node.role`
constraint (the autoscaler owns it) back from the running service and re-states them. Emitting the
spec's copy is how a redeploy triggered by an unrelated setting rolls production back
([docker/cli#2235](https://github.com/docker/cli/issues/2235)). Tests pin all three.

**Reservations are mandatory and load-bearing twice.** Swarm's scheduler subtracts them when placing
a task, *and* they are the entire input to the autoscaler's capacity arithmetic.
`Component.resources()` refuses to render a service without them. A component with none makes its
node look idle, and app replicas get packed on top of VictoriaMetrics.

**A component's credentials are `Secret` declarations, not `Field`s.** A Field's value is written to
`component.json`, which is 0640 and is the file someone pastes into an issue; a password must never
be in there. `SECRETS` are written to `secret.env` at 0600 by `apply_secrets()`, which treats blank
as "generate" both at create time and afterwards — a database reachable with no password by leaving
a form field empty is not a state worth having. The panel renders a Credentials tab for any type
that declares them and none for a type that does not, so a future Postgres needs no route change.
The displayed connection URL percent-encodes the password, because a user-chosen one containing `@`
turns `redis://default:p@ss@host` into a different host and an error that blames DNS.

**Capacity is measured in CPU/memory, never in replicas.** Every quantity is a `Res(nanocores, bytes)`
integer vector: a node's free capacity is what it advertises minus the reservations of every task on
it that is *not* an app; demand is the sum over services of replicas × that service's own
reservation. Replica-unit capacity cannot express two applications of different sizes at all, and
floats in the sum produce a one-replica flap nobody can reproduce. There is no
`REPLICAS_PER_WORKER`, no manager-capacity constant and no headroom constant — all three existed,
all three were wrong, and all three were guesses about hardware the autoscaler can read directly.

**`edge` and `monitoring` are external overlays created before any component exists.** `edge` by
`bootstrap.sh` with `docker network create`, `monitoring` by `stacks/monitoring.yml`. No component
may own a network another component needs — when `edge` belonged to the app stack, removing that
stack took the tunnel's route to everything with it.

**Charts key on a component's CATEGORY, never on its TYPE.** `_CATEGORY_BANDS` in `swarm.py` maps
`Application`/`Data` to a colour band and `short_service()` folds `<stack>_<type>` to `<stack>` from
`components.TYPES`. Both used to be literal tuples listing `app` and `redis` — a second registry, so
a type added to `TYPES` and not to them went grey on every chart and lost its short name.
`test_every_component_type_gets_a_colour_band_and_a_short_name` is that assertion.

**Service discovery for metrics is label-driven, never enumerated.** `config/vmagent.yml.tpl` keeps
any Swarm task carrying `prometheus.scrape/port/path` deploy labels, and `node-exporter`/`cadvisor`
run `mode: global`. The component renderer emits those three labels, so a new component is scraped
with no edit to the scrape config. The `service` label vmagent writes is what every per-component
alert and query joins on.

**Logs bypass any agent.** Every stack uses the `loki` Docker log driver pointed at
`http://${MASTER_PRIVATE_IP}:3100`, which is why Loki publishes port 3100 in `mode: host` and why
the plugin is installed in both cloud-inits.

**The master is not a worker.** `MIN_WORKERS`/`MAX_WORKERS` count Hetzner worker servers, and the
master is the control plane that happens to carry the load while none exists. `MIN_WORKERS=0` is the
free floor — nothing billed, master serving; `MIN_WORKERS=1` keeps one worker up, which is also the
only way to say "the master runs no application traffic". `AutoscalerAtMax` compares workers to
workers, so `autoscaler_current_hosts` is now purely informational and no threshold keys on it. At
the floor, losing the master costs uptime and not just scaling — the deliberate price of it.

**`cloudflared` is `mode: global` with no constraint**, in its own `ingress` stack. Global so the
master always has a registered connector and the tunnel does not gap during a handover; its own
stack so an application you have not built yet cannot take ingress — and with it the panel's own
hostname — down with it.

**Routing is manual and that is a choice.** The tunnel runs in token mode, so its ingress rules live
in the Cloudflare dashboard. The panel's job is to show you the exact local target
(`http://<name>_app:<port>`, service DNS on `edge`) to paste. Nothing here calls the Cloudflare API.

## Autoscaler control flow

`autoscaler/autoscaler.py` is a single-file loop (`loop()`, every `LOOP_SECONDS`) in a strict order.
The ordering is the whole gaplessness argument; reversing any of it causes an outage.

1. `reap_orphans()` — delete Hetzner servers that never joined, and swarm nodes whose server is gone.
2. **Inventory** (nodes + one `index_tasks()` call). Failing here is a HOLD: without it every later
   number is a guess.
3. `discover_workloads()` — services labelled `infra.workload=app`, with their policies.
   **Discovery raising and discovery returning zero services are opposite events**: an API error
   must never be read as "no demand" and delete the fleet, while an honest empty result is the
   normal state of a fresh cluster and must scale to the floor cleanly.
4. **Emergency**, before anything that can fail: pinned to workers with an empty fleet means every
   task is unplaceable, so release every pin immediately. This is the one path that must survive
   VictoriaMetrics or Hetzner being down, which is why it is at the top and ignores
   `update_in_progress`.
5. Capacity, per node. A node whose tasks cannot be listed contributes **zero free**, not zero used —
   believing in room that does not exist is how tasks end up pending.
6. `read_signals_batch()` — every service's p95 and CPU-per-replica in a handful of `by (service)`
   queries. Per-service queries do not fit in a 60s loop and `AutoscalerStalled` fires at 300s.
7. Tier 1 per service, each isolated: `desired_replicas()` → **want**.
8. `workers_needed()` from the **uncapped** want. Returns 0 when the master alone can hold
   everything, and never 1-because-one-more-would-do: past the master, the workers cover the whole
   demand including what the master was carrying.
9. `admit()` against the currently eligible nodes → **admitted**.
10. Apply in order: **release the pin (scaling in) → create nodes → set replicas → add the pin
    (scaling out) → remove nodes**.

**Want, hosts and admitted are three different numbers.** Collapsing want and admitted — which the
previous version did, mutating one variable in place — deadlocks with several services: each gets
capped to fit, the total never exceeds capacity, and no worker is ever bought. **Admission caps
growth and never scales anything down**; shrinking belongs to tier 1 and to node removal.

**One packer, used three times.** `place()` answers admission, fleet sizing and removal. Two
algorithms that disagree by one replica are a loop that buys a worker and immediately deletes it.
Allocation is round-robin in `(priority, name)` order — deterministic and monotone, so more capacity
never reduces anyone's allocation, which is what stops oscillation. Ranking by "worst breach first"
couples allocation to a noisy signal and hands a replica back and forth every 60 seconds.

**App tasks already on a node are not subtracted from its free capacity.** `node_free_for_apps()` is
"total room for apps here" and demand counts every replica including the ones already placed, so the
two sides never double-count. Demand comes from desired counts, never live tasks, so a start-first
rollout briefly doubling a footprint cannot inflate it.

**Placement is reconciled every loop, for every service** — not only on transitions. That fixes two
real bugs: a second service that had drifted out of the pin was never reconciled, and a service
created while the cluster was already in worker mode was never pinned at all.

- The pin is released *before* the last worker is deleted, and removal additionally blocks until a
  replica of **every** app service is running on the master — waiting indefinitely rather than
  trading uptime for a few euros.
- The pin is added only once workers are `ready` *and* can hold every replica.
- `pick_removal_candidate()` tests each candidate with the packer rather than comparing sums; with
  heterogeneous costs a sum is meaningless. It takes the master's free capacity as a bin only when
  every app is unpinned — without that the LAST worker is never removable.
- `provisioning_workers()` counts booting servers towards the fleet. Sizing off ready workers alone
  re-orders the same capacity every loop for two minutes and sails past `MAX_WORKERS`.

Node CPU is deliberately never a scaling trigger — it averages in exporters, the tunnel connector
and every other component. CPU-per-replica exists because `histogram_quantile` over an empty bucket
returns nothing, so at low traffic the latency signal vanishes and something must authorise scaling
back down. Its denominator is each service's own CPU limit read from the live spec, which is what
finally deleted `APP_CPU_LIMIT` and its hand-synchronisation warning.

Removal is LIFO: newest node first, least likely to hold warm state, and Hetzner bills hourly.

**The autoscaler's blast radius is the Hetzner label selector** `cluster==<APP_NAME>,role==swarm-worker`.
`reap_orphans()` deletes servers matching it that never joined the swarm. Anything else in the same
Hetzner project is protected only by that label.

**It drains and deletes only the nodes it owns, and ownership is a swarm label.** `adopt_workers()`
stamps `managedby=autoscaler` on every worker whose server carries that selector, so the label is
*derived* from the blast radius rather than configured next to it. `pick_removal_candidate()` only
considers owned nodes, `remove_worker()` repeats the check before it drains, and `reap_orphans()`
only removes swarm entries it owns. A worker someone joined by hand is therefore capacity the packer
may use and machinery it will never touch — which is what it always should have been; before the
label, nothing but "nobody has joined one yet" protected it. Adoption happens in `reap_orphans()`,
first thing in the loop, so the only unstamped window is a node that joins and dies within one
loop — and the cost of that is a leaked swarm entry, never a deleted node.

**The value is an owner NAME, not a flag.** A node stamped `managedby=dbmanager` later is refused by
exactly the same equality check with nothing in `autoscaler.py` to change, and the panel reserves the
whole key rather than blacklisting one value. That is the entire extension point — resist adding a
registry of owners until a second one actually exists.

## Deploy paths

There are exactly two, and they must not be confused:

- **`bin/stack-deploy <monitoring|ingress|admin>`** — the infrastructure. Re-applying the file is
  always safe because nothing else owns state in it. It refuses anything else.
- **`bin/component deploy <name>`** — one component. Renders its stack from its spec, with the live
  image, replica count and placement pinned, and deploys only that stack.

The panel shells out to both and calls `docker stack deploy` itself nowhere.

## The admin panel is a root console

`admin/` is a Flask app pinned to the manager with the docker socket mounted. It can deploy anything
and read every credential in the cluster, so changes to it are security changes:

- Every mutating route is POST + CSRF-checked, **including `/logout`**.
- Credentials arrive as docker secrets, never from the image or compose file. Login comparison is
  constant-time with a per-process PBKDF2 salt. **Every third failure from an address locks it out,
  and each lock is twice as long as the last** (60s, 2m, 4m … capped at 24h, forgotten after a day
  without a failure). A fixed window could be ground against forever at N tries per window; the
  doubling is what makes guessing stop paying. A much looser global cap still exists, because
  per-IP counting is evaded by anyone who can vary `CF-Connecting-IP`.
- Gunicorn runs **one worker on purpose** — the lockout counter lives in memory, so a second worker
  would hand an attacker a second lockout budget.
- **The routes are generic.** `/components/<name>` renders whatever tabs the component declares,
  `/components/<name>/action` dispatches whatever verbs it offers, and both forms are built from
  `fields()`. Adding a type is a class plus one line in `TYPES` — if you find yourself adding
  `if component.TYPE == ...` to a route or template, the abstraction is in the wrong place. The
  Credentials tab keys on `SECRETS` being non-empty and its contents come from
  `Component.credentials()`; the delete form keys on `KEEPS_VOLUME`. Both were `TYPE == "redis"`
  until a second database made them wrong.
- **An action carries its own weight and its own precondition.** `actions()` returns
  `action(run, label, confirm, tone, when)` per verb. `tone` is why the template no longer decides
  redness by matching verb names, and `when` (`"running"` / `"stopped"` / `None`) is how Stop and
  Deploy are a mutually exclusive pair rather than a toggle that has to guess which half it is.
- **Stop is `docker stack rm`, never `--replicas 0`.** A service scaled to zero is still discovered
  by the autoscaler, which reads its own floor off the policy labels and restores every replica
  within a loop. Removing the services removes them from discovery; the spec, environment,
  credentials and volumes are files and are all still there, so Deploy brings it back unchanged.
- **A node's page is where availability and labels are set.** `node.labels.<k> == <v>` in a
  component's extra constraints matches nothing until something sets that label on a node, and until
  `/cluster/nodes/<id>` existed there was no way to. `update_node()` reads the whole NodeSpec,
  changes one field and writes it back — Docker replaces rather than merges, so a partial payload
  drops every label or demotes the manager. Removing a node is refused while it is `ready`: the
  server behind a worker belongs to the autoscaler's reaper. The page links back to whichever screen
  linked to it — Overview's fleet map passes `origin`, the Cluster tab is the default — because
  landing on the wrong one of the two happens on every single visit otherwise.
- **`managedby` is a permission, and the panel treats the whole key as reserved.** Setting it by hand
  on a node the autoscaler did not create hands that node to the reaper; dropping it from one that it
  did leaks a server nothing will ever remove. So the label form does not render the row,
  `validate_labels()` refuses the key, and — because `update_node()` REPLACES the label map —
  `shape.merge_labels()` puts the live value back into whatever the form posted. All three, not one:
  hiding the row alone would delete the label on every save.
- **Availability is editable only while nobody else owns the node.** The autoscaler drains a worker
  as the first step of deleting it and re-reads availability every loop, so a value set here on one
  of its nodes is reverted underneath you. The control is faded and `disabled` with the reason next
  to it — a control that silently loses is worse than no control.
- **The header shows the newest image asked for above the one running.** `running` is read off the
  service, so it is a success by construction — a failed or in-flight deploy leaves the previous
  image up and the header looked healthy. The second line is the newest history entry whatever
  became of it, and the Map tab colours each replica by the tag it is running, so a rolling update
  is two colours and a stuck one is two colours that stay.
- **The theme is applied before the first paint, from `<head>`.** `app.js` loads at the end of
  `<body>`, so a saved light theme used to be applied only after the dark markup had been drawn —
  one white flash per navigation, on every tab. The inline script in `base.html` and `login.html`
  sets `data-theme` on `<html>`, which carries a full token set of its own; the body class `app.js`
  adds afterwards resolves to the same values. Moving that script back down reintroduces the flash.
- **The infrastructure version lives in the rail, under the theme button.** It qualifies every page,
  not the Cluster tab, so it comes from the `infra` context global rather than one route's argument.
  It is `_rail_version.html`, included by `base.html` **and** `preview.html`. The preview builds its
  own rail rather than extending base, so anything written only into base ships in the live panel and
  is silently missing from the artefact people actually review — which is exactly what happened to
  this block. Any new rail furniture goes in a partial for the same reason.
- **The create form and the Settings tab are the same partial.** `_spec_form.html` renders
  Configuration and the autoscale policy from `fields()` against a `spec` mapping; the two used to
  carry their own copies and drifted immediately. The one difference that is real: `managed` fields
  are shown on create and hidden afterwards, because before anything has run there is no live value
  to preserve and no image to deploy, while after it there is something overwriting them every few
  minutes. That is the `creating` flag, and it is the only branch the partial has.
- **"Managed for you" is not rendered when it is empty.** Both databases declare no managed fields,
  so both had a titled empty box on their Settings tab.
- **The confirm dialog fails closed.** `data-confirm` opens a `<dialog>` — rounded warning or bin
  glyph, Cancel focused, Escape and focus-trapping from the platform. If it cannot be built or
  opened, `ask()` returns false and the caller falls back to `window.confirm()`; there is no path
  where a delete submits unasked. It re-submits with `requestSubmit()` rather than `submit()`, so the
  other submit listeners — the preview notice — still fire on a confirmed form.
- `admin/components/` is **stdlib + PyYAML only**, so `bin/component` can import it on the host
  where Flask and docker-py are not installed. Live status lives in `swarm.py`, which is panel-only.
- `admin/shape.py` holds anything derived from a service dict, because `swarm.py` and `fixtures.py`
  are swapped for each other and anything computed in one and not the other is a drift waiting to
  happen — that is exactly how `history()` came to exist only in the fixtures while the live panel
  500'd on it.
- Only the open tab's expensive data is fetched. Logs shell out with a 30s timeout and the firewall
  probe is an SSH round trip with another; doing both on every request made the Overview tab wait
  for two things it does not render.

`admin/preview_build.py` renders the real templates against `fixtures.py` into a single
self-contained `admin/preview/index.html`, and seeds **real components** into a temp `INFRA_DIR` so
the preview drives the actual classes and renderer. It fails the build if any live form action
leaks into the output. Rebuild it after template changes.

## Alerting must fail loudly

The alerting path was rebuilt after all three of its parts turned out to be inert. Keep these
properties:

- **The destination is optional, and its absence is loud.** Bootstrap used to refuse to run without
  it; that was too blunt, because a cluster you cannot bring up is worse than one that tells you
  what is missing. Now `bin/stack-deploy` installs `config/alertmanager-none.yml` — a receiver that
  visibly drops everything — warns on every deploy, `smoke-test` fails on it, and the panel's Alerts
  page says so. The property to keep is not "refuse to boot", it is **never look like it is working
  when it is not**. Never reintroduce a placeholder receiver that looks configured.
- **The Telegram bot token is rendered into `config/alertmanager.yml` (0600), not a docker secret.**
  A secret cannot be absent — referencing one would stop the whole monitoring stack deploying while
  alerting is unconfigured — and cannot be changed in place, so the panel could never edit it. That
  is also why `bin/stack-deploy` does the rendering rather than `bootstrap.sh`: editing the
  destination in Settings redeploys monitoring, and the config it mounts has to be rebuilt in
  between. `parse_mode` is empty on purpose; Telegram 400s the whole message when HTML does not
  parse, and alert text routinely contains `<`, `>` and `&`.
- **Absent series must alert.** With components created at runtime, `or vector(0)` cannot do this —
  it can only name one service. The anchor is `autoscaler_service_running_replicas`, a gauge the
  autoscaler reads from the Docker API, which exists for as long as the component does. So "every
  replica is gone" is the value 0 rather than a missing series.
- **Cluster gauges stay unlabeled; only per-service quantities gain a `service` label.**
  `AppStrandedWithoutWorkers` is a plain `and` on the empty label signature — give
  `autoscaler_placement_worker_mode` a label and it matches nothing and goes permanently silent.
  Its four test cases are unchanged from before the refactor precisely as that assertion.
- **`ReplicaCeiling` needs its guards.** A fixed-replica component sits at `current == max == min`
  forever and would page continuously without `autoscale_enabled == 1` and `max > min`; the rule
  then gets muted and means nothing.
- **`Watchdog` fires permanently by design** and is routed to its own 24h receiver. Its silence is
  the signal; do not "fix" it.

**`autoscaler_current_workers` and `autoscaler_current_hosts` are different numbers.** The first
counts Hetzner servers and reaches 0 at the floor; the second adds the master and is never below 1.
Collapsing them made the stranded alert unfireable.

Guard all of it with `promtool test rules config/alerts_test.yml`. The suite is mutation-tested:
dropping the `ReplicaCeiling` guards or labelling the stranded gauge both fail it.

## Remaining known gap: secret rotation

There is no way to change a **docker** secret. `mk_secret` in `bootstrap.sh` is
`docker secret create ... || true`, so re-running bootstrap with a new `CF_TUNNEL_TOKEN` silently
does nothing — and Swarm secrets are immutable, so it could not update in place anyway. Rotating one
means creating a versioned secret (`CF_TUNNEL_TOKEN_v2`), editing the stack file, and redeploying.

This applies to the infrastructure credentials only (tunnel token, Hetzner token, Grafana, alert
webhook, admin login). A component's own credentials are not docker secrets: a Redis password lives
in that component's `secret.env`, and rotating it is a button that regenerates the value and
redeploys one stack. Nothing else holds a copy, so nothing else breaks — which is what made the
button possible.

MongoDB is the exception that proves the shape. `MONGO_INITDB_ROOT_PASSWORD` is read on the FIRST
start only, off an empty data directory, so rewriting `secret.env` and redeploying changes nothing
for an existing volume. `MongoComponent.rotate_password()` therefore runs `db.changeUserPassword` in
the live server first and only then saves and redeploys — a straight copy of the Redis button would
report success while every client kept working on the old password. Its sibling trap is the
WiredTiger cache: unset, it sizes itself from the HOST's memory and ignores the container limit, so
`--wiredTigerCacheSizeGB` is rendered explicitly and validated below the memory reservation.
