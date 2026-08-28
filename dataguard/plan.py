"""
When a database should change shape, and — far more often — why it must not.

THREE STATES, AND THE MIDDLE ONE IS THE POINT
---------------------------------------------
  1  MASTER      one member, on the manager, beside everything else. Free, and
                 where every database starts.
  2  PAIRED      a second member on a machine of its own, which becomes the
                 primary. The master keeps a copy as a priority-0 secondary, so
                 the expensive move — the initial sync — has already happened
                 before it is ever needed.
  3  DEDICATED   the set lives on its own machines and the master holds nothing.

Down is the same ladder in reverse. Nothing skips a rung: every transition is one
member added or one member removed, because `rs.reconfig` replaces the whole
config document and a two-member change has an intermediate state that cannot
elect.

WHY UPGRADING A NODE IS NOT A RESIZE
------------------------------------
The overseer can power-cycle a worker onto a bigger plan, and for a stateless
replica that is fine. For a database it is minutes of that member being down and
a machine that reboots with the data still on it — if the resize succeeds. So a
database never resizes: it provisions a BIGGER machine under a new lease, syncs a
member onto it, promotes it, and drops the old one. That is the same sequence as
every other transition here, it is interruptible at every step, and the old
machine is still serving until the moment it is not needed.

THE GATES ARE THE FEATURE
-------------------------
Everything above returns an action. `refusals()` returns the reasons not to take
it, and the loop reports them as `dataguard_refused_total{reason}` rather than
logging into silence — when nothing is happening, the only question anybody has
is which gate is holding it.
"""

import time

import engines

STATE_MASTER = 1
STATE_PAIRED = 2
STATE_DEDICATED = 3

#: What the loop is being asked to do. A namedtuple would do, but these carry
#: different fields and a plain object keeps the call sites readable.
class Action:
    def __init__(self, verb, **kw):
        self.verb = verb
        self.__dict__.update(kw)

    def __repr__(self):
        rest = {k: v for k, v in self.__dict__.items() if k != "verb"}
        return f"<{self.verb} {rest}>"


HOLD = Action("hold")


def current_state(component, topology):
    """
    Which rung this component is on, read from where its members ACTUALLY are.

    Never stored. A stored state is a claim about the cluster that stops being
    true the moment somebody deletes a service, and the whole machine then acts
    on a description of a database that no longer exists.
    """
    if not topology.ok:
        return None
    hosts = {m.host.split(":")[0] for m in topology.members}
    on_master = component.master_member in hosts
    others = len(hosts - {component.master_member})
    if others == 0:
        return STATE_MASTER
    if on_master and others >= 1:
        return STATE_PAIRED
    return STATE_DEDICATED


def refusals(component, topology, now=None, backup_age=None, syncing_elsewhere=False,
             disk_free_bytes=None, data_bytes=None):
    """
    Every reason this component must not change shape right now.

    Empty means go. The order is roughly cheapest-to-check first, but all of
    them are evaluated: reporting one reason when three apply sends somebody to
    fix the wrong thing.
    """
    now = now or time.time()
    out = []

    if not component.enabled:
        out.append("disabled")
    if component.dry_run:
        out.append("dry_run")
    if not topology.ok:
        # An unreachable database is the one state where doing nothing is
        # obviously right. Every action below starts by reconfiguring a set we
        # cannot currently see.
        out.append("unreachable")
        return out
    if topology.primary is None:
        out.append("no_primary")
    if not topology.has_majority:
        out.append("no_majority")
    if any(m.state == engines.STARTUP for m in topology.members):
        out.append("member_syncing")
    if syncing_elsewhere:
        # ONE INITIAL SYNC AT A TIME, CLUSTER-WIDE. A sync reads the entire
        # dataset off a live member; two at once turns a busy database into an
        # unavailable one, and neither of them is the emergency.
        out.append("another_sync_in_flight")
    if now - component.last_change_at < component.cooldown_seconds:
        out.append("cooldown")
    if component.backup_target and backup_age is None:
        # A component that asked for backups and has none is one whose backup
        # path is not working. Changing its shape then is betting the database
        # on the change going well.
        out.append("no_backup")
    elif component.backup_target and backup_age > component.backup_max_age_seconds:
        out.append("backup_stale")
    if (disk_free_bytes is not None and data_bytes is not None
            and disk_free_bytes < data_bytes * component.disk_headroom):
        out.append("insufficient_disk")
    return out


