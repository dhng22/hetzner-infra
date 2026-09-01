# The autoscaler, measured against Kubernetes

Kubernetes has had a decade and a very large number of production clusters to
find the sharp edges in autoscaling. This document lines this cluster's scaling
up against **HPA** (horizontal pod autoscaler), **Cluster Autoscaler** and
**VPA**, names what is genuinely missing, and — just as carefully — names where
copying Kubernetes would make this system worse.

Nothing here is implemented. It exists so the next change to the scaling loop is
a choice rather than an accident. The ranked candidates are at the bottom.

Everything below cites the code it describes. Where a Kubernetes default is
quoted it is the upstream default, not a claim about anyone's cluster.

---

## 1. Where the decision is actually made

Kubernetes puts the whole horizontal decision in one controller. Here it is
deliberately split across three processes, and the split is the first thing to
understand because most of the comparison follows from it:

| Step | Where | What it produces |
|---|---|---|
| Is this service busy, and if slow, whose fault | `signals/classify.py` `decide()` | a **direction**: up / down / hold, with a reason |
| How many replicas that direction means | `signals/workloads.py` `desired_replicas()` | a **count** |
| Whether the cluster can hold that count | `overseer/overseer.py` `admit()` | a **ceiling**, plus a placement mode |
| Writing it to Swarm | `autoscaler/autoscaler.py` `target_replicas()` / `set_replicas()` | the actual scale call |

`signals/` is shared source imported by both the overseer and the autoscaler
(`signals/workloads.py:516-521`), so the two processes cannot disagree about what
a service wants — which matters, because the overseer buys machines for replicas
the autoscaler is the only thing that creates.

Kubernetes has no equivalent of the direction/count split, and no equivalent of
the cause attribution in §4.

---

## 2. HPA: the algorithm

**Kubernetes.** The core is a ratio:

```
desiredReplicas = ceil( currentReplicas × currentMetricValue / desiredMetricValue )
```

with a **10% tolerance** band (no action inside it), and, since v2,
`behavior.scaleUp` / `behavior.scaleDown`:

* `stabilizationWindowSeconds` — the controller keeps a history of its own
  recommendations and picks the **max over the window** when scaling down (default
  300s) and the **min over the window** when scaling up (default 0s).
* `policies` — `type: Percent` or `type: Pods`, a `value`, and a `periodSeconds`;
  `selectPolicy: Max | Min | Disabled` picks between them. Defaults: scale up by
  100% or 4 pods per 15s, whichever is larger; scale down unrestricted.
* Multiple metrics: compute the desired count for each, **take the maximum**.

**Here.** Direction first, then a fixed step
(`signals/workloads.py:512-535`):

```python
if direction == UP:
    step = max(1, int(current * policy.up_factor))     # up_factor default 0.5
    return min(policy.max_replicas, current + step)
if direction == DOWN and current > policy.min_replicas:
    return current - 1
```

Stabilization is on the **signal**, not on the recommendation
(`overseer/overseer.py:613-661`): `held` is `min_over_time` over `sustain_up`
(default 90s) and `peak` is `max_over_time` over `sustain_down` (default 900s) —
"up fast, down slow, and never symmetric", which is the same intent as HPA's
asymmetric stabilization windows arrived at from the other side. On top of that
there is a flat per-service cooldown (`autoscaler.py:926-930`, default 60s).

### The real gaps

1. **No ratio math.** A service at 4× its target takes several loops of
   `×1.5` steps to get where HPA would go in one. Under a step change in load
   this converges visibly slower, and the shape of the ramp does not depend on
   how far over the line the service actually is.
2. **Scale-down is always −1.** `20 → 2` is eighteen cooldown periods. HPA's
   `Percent` policy would do it in a handful. This is the single largest
   practical difference and it costs money continuously, not just at peaks.
3. **No policy/period model.** `up_factor` + `cooldown` is one policy with two
   knobs; there is no way to express "at most 4 at a time" or "at most 10% per
   minute", and no `selectPolicy`.
4. **No recommendation history.** Stabilization smooths the input, not the
   output. A metric that is genuinely spiky at a timescale shorter than the
   window is smoothed; one that steps up and down either side of the window is
   not, and the replica count follows it.
