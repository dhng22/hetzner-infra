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


if __name__ == "__main__":
    unittest.main()
