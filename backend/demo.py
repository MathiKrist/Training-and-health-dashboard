"""Generate believable fake training data so the dashboard can be tried without Garmin."""
import json
import math
import random
from datetime import date, datetime, timedelta

import db
import review
import workouts

# weekday -> (kind, km range, avg HR range, pace s/km range)
PLAN = {
    0: None,
    1: ("intervals", (8, 11), (161, 170), (250, 280)),
    2: ("easy", (6, 9), (138, 152), (330, 360)),
    3: ("tempo", (9, 12), (158, 166), (270, 295)),
    4: None,
    5: ("easy", (6, 10), (140, 153), (325, 355)),
    6: ("long", (16, 24), (145, 156), (320, 350)),
}

DEMO_VERSION = "4"  # bumped when the demo data changes, so an existing demo database is rebuilt the same day

# Other sports, so every page has something: weekday -> (Garmin type, name, chance, km range, speed m/s range,
# avg HR range, load per hour, training effect label)
OTHER = {
    0: ("lap_swimming", "Pool swim", 0.6, (1.5, 2.5), (0.75, 0.9), (125, 140), 45, "AEROBIC_BASE"),
    5: ("road_biking", "Saturday ride", 0.7, (30, 60), (7.2, 8.6), (128, 145), 70, "AEROBIC_BASE"),
    6: ("walking", "Evening walk", 0.5, (3, 6), (1.3, 1.55), (95, 110), 10, None),
}
# How much faster than the run's average pace its best stretch over each distance is (shorter is faster)
EFFORT_PACE = {400: 0.86, 1000: 0.9, 1609: 0.92, 5000: 0.96, 10000: 0.98, 21098: 1.0, 42195: 1.0}


