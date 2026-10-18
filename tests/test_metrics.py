"""The dashboard's numbers. Run from the project folder: python -m unittest discover tests"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import config  # noqa: E402
import db  # noqa: E402
import metrics  # noqa: E402
import sync  # noqa: E402


class BestEfforts(unittest.TestCase):
    def test_even_pace(self):
        # 4 m/s for 20 minutes (4.8 km): every distance takes distance / 4 seconds, and 5 km is too far
        samples = [(t, t * 4.0) for t in range(0, 1201)]
        self.assertEqual(sync.best_efforts(samples, (400, 1000, 5000)), {400: 100.0, 1000: 250.0})

    def test_fastest_window_is_found(self):
        # 1 km slow (5 min), 1 km fast (3 min), 1 km slow
        samples, t, d = [(0, 0.0)], 0, 0.0
        for speed, metres in ((1000 / 300, 1000), (1000 / 180, 1000), (1000 / 300, 1000)):
            for _ in range(int(metres / speed)):
                t += 1
                d += speed
                samples.append((t, d))
        self.assertAlmostEqual(sync.best_efforts(samples, (1000,))[1000], 180, delta=1)

    def test_too_short(self):
        self.assertEqual(sync.best_efforts([(0, 0), (60, 300)], (400,)), {})


class Predictions(unittest.TestCase):
    def test_known_distance(self):
        self.assertEqual(metrics.predicted_time({"pred_5k": 1200}, 5000), 1200)

    def test_scaled_from_nearest(self):
        # 12 km is nearest to 10 km on a log scale; Riegel: t * (12/10) ** 1.06
        day = {"pred_5k": 1200, "pred_10k": 2500, "pred_half": 5600}
        self.assertEqual(metrics.predicted_time(day, 12000), round(2500 * 1.2 ** 1.06))

    def test_nothing_predicted(self):
        self.assertIsNone(metrics.predicted_time({}, 10000))


class Form(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(metrics._form_state(None), "Warming up")
        self.assertEqual(metrics._form_state(30), "Rested")
        self.assertEqual(metrics._form_state(10), "Fresh")
        self.assertEqual(metrics._form_state(0), "Balanced")
        self.assertEqual(metrics._form_state(-20), "Building")
        self.assertEqual(metrics._form_state(-40), "Overloaded")

    def test_form_pct_needs_some_fitness(self):
        self.assertIsNone(metrics._form_pct(4, 2))
        self.assertEqual(metrics._form_pct(50, 60), -20.0)


class Load(unittest.TestCase):
    def test_trimp_scale_recovers_the_ratio(self):
        rows = [{"duration_s": 3600, "avg_hr": hr, "training_load": 2 * metrics.trimp(3600, hr, 50, config.MAX_HR)}
                for hr in range(130, 145)]
        self.assertAlmostEqual(metrics.trimp_scale(rows, 50), 2.0)

    def test_trimp_scale_needs_enough_activities(self):
        rows = [{"duration_s": 3600, "avg_hr": 140, "training_load": 100}] * (metrics.TRIMP_SCALE_MIN - 1)
        self.assertEqual(metrics.trimp_scale(rows, 50), 1.0)

    def test_garmin_load_wins(self):
        a = {"training_load": 80, "duration_s": 3600, "avg_hr": 150, "type": "cycling"}
        metrics._add_load(a, 50, 3.0)
        self.assertEqual(a["load"], 80)


class Sports(unittest.TestCase):
    def test_sport_of(self):
        self.assertEqual(metrics.sport_of("trail_running"), "run")
        self.assertEqual(metrics.sport_of("virtual_ride"), "bike")
        self.assertEqual(metrics.sport_of("resort_skiing_snowboarding_ws"), "winter")
        self.assertEqual(metrics.sport_of("hiit"), "cardio")
        self.assertEqual(metrics.sport_of("sailing"), "other")
        self.assertEqual(metrics.sport_of(None), "other")

    def test_parse_activity(self):
        row = sync.parse_activity({"activityId": 7, "startTimeLocal": "2026-10-01 07:30:00",
                                   "activityType": {"typeKey": "running"}, "movingDuration": 1500, "duration": 1600})
        self.assertEqual((row["id"], row["date"], row["type"], row["duration_s"]), (7, "2026-10-01", "running", 1500))
        self.assertEqual(sync.parse_activity({})["type"], "other")


class Vo2max(unittest.TestCase):
    def test_weekly_values_carry_forward(self):
        today = date.today()
        start = today - timedelta(days=30)
        days = [{"date": start.isoformat(), "vo2max": 50.0}, {"date": (today - timedelta(days=3)).isoformat(), "vo2max": 51.0}]
        v = metrics._vo2max(days, today)
        self.assertEqual(v["now"], 51.0)
        self.assertEqual(v["weeks"][0]["vo2max"], 50.0)
        self.assertEqual(v["weeks"][-1]["vo2max"], 51.0)
        self.assertTrue(all(w["vo2max"] is not None for w in v["weeks"]))  # weeks without a new value keep the last one

    def test_needs_two_values(self):
        self.assertIsNone(metrics._vo2max([{"date": "2026-01-01", "vo2max": 50}], date.today()))


if __name__ == "__main__":
    unittest.main()


class ActivityPopup(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / "test.db")
        conn = db.connect(self.path)
        row = sync.parse_activity({"activityId": 7, "startTimeLocal": "2026-10-13 07:30:00",
                                   "activityType": {"typeKey": "running"}, "distance": 6000, "movingDuration": 1800,
                                   "fastestSplit_1000": 330.0, "fastestSplit_1609": 552.0, "fastestSplit_5000": 1960.0})
        db.upsert_activity(conn, row)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.dir.cleanup()

    def test_best_efforts_from_garmin_until_worked_out(self):
        best = metrics.build_activity(self.path, 7)["best"]
        self.assertEqual([b["m"] for b in best], [1000, 1609, 5000])

    def test_best_efforts_include_400m_once_worked_out(self):
        conn = db.connect(self.path)
        db.set_efforts(conn, 7, {"400": 128.1, "1000": 327.4, "1609": 550.1, "5000": 1959.7})
        conn.commit()
        conn.close()
        best = metrics.build_activity(self.path, 7)["best"]
        self.assertEqual([(b["label"], b["s"]) for b in best][:2], [("400 m", 128.1), ("1 km", 327.4)])