5. **No multi-metric max.** CPU, memory and latency are combined by an
   `or` into a direction (`signals/classify.py:196-204`), so the count that
   comes out is the same whichever of them fired. HPA computes a count per
   metric and takes the largest.

---

## 3. Cluster Autoscaler: the fleet

**Kubernetes.** Scale-up is triggered by **unschedulable pods** — the scheduler
fails, and CA asks "would a node from group X make this pod schedulable". Scale
down removes a node when its utilisation is below
`--scale-down-utilization-threshold` (default 0.5) for
`--scale-down-unneeded-time` (default 10m), respecting PodDisruptionBudgets,
`cluster-autoscaler.kubernetes.io/safe-to-evict`, and
`--scale-down-delay-after-add` (default 10m). Which node group grows is decided
by an **expander**: `random`, `most-pods`, `least-waste`, `price`, `priority`.

**Here.** Sizing is computed **before** placement, from what services want rather
than from what failed to schedule (`overseer/overseer.py:2747-2769`,
`workers_needed()` `:1423-1454`). One bin-packer — first-fit over bins ordered
`(is_manager, -free.cpu, key)` (`place()` `:1287-1310`) — is used three times: for
admission, for fleet sizing, and for deciding whether a node can be removed
(`fits_without()` `:1646`). Scale-down asks the packer "does this node's load fit
elsewhere" (`pick_removal_candidate()` `:1706`) rather than thresholding
utilisation.

Cooldowns: `COOLDOWN_UP_SECONDS` 300, `COOLDOWN_DOWN_SECONDS` 900,
`NODE_RESIZE_COOLDOWN_SECONDS` 900 (`overseer.py:222-223`, `:253`).

### Gaps

1. **No pending-workload trigger.** Pending tasks are exported as a metric
   (`S_PENDING`, `autoscaler.py:193`) and nothing acts on them. A task that
   cannot be placed for a reason the packer's model does not capture — a
   constraint, an exhausted port, a volume — waits forever without growing the
   fleet. The packer being right about capacity is doing a lot of work here.
2. **No node groups.** One `HCLOUD_LOCATION` and one server family
   (`_family()` `:998`). No zones, no mixed pools, no expander choice, and no
   spot/price tier.
3. **No PodDisruptionBudget equivalent.** The nearest thing is
   `would_lose_last_replica()` (`:1666`) plus a warning telling you to raise that
   service's minimum to 2 (`:2221-2225`). That covers the common case and not the
   general one.
4. **No per-node utilisation threshold.** Arguably correct — see §4 — but it does
   mean there is no simple knob for "leave 30% headroom".

---

## 4. Where this system is ahead, and should not be "fixed"

These are not gaps. They are places where copying Kubernetes would be a
regression, and they are written down here so nobody removes them in the name of
parity.

**Cause attribution.** `attribute()` (`overseer.py:664-701`) decides whether a
latency breach is the service's own fault (`local`), its database's, an
upstream's, or unknown, and routes the non-local ones to whoever claims them via
the `infra.handles` label (`signals/classify.py:24-33`). HPA has no notion of
this: point it at a latency-derived custom metric and it will happily scale a
service to twenty replicas because its database is slow — which makes the
database slower.

**The busy guard.** UP on latency requires the replicas to *also* be working
(`saturated()` `classify.py:45-54`, used at `:196-204`). The docstring records
the incident it exists for: one 904ms request from one Android tester scaled a
service 2→3→4 at 11% CPU. HPA on a latency metric has nothing equivalent, and
this is the single most valuable thing in the loop.

**Missing data holds, never shrinks.** `quiet_cpu` requires a non-None peak
(`classify.py:210-216`), so a cadvisor gap cannot read as "idle". A metrics
outage here does not silently drain the cluster.

**Vertical node resize.** `plan_resize()` (`:2141-2246`) drains a worker, powers
it off, changes the Hetzner plan and brings it back, as a phase machine with
per-phase deadlines (`RESIZE_DEADLINES` `:1831`). Cluster Autoscaler has no
concept of resizing a node — it can only add and remove them.

