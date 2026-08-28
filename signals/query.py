"""
VictoriaMetrics access, shared by every process that reads a performance signal.

Extracted from the autoscaler when the overseer was split out of it. Two
copies of `mean_expr` in two images is the drift this repository keeps being
bitten by — a query fixed in one process and not the other produces two
components that disagree about whether the same service is slow, and nothing
fails while they do.

Errors are counted through a HOOK rather than a metric declared here: the
autoscaler and the overseer each own their own Prometheus registry, and a
counter defined in a shared library would either have to be passed everywhere or
become a third registry nobody scrapes.
"""

import logging
import os
import time

import requests

log = logging.getLogger("signals")

VM_URL = os.environ.get("VM_URL", "http://victoriametrics:8428")
TIMEOUT_SECONDS = 15

#: Called with the stage name when a query fails. Replaced by the importing
#: process with its own counter; a no-op by default so the library is usable
#: from a test or a script without any wiring.
on_error = lambda stage: None          # noqa: E731


def _get(path, params):
    resp = requests.get(f"{VM_URL}{path}", params=params, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def vm_query(expr):
    """Instant query. Returns float or None."""
    try:
        results = _get("/api/v1/query", {"query": expr}).get("data", {}).get("result", [])
        if not results:
            return None
        return float(results[0]["value"][1])
    except Exception as exc:  # noqa: BLE001
        on_error("query")
        log.warning("query failed (%s): %s", expr[:60], exc)
        return None


def vm_query_map(expr, label="service"):
    """
    One query, many series. Returns {label value: float}.

    Every per-service signal is aggregated `by (service)` and read through this
    rather than issued once per service. Ten components x six queries at a 15s
    timeout does not fit in a 60s loop, and AutoscalerStalled fires at 300s.
    """
    out = {}
    try:
        for row in _get("/api/v1/query", {"query": expr}).get("data", {}).get("result", []):
            key = row.get("metric", {}).get(label)
            if key:
                out[key] = float(row["value"][1])
    except Exception as exc:  # noqa: BLE001
        on_error("query")
        log.warning("grouped query failed (%s): %s", expr[:60], exc)
    return out


def vm_series_rows(selector):
    """
    [(metric name, service, full label set)] for a selector.

    /series rather than an instant query: it returns label sets without values,
    so asking "which metrics does this cluster publish" does not also drag every
    sample back through the loop.
    """
    out = []
    try:
        data = _get("/api/v1/series",
                    {"match[]": selector, "start": int(time.time()) - 3600})
        for row in data.get("data", []):
            name, svc = row.get("__name__"), row.get("service")
            if name and svc:
                out.append((name, svc, row))
    except Exception as exc:  # noqa: BLE001
        on_error("query")
        log.warning("series lookup failed (%s): %s", selector[:60], exc)
    return out


def sustained(expr, window, aggregate):
    """
    Was `expr` continuously above (min_over_time) or below (max_over_time) for
    the whole window? A subquery, so no local state is needed.
    """
    step = max(15, window // 12)
    return f"{aggregate}(({expr})[{window}s:{step}s])"
