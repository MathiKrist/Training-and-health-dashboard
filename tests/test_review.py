"""Reviewing a run against the workout it was run from. Run from the project folder: python -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import db  # noqa: E402
import metrics  # noqa: E402
import review  # noqa: E402
import sync  # noqa: E402
import workouts  # noqa: E402

TODAY = date.today().isoformat()
T = workouts.Targets({"lt_speed": 1000 / 330, "zones": [99, 118, 138, 158, 177, 198]}, None)  # threshold 5:30 /km
STEPS = workouts._run_steps("threshold", 60, T)  # 15 min warm-up, 4 × (8 min threshold, 2 min jog), 10 min cool-down
WORK = STEPS[1]["steps"][0]["target"]
SESSION = {"date": TODAY, "sport": "run", "title": "Threshold intervals", "kind": "threshold", "steps": STEPS,
           "total_s": 3600, "km": 10.2, "why": ["Readiness 74"], "plan": False}


def run(**kw):
    return {"id": 5, "date": TODAY, "sport": "run", "workout_id": 77, "duration_s": 3600, "km": 10.0,
            "speed": 2.8, "avg_hr": 155, **kw}


def lap(pace, s=480, hr=165):
    return {"duration_s": s, "km": s / pace, "speed": 1000 / pace, "avg_hr": hr}


def laps(work_paces):
    """A lap per step, as the watch records a sent workout: the reps at the given paces."""
    out = [lap(390, 900, 140)]
    for p in work_paces:
        out += [lap(p), lap(420, 120, 145)]
    return out + [lap(400, 600, 140)]


class Planned(unittest.TestCase):
    def test_found_by_garmin_workout_id_in_the_archive(self):
        self.assertEqual(review.planned(run(), {"77": SESSION}, [], [], None, [])["title"], "Threshold intervals")

    def test_not_one_the_dashboard_sent(self):
        self.assertIsNone(review.planned(run(workout_id=12), {"77": SESSION}, [], [], None, []))
        self.assertIsNone(review.planned(run(workout_id=None), {"77": SESSION}, [], [], None, []))

    def test_not_todays_session_once_it_has_changed(self):
        # Sent as anaerobic repeats; after the run, today's suggestion became a recovery run
        sent = [{"date": TODAY, "sport": "run", "title": "Anaerobic repeats", "id": 77}]
        now = {**SESSION, "title": "Recovery run", "why": []}
        self.assertIsNone(review.planned(run(), {}, sent, [], None, [now]))

    def test_sent_before_the_archive_was_kept(self):
        today = {**SESSION, "label": "Running", "why": ["Readiness 74"]}
        sent = [{"date": TODAY, "sport": "run", "title": "Threshold intervals", "id": 77}]
        self.assertEqual(review.planned(run(), {}, sent, [], None, [today])["steps"], STEPS)
        # A plan session: the one the run counted for
        plan = {"config": {"name": "Odense Half"}, "weeks": [{"start": TODAY, "phase": "build", "deload": False,
                                                              "sessions": [{**SESSION, "actual": {"id": 5}}]}]}
        found = review.planned(run(), {}, [], [{"date": TODAY, "key": "k", "id": 77}], plan, [])
        self.assertEqual(found["why"][:2], ["Odense Half", "Plan week 1 of 1"])


class Compare(unittest.TestCase):
    def test_each_step_against_its_lap(self):
        mid = (WORK["fast"] + WORK["slow"]) / 2
        r = review.build(run(), SESSION, laps([mid, mid, WORK["slow"] + 6, WORK["fast"] - 4]), None)
        self.assertTrue(r["paired"])
        reps = [x for x in r["rows"] if x["intensity"] == "high"]
        self.assertEqual([(x["off"], x["side"]) for x in reps], [(0, None), (0, None), (6, "slow"), (4, "fast")])
        self.assertEqual(r["on_target"], [2, 4])
        self.assertTrue(r["reps"])
        # The warm-up has no target, so nothing to be off
        self.assertIsNone(r["rows"][0]["off"])

    def test_heart_rate_and_power_targets(self):
        self.assertEqual(review._off({"avg_hr": 150}, T.hr(2)), (13, "high"))  # zone 2 is 118-137
        self.assertEqual(review._off({"power": 180}, {"type": "power", "lo": 190, "hi": 210}), (10, "low"))
        self.assertEqual(review._off({"power": 200}, {"type": "power", "lo": 190, "hi": 210}), (0, None))

    def test_whole_run_when_the_laps_dont_pair(self):
        r = review.build(run(), SESSION, [lap(330, 600)] * 3, None)
        self.assertFalse(r["paired"])
        self.assertEqual(len(r["rows"]), 1)
        self.assertIsNone(r["on_target"])

    def test_the_coach_gets_the_numbers(self):
        mid = (WORK["fast"] + WORK["slow"]) / 2
        text = review.prompt(review.build(run(), SESSION, laps([mid] * 4), None))
        self.assertIn("Why this session: Readiness 74", text)
        self.assertIn("4 of 4 reps on target", text)


ANAEROBIC = workouts._run_steps("anaerobic", 50, T)


class FakeGarmin:
    def __init__(self):
        self.asked = []

    def get_workout_by_id(self, i):
        self.asked.append(i)
        w = {"sport": "run", "title": "Anaerobic repeats", "steps": ANAEROBIC, "total_s": 0, "why": ["Readiness 70"]}
        return workouts.to_garmin(w, date.today())


class SentBeforeTheyWereKept(unittest.TestCase):
    """This morning's anaerobic repeats, sent before the dashboard kept sent workouts, and run at noon."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / "test.db")
        conn = db.connect(self.path)
        start = datetime.combine(date.today(), datetime.min.time()) + timedelta(minutes=1)
        db.upsert_activity(conn, sync.parse_activity({
            "activityId": 5, "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"), "activityType": {"typeKey": "running"},
            "distance": 6260, "movingDuration": 2746, "averageHR": 156, "workoutId": 77}))
        db.set_meta(conn, "workouts_sent", json.dumps([{"date": TODAY, "sport": "run", "title": "Anaerobic repeats", "id": 77}]))
        db.set_meta(conn, "training_profile", json.dumps({"lt_speed": 1000 / 330, "zones": [99, 118, 138, 158, 177, 198]}))
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def test_fetched_back_from_garmin_once(self):
        garmin = FakeGarmin()
        self.assertEqual(review.recover(self.conn, lambda: garmin, date.today()), 1)
        kept = review.load_archive(self.conn)["77"]
        self.assertEqual((kept["title"], kept["why"]), ("Anaerobic repeats", ["Readiness 70"]))
        self.assertEqual([s["intensity"] for s in workouts._flat(kept["steps"])][:3], ["easy", "anaerobic", "rest"])
        self.assertEqual(review.recover(self.conn, lambda: garmin, date.today()), 0)
        self.assertEqual(garmin.asked, [77])

    def test_a_note_on_other_numbers_isnt_shown_again(self):
        wrong = review.build(run(), {**SESSION, "title": "Recovery run"}, [], None)
        right = review.build(run(), SESSION, [], None)
        review.save_note(self.conn, wrong, {"text": "This was a recovery run in name only."})
        self.assertIsNone(review.get_note(self.conn, right))
        self.assertEqual(review.get_note(self.conn, wrong)["text"], "This was a recovery run in name only.")

    def test_todays_session_stays_as_sent_and_is_done(self):
        review.recover(self.conn, lambda: FakeGarmin(), date.today())
        d = metrics.build_dashboard(self.path)
        w = next(w for w in d["workouts"] if w["sport"] == "run")
        self.assertEqual((w["title"], w["sent"], w["done"]), ("Anaerobic repeats", True, True))
        self.assertTrue(d["last_by_sport"]["run"]["review"])


if __name__ == "__main__":
    unittest.main()