**A real zero-worker floor.** `workers_needed()` returns 0 when the master alone
could hold everything, tested as a hypothetical with all pins removed
(`:1437-1441`), and the tunnel connector runs `global` with no role constraint so
ingress survives the last worker leaving (`stacks/ingress.yml:69-74`). CA scales
node groups to zero; it does not have a control-plane node that takes the work
back.

**One packer, three questions.** Admission, sizing and removal all ask the same
function. In Kubernetes those are the scheduler, CA's simulator and CA's
scale-down simulator — three models of the same thing, which is why CA ships a
scheduler simulator to stay in sync with the real one.

---

## 5. VPA, briefly

Kubernetes VPA recommends requests from historical usage percentiles and, in
`updateMode: Auto`, evicts pods to apply them.

Here the equivalent is already in the autoscaler and runs last in the loop
(`autoscaler.py:565-618`, `:969-985`): `USAGE_CPU_Q = 0.90`,
`CPU_RESERVE_HEADROOM = 2.0`, `MEM_RESERVE_HEADROOM = 1.5`,
`RESIZE_MIN_CHANGE = 0.25`, `RESIZE_MIN_HISTORY = "2h"`,
`RESIZE_COOLDOWN_SECONDS = 3600`. It updates the service rather than evicting
tasks, and it does not fight the horizontal scaler, which VPA and HPA famously do
on the same metric.

This one is close to parity and is not a candidate below.

---

## 6. Candidates, ranked

Each is a real change with a real cost. Nothing is implemented until you choose.

### 1. Proportional scale-down — *highest value, lowest risk*

Replace the flat `−1` with a bounded percentage step, mirroring HPA's
`scaleDown.policies`. One function (`desired_replicas`, `workloads.py:512`), one
new policy field, tests in `signals/tests/`.

* **Buys:** `20 → 2` in a few loops instead of eighteen. Continuous saving.
* **Risk:** low. Down is already gated on every signal being quiet for
  `sustain_down` (900s), so a too-large step is corrected on the next loop by the
  same evidence that caused it.
* **Watch:** interacts with `admit()`'s monotonicity (`overseer.py:1360-1366`),
  which is written assuming admission never scales down.

### 2. Ratio-based scale-up

Use `ceil(current × metric / target)` where a target actually exists — CPU and
memory have one; latency has an SLO — and keep `up_factor` as the cap.

* **Buys:** converges in one step instead of several under a load step.
* **Risk:** medium. It makes the size of the jump depend on a single reading,
  which is exactly what `sustain_up`'s `min_over_time` was introduced to stop.
  Would need the ratio computed from the held value, not the instantaneous one.

### 3. Recommendation history

Keep the last N computed counts per service and take max-over-window when
shrinking, as HPA does.

* **Buys:** immunity to a metric that oscillates across the window boundary.
* **Risk:** low, but it is new state in a loop that is otherwise strictly
  level-triggered and re-derived every pass. That property is worth a lot — it is
  why a restart mid-decision is harmless — and this is the first thing that would
  erode it.

### 4. A pending-task trigger for the fleet

If a task has been `pending` for longer than a threshold and the packer thinks
there is room, believe the task, not the packer, and grow.

* **Buys:** covers placement failures the packer's model does not represent.
* **Risk:** medium. `pending` is also what a task looks like while an image
  pulls, so it needs a duration and a reason filter or it will buy a machine for
  a slow registry.

### 5. Multi-metric max

Compute a count per metric and take the largest, rather than deriving one count
from a combined direction.

* **Buys:** correctness when CPU and latency disagree about magnitude.
* **Risk:** low in isolation, but it only means anything after (2) — with a fixed
  step, every metric produces the same count and the max is a no-op.

### Not recommended

* **`--scale-down-utilization-threshold`.** The packer answers a better question
  than a threshold does: "does this node's load fit elsewhere" versus "is this
  node under half full". Adding the threshold would give two answers that can
  disagree.
* **Node groups / expanders.** Real value only with more than one machine family
  or location, and the ladder (`_ladder()` `:1175-1218`) already picks by size
  rather than by name for the one family that exists.
* **Evicting to apply reservations.** Swarm updates the service; there is nothing
  to gain from VPA's eviction model here.
