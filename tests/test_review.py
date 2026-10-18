"""Reviewing a run against the workout it was run from. Run from the project folder: python -m unittest discover tests"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import review  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