def seed(db_path, days=420):
    """Made-up data up to today, built once a day. 420 days, so this year can be set against last year."""
    conn = db.connect(db_path)
    if db.get_meta(conn, "demo_date") == date.today().isoformat() and db.get_meta(conn, "demo_version") == DEMO_VERSION:
        conn.close()
        return
    # A race goal, plan and chat changes set in demo mode outlive the daily rebuild
    kept = {k: db.get_meta(conn, k) for k in ("race_goal", "training_plan", "plan_sent", "workout_changes", "coach_chat")}
    conn.executescript("DELETE FROM activities; DELETE FROM daily; DELETE FROM tracks; DELETE FROM laps; "
                       "DELETE FROM efforts; DELETE FROM meta;")
    for k, v in kept.items():
        if v:
            db.set_meta(conn, k, v)
    rng = random.Random(7)
    today = date.today()
    if not kept["training_plan"]:  # a half marathon plan, 5 weeks in
        start = today - timedelta(days=today.weekday(), weeks=5)
        plan = {"goal": "race", "name": "Odense Half", "metres": 21098, "date": (start + timedelta(weeks=12, days=-1)).isoformat(),
                "target_s": 6300, "start": start.isoformat(), "days": [1, 3, 5, 6], "long_day": 6, "start_km": 32,
                "fitness0": 3.45}
        db.set_meta(conn, "training_plan", json.dumps(plan))
        db.set_meta(conn, "race_goal", json.dumps({k: plan[k] for k in ("name", "date", "metres", "target_s")}))
    plan_start = json.loads(db.get_meta(conn, "training_plan"))["start"]
    fatigue = 0.0
    loads = []
    status = since = None
    for i in range(days, -1, -1):
        d = today - timedelta(days=i)
        build = 0.75 + 0.35 * (1 - i / days)          # volume grows over the block
        if (d.isocalendar()[1] % 4) == 0:              # every 4th week is a recovery week
            build *= 0.65
        plan = PLAN[d.weekday()]
        load = 0.0
        if plan and rng.random() > 0.08 and i > 0:
            kind, km_r, hr_r, pace_r = plan
            km = rng.uniform(*km_r) * build
            pace = rng.uniform(*pace_r)
            hr = rng.uniform(*hr_r)
            dur = km * pace
            load = dur / 60 * (hr - 120) / 30 * (1.6 if kind in ("intervals", "tempo") else 1.0)
            start = datetime.combine(d, datetime.min.time()) + timedelta(hours=rng.choice([6, 7, 17, 18]), minutes=rng.randint(0, 59))
            db.upsert_activity(conn, {
                "id": int(d.strftime("%Y%m%d")) * 10 + 1, "start_local": start.strftime("%Y-%m-%d %H:%M:%S"),
                "date": d.isoformat(), "type": "running",
                "name": {"intervals": "Odense intervals", "tempo": "Tempo run", "easy": "Easy run", "long": "Long run"}[kind],
                "distance_m": km * 1000, "duration_s": dur, "avg_hr": round(hr), "max_hr": round(hr + rng.uniform(8, 18)),
                "avg_speed": 1000 / pace, "elev_gain": rng.uniform(20, 120), "training_load": round(load),
                "aerobic_te": round(rng.uniform(2.5, 4.2), 1), "anaerobic_te": round(rng.uniform(0, 2.5), 1),
                "te_label": {"intervals": rng.choice(["VO2MAX", "ANAEROBIC_CAPACITY"]), "tempo": rng.choice(["TEMPO", "LACTATE_THRESHOLD"]),
                             "easy": "AEROBIC_BASE", "long": "AEROBIC_BASE"}[kind], "raw": None,
            })
            if kind in ("intervals", "tempo") and d.isoformat() >= plan_start:
                db.set_laps(conn, int(d.strftime("%Y%m%d")) * 10 + 1, interval_laps(rng, kind, hr))
            # Best efforts within the run, a bit faster than its average pace over shorter stretches
            db.set_efforts(conn, int(d.strftime("%Y%m%d")) * 10 + 1, {
                m: round(m / 1000 * pace * f * rng.uniform(0.98, 1.02), 1) for m, f in EFFORT_PACE.items() if m <= km * 1000})
        other = OTHER.get(d.weekday())
        if other and i > 0 and rng.random() < other[2]:
            typ, name, _, km_r, speed_r, hr_r, per_hour, te = other
            km, speed, hr = rng.uniform(*km_r) * build, rng.uniform(*speed_r), rng.uniform(*hr_r)
            dur = km * 1000 / speed
            db.upsert_activity(conn, {
                "id": int(d.strftime("%Y%m%d")) * 10 + 3, "start_local": f"{d.isoformat()} {rng.choice(['10', '12', '15'])}:15:00",
                "date": d.isoformat(), "type": typ, "name": name, "distance_m": km * 1000, "duration_s": dur,
                "avg_hr": round(hr), "max_hr": round(hr + rng.uniform(10, 25)), "avg_speed": speed,
                "elev_gain": 0 if typ == "lap_swimming" else rng.uniform(10, 40) * km / 5, "training_load": round(per_hour * dur / 3600),
                "aerobic_te": round(rng.uniform(1.5, 3.2), 1), "anaerobic_te": round(rng.uniform(0, 0.8), 1), "te_label": te, "raw": None,
            })
            load += per_hour * dur / 3600
        if d.weekday() == 3 and i > 0:
            db.upsert_activity(conn, {
                "id": int(d.strftime("%Y%m%d")) * 10 + 2,
                "start_local": f"{d.isoformat()} 19:30:00", "date": d.isoformat(), "type": "strength_training",
                "name": "Strength", "distance_m": 0, "duration_s": 2400, "avg_hr": 112, "max_hr": 140,
                "avg_speed": None, "elev_gain": 0, "training_load": 25, "aerobic_te": 1.2, "anaerobic_te": 0.8, "raw": None,
            })
            load += 25
        fatigue = fatigue * 0.8 + load * 0.2
        loads.append(load)
        recovery_week = (d.isocalendar()[1] % 4) == 0
        new_status = "RECOVERY" if recovery_week else "PRODUCTIVE" if build > 0.95 else "MAINTAINING"
        if new_status != status:
            status, since = new_status, d.isoformat()
        active = round(450 + load * 5 + rng.gauss(0, 80))
        hrv = 64 - fatigue * 0.12 + rng.gauss(0, 5)
        vo2 = 50 + (1 - i / days) * 2
        pred_5k = 1250 - (vo2 - 50) * 35 + rng.gauss(0, 4)
        db.upsert_daily(conn, {
            "date": d.isoformat(), "resting_hr": round(48 + fatigue * 0.04 + rng.gauss(0, 1.5)),
            "hrv_last_night": round(hrv), "hrv_weekly_avg": round(64 - fatigue * 0.1), "hrv_low": 54, "hrv_high": 72,
            "hrv_status": "BALANCED" if hrv >= 54 else "UNBALANCED",
            "sleep_s": rng.uniform(6.2, 8.6) * 3600, "sleep_score": rng.randint(62, 92),
            "bb_high": rng.randint(70, 98), "bb_low": rng.randint(10, 35), "stress_avg": rng.randint(20, 40),
            "steps": rng.randint(5000, 15000),
            # Calories: resting (BMR) plus active, which follows the day's training
            "cal_resting": 1750, "cal_active": active, "cal_total": 1750 + active,
            "readiness": max(5, min(100, round(85 - fatigue * 0.5 + rng.gauss(0, 8)))),
            # Recovery time on waking, from the day before's training
            "recovery_h": round(max(0, (loads[-2] if len(loads) > 1 else 0) * 0.2 + rng.gauss(0, 3)), 1),
            "training_status": status, "training_status_since": since,
            "acute_load": round(sum(loads[-7:])), "acute_load_min": 300, "acute_load_max": 560,
            "acwr_status": "LOW" if sum(loads[-7:]) < 300 else "OPTIMAL", "vo2max": round(vo2, 1),
            # Garmin's race predictor, following VO2 max
            "pred_5k": round(pred_5k), "pred_10k": round(pred_5k * 2.08), "pred_half": round(pred_5k * 4.62),
            "pred_marathon": round(pred_5k * 9.8),
            "load_low": 760, "load_low_min": 300, "load_low_max": 820, "load_high": 690, "load_high_min": 640,
            "load_high_max": 1160, "load_anaerobic": 40, "load_anaerobic_min": 170, "load_anaerobic_max": 510,
            "load_focus": "ANAEROBIC_SHORTAGE",
        })
    todays_run(conn, rng, today)
    # A GPS loop for the newest outdoor activity, so the last activity panel has a map when that's the newest
    last = conn.execute("SELECT id, distance_m FROM activities WHERE type IN ('running', 'road_biking', 'walking') "
                        "ORDER BY start_local DESC LIMIT 1").fetchone()
    if last:
        db.set_track(conn, last["id"], demo_track(rng, last["distance_m"] / 1000))
    now = datetime.now().isoformat(timespec="seconds")
    db.set_meta(conn, "last_sync_at", now)
    db.set_meta(conn, "demo_date", today.isoformat())
    db.set_meta(conn, "demo_version", DEMO_VERSION)
    # Lactate threshold (4:50 /km) and heart rate zones, for the workout targets
    db.set_meta(conn, "training_profile", json.dumps({"lt_speed": 3.45, "lt_hr": 172, "zones": [98, 117, 137, 156, 175, 195]}))
    conn.commit()
    conn.close()


