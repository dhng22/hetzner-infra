"""
The metric expressions every performance signal is read through.

One definition each, imported by the autoscaler and the overseer. A service
is "slow" or "busy" according to these and nothing else, which is what lets two
processes reach the same verdict about the same service.
"""

#: cadvisor writes the Swarm service name into this label. Everything per-replica
#: is grouped by it and divided by that service's OWN limit by the caller — which
#: is what removed the hand-maintained APP_CPU_LIMIT.
CPU_LABEL = "container_label_com_docker_swarm_service_name"

CPU_BY_SERVICE = (f'avg by ({CPU_LABEL}) (rate(container_cpu_usage_seconds_total'
                  f'{{{CPU_LABEL}!=""}}[3m]))')

# Working set rather than RSS or usage: usage counts reclaimable page cache, so a
# container that has merely READ a large file reads as nearly full and would
# scale a service out for having done its job.
MEM_BY_SERVICE = (f'avg by ({CPU_LABEL}) (container_memory_working_set_bytes'
                  f'{{{CPU_LABEL}!=""}})')


def unit_of(name):
    return "milliseconds" if ("millis" in name or name.endswith("_ms")) else "seconds"


def p95_expr(histogram, unit, by="service"):
    """
    p95 in milliseconds, grouped by `by`. `service` is written by vmagent.

    `by` exists for DEPENDENCY timers, which have to be grouped by whatever
    names the thing being called — `host`, `server_address` — so one slow
    third party can be told apart from the rest of a service's outbound calls.
    Grouping those by `service` would average the slow one into the fast ones
    and name nothing.
    """
    scale = 1000 if unit == "seconds" else 1
    return (f"histogram_quantile(0.95, sum by ({by}, le) "
            f"(rate({histogram}[2m]))) * {scale}")


def per_request_expr(dep_base, dep_unit, request_base, by="service"):
    """
    Milliseconds of `dep_base` spent inside the AVERAGE request, grouped by `by`.

    This is the only dependency number that can be added up, and adding them up
    is the whole point. A dependency's p95 cannot: it is the tail of one call
    measured against the tail of a request, the two tails are not the same
    requests, and dividing one by the other produced readings like "this
    service spends 1112% of a request in media.tikdrama.asia" — arithmetically
    defensible, useless to read, and impossible to draw.

    Total seconds spent in the dependency, divided by the number of REQUESTS
    that time was spent on, is time per request. Every part measured this way
    sits inside the same average request, so the parts and the remainder add up
    to the end-to-end mean and a stacked bar is an honest picture of it.

    Two ways it can still mislead, both named on the card rather than hidden:
    calls made CONCURRENTLY are counted once each but overlap in wall-clock, so
    the parts can exceed the request they sit in; and work with no timer at all
    lands in the remainder, which is why the remainder is labelled as
    "unmeasured" rather than as the service's own compute.
    """
    scale = 1000 if dep_unit == "seconds" else 1
    return (f"(sum by ({by}) (rate({dep_base}_sum[2m])) "
            f"/ on (service) group_left () "
            f"sum by (service) (rate({request_base}_count[2m]))) * {scale}")


def mean_expr(base, unit, by="service"):
    """
    Mean latency, in milliseconds, grouped by `by`.

    Used when a service publishes a timer but no buckets. It is NOT a p95 and is
    not pretended to be one: the mean sits below the tail, so a service compared
    against a p95 SLO through this scales up later than one with a real
    histogram. That is still enormously better than no latency signal at all,
    and it is the common case — a Micrometer/Prometheus timer publishes _sum and
    _count by default and publishes buckets only when someone enables them.

    `by` exists for the same reason it does on `p95_expr`: a DEPENDENCY timer
    with no buckets still has to be split per service and per target, or one
    service's outbound calls are averaged into another's.

    THE 2-MINUTE WINDOW IS WHY LOW TRAFFIC LIES. With four requests in it, this
    fraction is one request's duration, held steady for two minutes — long
    enough to satisfy any sustain check. That is not a bug in the expression; it
    is why a latency breach has to be corroborated by local saturation before
    anything acts on it. See signals.classify.saturated.
    """
    scale = 1000 if unit == "seconds" else 1
    return (f"(sum by ({by}) (rate({base}_sum[2m])) "
            f"/ sum by ({by}) (rate({base}_count[2m]))) * {scale}")