def _voting_after(topology, delta):
    return len(topology.voting) + delta


def free_index(component, topology, on_master=False):
    """
    The lowest member slot that is not currently in the set, or None.

    NOT `len(members) + 1`. Every member is a Swarm service rendered up front —
    `<name>_mongo-1` through `-<pool>` — so an index is a slot, not a count, and
    the two stop agreeing the moment anything is removed. A set of {1, 3} has
    two members and `len + 1` is 3, which is the member that is already running:
    the "new" member would be an add of a host already in the config, and the
    reconfig either no-ops or duplicates depending on which engine sees it first.

    Slot 1 is the copy on the master and is never handed out for growth; it is
    asked for by name when a component comes back down to the master.
    """
    used = {m.host for m in topology.members}
    for index in range(1 if on_master else 2, component.pool + 1):
        if component.member_host(index) not in used:
            return index
    return None


def _size(sizes, host):
    """The (cores, memory) of the machine a member sits on, or None if unknown."""
    return (sizes or {}).get(host)


def next_action(component, state, topology, pressure, engine_votes=True,
                sizes=None, at_max_plan=False):
    """
    The single next step, or HOLD.

    `pressure` is what the world is saying about this component:
        capacity   the machine it is on is running out of cpu/memory/disk
        latency    it is slow, sustained, and the overseer attributed it here
        read       the slowness is reads (only meaningful with secondary reads on)
        write      the slowness is writes
        quiet      none of the above, for long enough to consider shrinking

    `sizes` maps a member's host to the (cores, memory) of the machine under it,
    which is how an upgrade in flight is recognised without remembering that one
    was started. `at_max_plan` is the overseer reporting that this component is
    already on the biggest machine the ceiling allows — the difference between
    "grow" and "there is nothing left to grow onto", which nothing could see
    from inside a replica set.

    Exactly one action comes back. The loop applies it and the next loop reads
    the world again — there is no multi-step plan to get out of sync with
    reality, which is what makes a restart mid-transition harmless.
    """
    growing = pressure.get("capacity") or pressure.get("latency")
    shrinking = pressure.get("quiet")

    # --- promotion of anything that has caught up comes first ---------------
    # It is the cheapest action, it is always safe, and leaving a synced member
    # non-voting is leaving the set less resilient than it looks.
    for member in topology.members:
        if (member.state == engines.SECONDARY and member.votes == 0
                and member.lag_seconds is not None
                and member.lag_seconds <= component.lag_budget_seconds):
            return Action("enfranchise", host=member.host)

    # A lagging member must not serve reads. Reversible, and applied in both
    # directions so a member that recovers gets its traffic back.
    for member in topology.members:
        if member.state != engines.SECONDARY or member.lag_seconds is None:
            continue
        too_slow = member.lag_seconds > component.lag_budget_seconds
        if too_slow and not member.hidden and component.secondary_reads:
            return Action("hide", host=member.host, lag=member.lag_seconds)
        if not too_slow and member.hidden and component.secondary_reads:
            return Action("unhide", host=member.host)

    # --- finishing an upgrade that is already half done ---------------------
    # A database is never power-cycled onto a bigger plan: a member is started
    # on a bigger machine, syncs, takes over, and the small one goes. Those last
    # two steps are driven from what the cluster LOOKS like — a caught-up member
    # on a machine bigger than the primary's — rather than from a note saying an
    # upgrade is in progress, so a dataguard that restarts mid-upgrade finishes
    # it instead of leaving the set permanently one member too big.
    if state == STATE_DEDICATED and topology.primary is not None:
        on_primary = _size(sizes, topology.primary.host)
        if on_primary:
            ready = [m for m in topology.members
                     if m.state == engines.SECONDARY and m.votes > 0
                     and not m.hidden and m.lag_seconds is not None
                     and m.lag_seconds <= component.lag_budget_seconds
                     and (_size(sizes, m.host) or (0, 0)) > on_primary]
            if ready:
                target = max(ready, key=lambda m: _size(sizes, m.host))
                return Action("promote", host=target.host,
                              reason="it is caught up and on a bigger machine "
                                     "than the primary")
            # Only once the primary IS the big one is the small member spare.
            # Checked after the promotion above, never beside it: removing it
            # first is removing capacity from a set that has not yet moved.
            spare = [m for m in topology.members
                     if m.host != topology.primary.host and m.votes > 0
                     and (_size(sizes, m.host) or on_primary) < on_primary]
            if spare and len(topology.healthy_voting) - 1 >= 3:
                return Action("remove",
                              host=min(spare, key=lambda m: _size(sizes, m.host)).host,
                              reason="a bigger machine has taken over from it")

    if growing:
        if state == STATE_MASTER:
            # 1 -> 2. One machine, one member, and the master keeps its copy.
            return Action("provision", index=2, reason="growing off the master",
                          node_type=component.node_type)
        if state == STATE_PAIRED:
            if len(topology.members) < 3:
                return Action("provision", index=3, reason="a set of two cannot elect",
                              node_type=component.node_type)
            # 2 -> 3 needs the master out, and the master only comes out once
            # there are enough members WITHOUT IT to keep a majority — which is
            # what the count has to exclude it to mean. Counting the master
            # among the three satisfied the condition with {master, a, b} and
            # left {a, b}: a two-member set, whose majority is two, so either
            # machine going down takes the writes with it. The set then noticed
            # it was too small and bought a third, so the dip lasted one
            # transition and looked like progress in the log.
            without_master = [m for m in topology.healthy_voting
                              if m.host != component.master_host]
            if len(without_master) >= 3:
                return Action("remove", host=component.master_host,
                              reason="the set no longer needs the master")
            # AFTER the removal above, never before it. Shedding the master is
            # not growth — it makes the set smaller — so a component sitting at
            # its member ceiling was told it could not grow and therefore never
            # got to the line that would have freed the master. It stayed on the
            # master forever, at the ceiling, with `at_ceiling` in the metrics
            # looking like the ceiling was doing its job.
            if _voting_after(topology, 1) > component.max_members:
                return Action("at_ceiling", limit=component.max_members)
            index = free_index(component, topology)
            if index is None:
                return Action("at_ceiling", limit=component.pool)
            return Action("provision", index=index,
                          reason="building out to a set that can lose one",
                          node_type=component.node_type)
        # state == DEDICATED. WHICH way to grow depends on WHY.
        if pressure.get("write") or pressure.get("capacity"):
            # More replicas cannot absorb writes — every write goes to the
            # primary whatever the topology is — so this is a bigger machine.
            # Delivered as a new member on a bigger node, then a promotion: a
            # database is never power-cycled onto another plan.
            #
            # BOTH ceilings apply, and they are different things. `max_members`
            # is how many replicas this database may have; `at_max_plan` is the
            # overseer saying it has already bought the biggest machine its
            # ceiling allows, and is the one that used to be missing — without
            # it this branch asks for "bigger" every cooldown forever, is handed
            # another machine the same size, and bills for it.
            if at_max_plan:
                return Action("at_ceiling", limit=component.max_members,
                              reason="this database is already on the biggest "
                                     "machine its ceiling allows. Raise "
                                     "DB_MAX_CORES / DB_MAX_MEMORY_GB, or reduce "
                                     "what it is being asked to write.")
            if _voting_after(topology, 1) > component.max_members:
                return Action("at_ceiling", limit=component.max_members)
            index = free_index(component, topology)
            if index is None:
                return Action("at_ceiling", limit=component.pool)
            return Action("provision", index=index,
                          reason="writes need a bigger primary",
                          node_type=component.bigger_node_type, promote=True)
        if pressure.get("read"):
            if not component.secondary_reads:
                # Refusing to pretend. Adding replicas cannot help reads that
                # all go to the primary, and doing it anyway would look like an
                # action while changing nothing a user could feel.
                return Action("cannot_help",
                              reason="read latency, but this component sends every "
                                     "read to the primary. Turn on secondary reads "
                                     "— and read the causal-consistency note first "
                                     "— or give the primary a bigger machine.")
            if len(topology.members) >= component.max_members:
                return Action("at_ceiling", limit=component.max_members)
            index = free_index(component, topology)
            if index is None:
                return Action("at_ceiling", limit=component.pool)
            return Action("provision", index=index,
                          reason="reads need more replicas",
                          node_type=component.node_type)
        return HOLD

    if shrinking:
        # ONE RULE, TWO STATES: while there is more than the minimum, drop one
        # member; at the minimum, move down a rung. Written as a size check
        # first and a state check second because the state is DERIVED from where
        # the members are, and coming down changes it underneath the decision —
        # the master rejoining turns DEDICATED into PAIRED on the very next
        # loop, which is how the two used to be written as separate ladders and
        # how the second one ended in a dead end.
        #
        # That dead end: DEDICATED shrank to three, put a copy back on the
        # master, and became PAIRED with four voting members — and the only
        # PAIRED branch that existed required two. Nothing matched, so a quiet
        # database held four members and three machines it was not using,
        # indefinitely, with the log saying nothing at all.
        floor = 3 if state == STATE_DEDICATED else 2
        if len(topology.voting) > floor:
            victim = _shrink_candidate(topology, component)
            if victim:
                return Action("remove", host=victim.host, reason="quiet")
            return HOLD
        if state == STATE_DEDICATED:
            # 3 -> 2 means putting a member back on the master. It is the only
            # transition that ADDS work to the box running the control plane, so
            # it is deliberate and it is last.
            return Action("provision", index=1, reason="returning to the master",
                          node_type=None, on_master=True)
        if state == STATE_PAIRED:
            other = next((m for m in topology.members
                          if m.host != component.master_host), None)
            if other and topology.primary and topology.primary.host != component.master_host:
                return Action("stepdown", host=topology.primary.host,
                              reason="handing the primary back to the master")
            if other:
                return Action("remove", host=other.host, reason="quiet")
        return HOLD

    return HOLD