def todays_run(conn, rng, today):
    """This morning's threshold intervals, run from a workout sent to the watch from the dashboard (Garmin marks the
    activity with the workout's id), with a lap per step: the Workout page shows it at the top, with its review."""
    t = workouts.Targets({"lt_speed": 3.45, "zones": [98, 117, 137, 156, 175, 195]}, None)
    steps = workouts._run_steps("threshold", 60, t)  # 4 × 8 min, the third a little slow
    laps = []
    for i, s in enumerate(workouts._flat(steps)):
        target = s["target"]
        pace = ((target["fast"] + target["slow"]) / 2 * (1.04 if i == 5 else rng.uniform(0.985, 1.0)) if target
                else 345 if s["kind"] == "warmup" else 350 if s["kind"] == "cooldown" else 420)
        hr = 168 + i if s["intensity"] == "high" else 142
        laps.append({"km": round(s["s"] / pace, 3), "duration_s": s["s"], "speed": 1000 / pace, "avg_hr": hr,
                     "max_hr": hr + 6})
    now = datetime.now()
    start = max(datetime.combine(today, datetime.min.time()), min(now - timedelta(minutes=90), now.replace(hour=6, minute=30)))
    km, dur = sum(l["km"] for l in laps), sum(l["duration_s"] for l in laps)
    run_id = int(today.strftime("%Y%m%d")) * 10 + 1
    db.upsert_activity(conn, {
        "id": run_id, "start_local": start.strftime("%Y-%m-%d %H:%M:%S"), "date": today.isoformat(), "type": "running",
        "name": "Odense threshold", "distance_m": km * 1000, "duration_s": dur, "avg_hr": 158, "max_hr": 178,
        "avg_speed": km * 1000 / dur, "elev_gain": 35, "training_load": 140, "aerobic_te": 3.8, "anaerobic_te": 1.2,
        "te_label": "LACTATE_THRESHOLD", "raw": {"workoutId": 1},
    })
    db.set_laps(conn, run_id, laps)
    db.set_efforts(conn, run_id, {400: 98.0, 1000: 262.0, 1609: 430.0, 5000: 1500.0})
    w = {"sport": "run", "title": "Threshold intervals", "kind": "threshold", "steps": steps,
         "total_s": sum(s["s"] for s in workouts._flat(steps)), "km": workouts.km_of(steps, t), "plan": False}
    review.remember(conn, 1, w, today, ["Garmin load focus: high aerobic shortage", "Readiness 74"], today)


