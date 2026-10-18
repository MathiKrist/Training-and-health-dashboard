"""Today's workout: which session, its targets, and Garmin's format. Run from the project folder: python -m unittest discover tests"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import workouts  # noqa: E402

TODAY = date(2026, 10, 11)
PROFILE = {"lt_speed": 1000 / 330, "zones": [99, 118, 138, 158, 177, 198]}  # threshold 5:30 /km


def run(days_ago, label="AEROBIC_BASE", minutes=50):
    return {"date": (TODAY - timedelta(days=days_ago)).isoformat(), "sport": "run", "te_label": label,
            "focus": None, "duration_s": minutes * 60}


def recommend(acts=(), readiness=80, status="PRODUCTIVE", focus="BALANCED", race=None, sent=(), **body):
    return workouts.recommend(list(acts), TODAY, readiness=readiness, status=status, focus=focus, focus_title="Focus",
                              race=race, profile=PROFILE, pred_5k=1500, sent=list(sent), **body)


class Session(unittest.TestCase):
    def test_low_readiness_is_short_and_easy(self):
        w = recommend([run(3, minutes=60)], readiness=45)[0]
        self.assertEqual(w["kind"], "easy")
        self.assertEqual(w["total_s"], 35 * 60)  # 0.6 of the usual hour

    def test_short_easy_is_capped(self):
        self.assertEqual(recommend([run(3, minutes=90)], recovery_h=15)[0]["total_s"], 40 * 60)

    def test_long_recovery_low_readiness_and_poor_sleep_is_a_recovery_run(self):
        # Readiness 42, 25 h of recovery left and a poor night: half an hour of recovery, as Garmin suggests
        w = recommend([run(3, minutes=60)], readiness=42, recovery_h=25, sleep_score=48)[0]
        self.assertEqual((w["kind"], w["total_s"]), ("recovery", 30 * 60))
        self.assertEqual(w["why"], ["Readiness 42", "Recovery 25 h", "Sleep score 48"])

    def test_two_mild_signs_make_a_recovery_run(self):
        self.assertEqual(recommend(readiness=45, hrv_status="UNBALANCED")[0]["kind"], "recovery")

    def test_very_long_recovery_is_a_rest_day(self):
        for w in recommend([{**run(5), "sport": "bike"}], recovery_h=50):
            self.assertEqual((w["kind"], w["title"], w["steps"], w["total_s"]), ("rest", "Rest day", [], 0))

    def test_no_hard_session_while_recovering(self):
        self.assertEqual(recommend(focus="AEROBIC_HIGH_SHORTAGE", recovery_h=15)[0]["kind"], "easy")
        self.assertEqual(recommend(focus="AEROBIC_HIGH_SHORTAGE", recovery_h=5)[0]["kind"], "threshold")

    def test_rest_before_a_race_when_worn_out(self):
        self.assertEqual(recommend(race={"days_to_go": 2}, readiness=15)[0]["kind"], "rest")

    def test_strain_levels(self):
        self.assertEqual(workouts.strain(80, "PRODUCTIVE"), 0)
        self.assertEqual(workouts.strain(80, "PRODUCTIVE", sleep_score=55), 1)
        self.assertEqual(workouts.strain(30, "PRODUCTIVE"), 2)
        self.assertEqual(workouts.strain(40, "OVERREACHING"), 3)

    def test_very_low_readiness_or_overreaching_is_recovery(self):
        self.assertEqual(recommend(readiness=20)[0]["kind"], "recovery")
        self.assertEqual(recommend(status="OVERREACHING")[0]["kind"], "recovery")

    def test_hard_yesterday_is_easy(self):
        self.assertEqual(recommend([run(1, "VO2MAX")])[0]["kind"], "easy")

    def test_load_focus_picks_the_session(self):
        self.assertEqual(recommend(focus="AEROBIC_HIGH_SHORTAGE")[0]["kind"], "threshold")
        self.assertEqual(recommend(focus="ANAEROBIC_SHORTAGE")[0]["kind"], "anaerobic")
        self.assertEqual(recommend(focus="AEROBIC_LOW_SHORTAGE")[0]["kind"], "long")

    def test_threshold_this_week_makes_it_vo2_max(self):
        self.assertEqual(recommend([run(4, "LACTATE_THRESHOLD")], focus="AEROBIC_HIGH_SHORTAGE")[0]["kind"], "vo2")

    def test_race_in_two_days_is_a_shakeout(self):
        self.assertEqual(recommend(race={"days_to_go": 2})[0]["kind"], "shakeout")

    def test_main_sport_first(self):
        acts = [run(3), {**run(5), "sport": "bike"}, {**run(6), "sport": "bike"}]
        self.assertEqual([w["sport"] for w in recommend(acts)], ["bike", "run"])

    def test_sent_today(self):
        title = recommend()[0]["title"]
        self.assertTrue(recommend(sent=[{"date": TODAY.isoformat(), "sport": "run", "title": title}])[0]["sent"])
        self.assertFalse(recommend(sent=[{"date": "2026-10-10", "sport": "run", "title": title}])[0]["sent"])


class Targets(unittest.TestCase):
    def test_paces_from_threshold(self):
        t = workouts.Targets(PROFILE, None)
        self.assertEqual(t.pace("threshold"), {"type": "pace", "slow": round(330 / 0.97), "fast": round(330 / 1.01)})

    def test_threshold_from_5k_without_garmin_threshold(self):
        self.assertAlmostEqual(workouts.Targets({}, 1500).lt, 5000 / 1500 * workouts.LT_FROM_5K)

    def test_heart_rate_zones(self):
        t = workouts.Targets(PROFILE, None)
        self.assertEqual(t.hr(2), {"type": "hr", "zone": 2, "lo": 118, "hi": 137})
        self.assertEqual(t.hr(5), {"type": "hr", "zone": 5, "lo": 177, "hi": 198})


class GarminFormat(unittest.TestCase):
    def test_steps_and_repeats(self):
        w = recommend(focus="AEROBIC_HIGH_SHORTAGE")[0]
        g = workouts.to_garmin(w, TODAY)
        steps = g["workoutSegments"][0]["workoutSteps"]
        self.assertEqual([s["type"] for s in steps], ["ExecutableStepDTO", "RepeatGroupDTO", "ExecutableStepDTO"])
        # Steps are numbered through the repeat: 1, the group 2, its steps 3 and 4, then 5
        self.assertEqual([steps[0]["stepOrder"], steps[1]["stepOrder"], *[s["stepOrder"] for s in steps[1]["workoutSteps"]],
                          steps[2]["stepOrder"]], [1, 2, 3, 4, 5])
        hard = steps[1]["workoutSteps"][0]
        self.assertEqual(hard["targetType"]["workoutTargetTypeKey"], "pace.zone")
        self.assertLess(hard["targetValueOne"], hard["targetValueTwo"])  # m/s, the slow end first
        self.assertEqual(g["estimatedDurationInSecs"], w["total_s"])

    def test_back_from_garmin_as_sent(self):
        t = workouts.Targets(PROFILE, 1500)
        for kind in ("threshold", "vo2", "anaerobic"):
            w = {"sport": "run", "title": kind, "steps": workouts._run_steps(kind, 50, t), "total_s": 0, "why": ["Readiness 70"]}
            back = workouts.from_garmin(workouts.to_garmin(w, TODAY), t)
            same = lambda steps: [(s["kind"], s["intensity"], s["s"], s["target"]) for s in workouts._flat(steps)]
            self.assertEqual(same(back["steps"]), same(w["steps"]))
            self.assertEqual((back["sport"], back["why"]), ("run", ["Readiness 70"]))

    def test_warm_up_and_cool_down_go_by_feel(self):
        w = recommend(focus="AEROBIC_HIGH_SHORTAGE")[0]
        self.assertEqual([(s["kind"], s["target"]) for s in (w["steps"][0], w["steps"][-1])],
                         [("warmup", None), ("cooldown", None)])
        steps = workouts.to_garmin(w, TODAY)["workoutSegments"][0]["workoutSteps"]
        self.assertEqual([s["targetType"]["workoutTargetTypeKey"] for s in (steps[0], steps[-1])], ["no.target", "no.target"])
        # Whoever passes one (the chat's custom steps), a warm-up never gets a target
        self.assertIsNone(workouts._step("warmup", "Warm-up", 600, "easy", {"type": "hr", "zone": 2})["target"])


class OtherSports(unittest.TestCase):
    def test_swim_goes_by_distance_in_its_pool(self):
        import multisport
        title, steps = multisport.swim("threshold", 2000, 120)
        g = workouts.to_garmin({"sport": "swim", "title": title, "steps": steps, "total_s": 2400, "why": [], "pool": 50}, TODAY)
        self.assertEqual((g["sportType"]["sportTypeKey"], g["poolLength"]), ("swimming", 50.0))
        first, reps = g["workoutSegments"][0]["workoutSteps"][:2]
        self.assertEqual((first["endCondition"]["conditionTypeKey"], first["endConditionValue"]), ("distance", 300.0))
        self.assertEqual([s["stepType"]["stepTypeKey"] for s in reps["workoutSteps"]], ["interval", "rest"])

    def test_power_targets_in_watts(self):
        t = workouts.Targets({**PROFILE, "ftp": 200}, None)
        steps = workouts._bike_steps("threshold", 60, t)
        g = workouts.to_garmin({"sport": "bike", "title": "Threshold", "steps": steps, "total_s": 3600, "why": []}, TODAY)
        work = g["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"][0]
        self.assertEqual((work["targetType"]["workoutTargetTypeKey"], work["targetValueOne"], work["targetValueTwo"]),
                         ("power.zone", 190.0, 210.0))


    def test_rides_warm_up_by_feel_but_race_effort_endurance_keeps_power(self):
        import multisport
        t = workouts.Targets({**PROFILE, "ftp": 200}, None)
        self.assertIsNone(workouts._bike_steps("threshold", 60, t)[0]["target"])
        _, steps = multisport.ride("long", 180, t, tri="half")
        self.assertEqual([s["target"]["type"] for s in (steps[0], steps[-1])], ["power", "power"])


if __name__ == "__main__":
    unittest.main()