def _shrink_candidate(topology, component):
    """
    Which member to drop. Never the primary, never the last healthy secondary.

    Picking the primary would work — it would step down first — but it turns a
    cost saving into an election, and an election is a few seconds of refused
    writes for no reason at all.
    """
    candidates = [m for m in topology.members
                  if m.state == engines.SECONDARY and m.host != component.master_host]
    if not candidates:
        return None
    # WHAT SURVIVES is counted over the whole set, while what may GO excludes
    # the master. Those are two different questions and conflating them stalled
    # the way down: the copy on the master is a healthy secondary like any
    # other, so a set of {master, secondary, primary} was read as having one
    # spare member when it has two — and the last machine was never given back.
    if len([m for m in topology.members if m.healthy]) - 1 < 2:
        return None
    # The laggiest first: it is the one contributing least and the one whose
    # removal is least likely to be noticed.
    return max(candidates, key=lambda m: (m.lag_seconds or 0.0))


def would_break_majority(topology, host, voting=True):
    """
    Would removing this member leave a set that cannot elect?

    Asked BEFORE every removal, including the ones the shrink path chose, because
    the topology may have changed between choosing and acting — a member can go
    down in the seconds between two reads, and the arithmetic that was safe then
    is not safe now.
    """
    if not voting:
        return False
    member = topology.by_host(host)
    if member is None:
        return False
    remaining = [m for m in topology.voting if m.host != host]
    healthy = [m for m in remaining if m.healthy]
    if not remaining:
        return True
    return not len(healthy) > len(remaining) // 2
