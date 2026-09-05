"""
What discovery says out loud when the answer changes underneath a service.
"""
import unittest

from signals import discovery


def rows_for(suffix, metric):
    """A `vm_series_rows` stand-in that only answers for one suffix."""
    def rows(query):
        return [(metric, "api_app", {})] if suffix in query else []
    return rows


class LatencyKindChangeTest(unittest.TestCase):
    def setUp(self):
        self.real = discovery.vm_series_rows
        discovery.reset_caches()

    def tearDown(self):
        discovery.vm_series_rows = self.real
        discovery.reset_caches()

    def refresh(self):
        """Re-discover without forgetting what was discovered last time."""
        discovery._latency.value, discovery._latency.at = {}, 0.0

    def test_a_mean_becoming_a_p95_is_announced_not_swapped_silently(self):
        """
        Shipping histogram buckets is a one-line library change with no visible
        connection to scaling, and the moment it lands this service is measured
        by a different statistic against the SAME autoscale.slo_p95_ms. On the
        live cluster the reported latency went from a 92ms mean to a 497ms p95
        with the application untouched — and the dangerous half is silent: the
        scale-DOWN threshold became unreachable, so the service could never
        shrink again and simply held its replica count forever.
        """
        discovery.vm_series_rows = rows_for(
            "_count", "ktor_http_server_requests_seconds_count")
        _, kind, _ = discovery.discover_latency(["api_app"])["api_app"]
        self.assertEqual(kind, "mean")

        self.refresh()
        discovery.vm_series_rows = rows_for(
            "_bucket", "ktor_http_server_requests_seconds_bucket")
        with self.assertLogs("signals", level="WARNING") as caught:
            _, kind, _ = discovery.discover_latency(["api_app"])["api_app"]
        self.assertEqual(kind, "p95")
        said = "\n".join(caught.output)
        self.assertIn("changed from mean to p95", said)
        self.assertIn("down_p95_ratio", said)

    def test_the_same_statistic_twice_says_nothing(self):
        # Discovery re-runs every fifteen minutes. Warning on each pass would
        # make the one that matters invisible.
        discovery.vm_series_rows = rows_for(
            "_bucket", "ktor_http_server_requests_seconds_bucket")
        discovery.discover_latency(["api_app"])
        self.refresh()
        with self.assertNoLogs("signals", level="WARNING"):
            discovery.discover_latency(["api_app"])


class DependencyStatisticTest(unittest.TestCase):
    """
    A dependency's reading is compared against the SERVICE's latency to decide
    whether it is where the request's time went. That comparison only means
    anything between the same statistic.
    """

    def setUp(self):
        self.real = discovery.vm_series_rows
        discovery.reset_caches()

    def tearDown(self):
        discovery.vm_series_rows = self.real
        discovery.reset_caches()

    def rows(self, *metrics):
        def rows(query):
            suffix = "_bucket" if "_bucket" in query else "_count"
            return [(m, "api_app", {"host": "media.example"})
                    for m in metrics if m.endswith(suffix)]
        return rows

    def test_a_dependency_with_buckets_is_read_as_a_p95(self):
        """
        Read as a mean, a third-party host answering at a 768ms p95 inside a
        725ms p95 request — essentially the whole request — came back as 317ms
        and lost to the threshold. The service was reported as slow for reasons
        nobody knows while the answer sat in a timer it was publishing.
        """
        discovery.vm_series_rows = self.rows(
            "http_client_requests_seconds_count",
            "http_client_requests_seconds_bucket")
        [(cause, expr, _base, target)] = discovery.discover_dependencies(
            ["api_app"])["api_app"]
        self.assertEqual(cause, "upstream")
        self.assertIn("histogram_quantile", expr)
        # Grouped by the thing being CALLED, not by the calling service, or one
        # slow third party is averaged into every fast one and nothing is named.
        self.assertEqual(target, "host")
        self.assertIn("by (host, le)", expr)

    def test_a_dependency_without_buckets_still_falls_back_to_a_mean(self):
        # Most timers publish _sum and _count and nothing else. A mean is worth
        # far more than no dependency signal at all.
        discovery.vm_series_rows = self.rows("http_client_requests_seconds_count")
        [(_cause, expr, _base, _target)] = discovery.discover_dependencies(
            ["api_app"])["api_app"]
        self.assertNotIn("histogram_quantile", expr)


if __name__ == "__main__":
    unittest.main()
