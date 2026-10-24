import unittest

from app.services.geocode import parse_nominatim


class GeocodeParseTests(unittest.TestCase):
    def test_city_road(self):
        data = {"display_name": "x", "address": {
            "road": "Okhla Main Road", "suburb": "Okhla Phase III", "city": "New Delhi",
            "state": "Delhi", "ISO3166-2-lvl4": "IN-DL", "country_code": "in"}, "namedetails": {}}
        p = parse_nominatim(data)
        self.assertEqual(p["road_name"], "Okhla Main Road")
        self.assertEqual(p["city_key"], "delhi")
        self.assertEqual(p["state_key"], "dl")
        self.assertEqual(p["address"], "Okhla Main Road, Okhla Phase III, New Delhi")
        self.assertIsNone(p["road_ref"])

    def test_highway_ref(self):
        data = {"address": {"road": "Grand Trunk Road", "town": "Bakhtiyarpur", "ISO3166-2-lvl4": "IN-BR"},
                "namedetails": {"ref": "NH19"}}
        p = parse_nominatim(data)
        self.assertEqual(p["road_ref"], "NH19")
        self.assertEqual(p["state_key"], "br")
        self.assertEqual(p["city_key"], "bakhtiyarpur")

    def test_error(self):
        self.assertEqual(parse_nominatim({"error": "Unable to geocode"}), {})


if __name__ == "__main__":
    unittest.main()
