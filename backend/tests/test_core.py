"""Core logic tests. Stdlib unittest, so they run anywhere: python -m unittest discover tests"""
import unittest
from datetime import datetime, timedelta

from app.core import geo, scoring, lifecycle, routing
from app.core.lifecycle import Event, State, PortalStatus, PassObservation


class GeoTests(unittest.TestCase):
    def test_known_geohash(self):
        # Reference value from the original geohash.org example
        self.assertEqual(geo.encode(57.64911, 10.40744, 11), "u4pruydqqvj")

    def test_roundtrip(self):
        gh = geo.encode(25.6029, 85.1374, 8)
        lat, lon = geo.decode(gh)
        self.assertLess(geo.haversine_m(lat, lon, 25.6029, 85.1374), 30)

    def test_neighbours(self):
        gh = geo.encode(28.5459, 77.2732, 7)  # IIIT Delhi
        n = geo.neighbours(gh)
        self.assertEqual(len(n), 9)
        self.assertIn(gh, n)
        # A point 120 m east must land in the cell or a neighbour.
        east = geo.encode(28.5459, 77.2732 + 0.00123, 7)
        self.assertIn(east, n)

    def test_haversine(self):
        d = geo.haversine_m(28.6139, 77.2090, 28.6139, 77.2100)
        self.assertAlmostEqual(d, 97.7, delta=1.5)


class ScoringTests(unittest.TestCase):
    def test_crater_vs_crack(self):
        crater = scoring.DetectionSignal(area_frac=0.09, model_conf=0.9, jolt_g=0.9, speed_kmh=30, road_class="arterial")
        crack = scoring.DetectionSignal(area_frac=0.005, model_conf=0.6, jolt_g=0.12, speed_kmh=30, road_class="local")
        self.assertEqual(scoring.classify(scoring.detection_score(crater)), scoring.Severity.HIGH)
        self.assertEqual(scoring.classify(scoring.detection_score(crack)), scoring.Severity.LOW)

    def test_no_imu_uses_visual_only(self):
        s = scoring.detection_score(scoring.DetectionSignal(area_frac=0.04, model_conf=0.8, jolt_g=None))
        self.assertGreater(s, 0.3)

    def test_speed_normalises_jolt(self):
        slow = scoring.detection_score(scoring.DetectionSignal(0.02, 0.8, 0.6, speed_kmh=10))
        fast = scoring.detection_score(scoring.DetectionSignal(0.02, 0.8, 0.6, speed_kmh=60))
        self.assertGreater(slow, fast)

    def test_outlier_does_not_escalate(self):
        scores = [0.2, 0.22, 0.25, 0.21, 0.95]
        self.assertEqual(scoring.classify(scoring.hotspot_score(scores)), scoring.Severity.LOW)

    def test_confidence_counts_distinct_riders(self):
        one = scoring.crowd_confidence({"r1": 0.7})
        three = scoring.crowd_confidence({"r1": 0.7, "r2": 0.7, "r3": 0.7})
        self.assertAlmostEqual(one, 0.7)
        self.assertGreater(three, 0.97)
        policy = scoring.FilingPolicy()
        self.assertFalse(scoring.eligible_to_file(1, 0.99, policy))
        self.assertTrue(scoring.eligible_to_file(2, 0.91, policy))

    def test_priority_monotonic(self):
        p = scoring.priority
        H, M = scoring.Severity.HIGH, scoring.Severity.MEDIUM
        self.assertGreater(p(H, "local", 5, 1), p(M, "local", 5, 1))
        self.assertGreater(p(H, "local", 50, 1), p(H, "local", 5, 1))
        self.assertGreater(p(H, "local", 5, 20), p(H, "local", 5, 1))
        self.assertGreater(p(H, "national", 5, 1), p(H, "local", 5, 1))


class LifecycleTests(unittest.TestCase):
    def test_happy_path(self):
        s = State.CANDIDATE
        for ev in [Event.CONFIRM, Event.APPROVE, Event.FILED_OK, Event.PORTAL_CLOSED, Event.ROAD_CLEAR]:
            s = lifecycle.next_state(s, ev)
        self.assertEqual(s, State.VERIFIED_FIXED)

    def test_fake_closure_escalates(self):
        s = lifecycle.next_state(State.FILED, Event.PORTAL_CLOSED)
        s = lifecycle.next_state(s, Event.ROAD_STILL_BAD)
        s = lifecycle.next_state(s, Event.ESCALATE)
        self.assertEqual(s, State.ESCALATED)

    def test_invalid(self):
        with self.assertRaises(lifecycle.InvalidTransition):
            lifecycle.next_state(State.CANDIDATE, Event.FILED_OK)

    def test_portal_parsing(self):
        P = lifecycle.parse_portal_status
        self.assertEqual(P("Complaint No: GR123\nStatus: Disposed"), PortalStatus.CLOSED)
        self.assertEqual(P("Status - Under Process"), PortalStatus.IN_PROGRESS)
        self.assertEqual(P("शिकायत की स्थिति: लंबित"), PortalStatus.OPEN)
        self.assertEqual(P("Status: Rejected (not feasible)"), PortalStatus.REJECTED)
        self.assertIsNone(P("Welcome to the portal of New Delhi"))

    def test_portal_parsing_scoped_to_id(self):
        page = ("| GR100 | Pothole MG Road | Status: Closed |\n"
                + "x" * 1000 +
                "\n| GR200 | Pothole Ring Road | Status: Pending |")
        self.assertEqual(lifecycle.parse_portal_status(page, "GR200"), PortalStatus.OPEN)
        self.assertIsNone(lifecycle.parse_portal_status(page, "GR999"))

    def test_verification(self):
        D = lifecycle.decide_verification
        self.assertIsNone(D([PassObservation("a", False)]))
        self.assertEqual(D([PassObservation("a", False), PassObservation("b", False)]), Event.ROAD_CLEAR)
        self.assertEqual(D([PassObservation("a", True, 0.9, 0.7)]), Event.ROAD_STILL_BAD)
        self.assertEqual(D([PassObservation("a", True, 0.6), PassObservation("b", True, 0.6)]), Event.ROAD_STILL_BAD)
        # one clear + one weak sighting: keep waiting
        self.assertIsNone(D([PassObservation("a", False), PassObservation("b", True, 0.5)]))

    def test_sla(self):
        t0 = datetime(2026, 11, 1)
        self.assertFalse(lifecycle.sla_breached(t0, t0 + timedelta(days=6), 7))
        self.assertTrue(lifecycle.sla_breached(t0, t0 + timedelta(days=8), 7))
        self.assertFalse(lifecycle.sla_breached(t0, t0 + timedelta(days=8), 7, escalation_level=1))


class RoutingTests(unittest.TestCase):
    def test_rules(self):
        r = routing.resolve_authority
        self.assertEqual(r("NH 44", None, "delhi", "dl").authority_key, "nhai")
        self.assertEqual(r("SH-78", None, None, "br").authority_key, "pwd_br")
        local = r(None, "Khan Market Lane", "delhi", "dl")
        self.assertEqual(local.authority_key, "city_delhi")
        self.assertIsNone(local.search_query)
        arterial = r(None, "Outer Ring Road", "delhi", "dl")
        self.assertIsNotNone(arterial.search_query)
        self.assertIsNone(r(None, None, None, None).authority_key)

    def test_road_class(self):
        self.assertEqual(routing.road_class_from_ref("NH-19", None), "national")
        self.assertEqual(routing.road_class_from_ref(None, "Bailey Road"), "local")


if __name__ == "__main__":
    unittest.main()