def interval_laps(rng, kind, hr):
    """Laps as a watch records a structured workout: warm-up, work reps with jogs between, cool-down. The reps run
    a little faster than the plan's targets (threshold 4:52-5:00 /km, VO2 max off the 5 km pace)."""
    work_s, n, pace = (480, 4, rng.uniform(282, 292)) if kind == "tempo" else (180, 5, rng.uniform(262, 272))
    lap = lambda s, p, h: {"km": round(s / p, 3), "duration_s": s, "speed": 1000 / p, "avg_hr": round(h)}
    laps = [lap(900, 345, hr - 18)]
    for i in range(n):
        laps.append(lap(work_s, pace * rng.uniform(0.99, 1.01), hr + 2 + i))
        if i < n - 1:
            laps.append(lap(120 if kind == "tempo" else 180, 420, hr - 15))
    return laps + [lap(600, 350, hr - 14)]


def demo_track(rng, km, lat=55.3959, lon=10.3883, points=240):
    """A wobbly loop of about `km` around central Odense."""
    r = km / (2 * math.pi)
    wobble = [(rng.uniform(0.05, 0.15), rng.uniform(0, 2 * math.pi), k) for k in (3, 5, 7)]
    track = []
    for i in range(points + 1):
        t = 2 * math.pi * i / points
        rr = r * (1 + sum(a * math.sin(k * t + ph) for a, ph, k in wobble))
        track.append([round(lat + rr * math.sin(t) / 111.3, 6),
                      round(lon + rr * 1.3 * (math.cos(t) - 1) / (111.3 * math.cos(math.radians(lat))), 6)])
    return track


def fill_details(db_path, activity_id):
    """A made-up route and laps (per km, per 5 km on the bike) for a demo activity, the first time it's opened."""
    conn = db.connect(db_path)
    try:
        a = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        if not a or db.get_laps(conn, activity_id) is not None:
            return
        rng = random.Random(activity_id)
        km = (a["distance_m"] or 0) / 1000
        laps = []
        if a["type"] in ("running", "road_biking", "walking") and km > 0:
            if db.get_track(conn, activity_id) is None:
                db.set_track(conn, activity_id, demo_track(rng, km))
            pace, step = a["duration_s"] / km, 5.0 if a["type"] == "road_biking" else 1.0
            left = km
            while left > 0.05:
                lap_km = min(step, left)
                lap_pace = pace * rng.uniform(0.95, 1.05)
                laps.append({"km": round(lap_km, 2), "duration_s": lap_km * lap_pace, "speed": 1000 / lap_pace,
                             "avg_hr": round(a["avg_hr"] + rng.uniform(-6, 6)), "elev_gain": round(rng.uniform(0, 12)),
                             "cadence": round(rng.uniform(164, 178)) if a["type"] == "running" else None})
                left -= lap_km
        db.set_laps(conn, activity_id, laps)
        conn.commit()
    finally:
        conn.close()
