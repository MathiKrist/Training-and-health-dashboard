"""Turn the raw tables into the numbers the dashboard shows."""
import json
import math
from datetime import date, timedelta
from statistics import median

import config
import db
import changes as chat_changes
import plans
import review
import workouts

CTL_DAYS = 42  # fitness: long-term average load
ATL_DAYS = 7   # fatigue: short-term average load
RECENT_DAYS = 7  # Garmin's daily numbers are shown up to this old (marked with their date when not from today)


def is_run(activity_type):
    return "running" in (activity_type or "")


# Sport groups for the activity filter and the sport pages: (key, label, Garmin type keys). First match wins; type keys
# are matched as substrings. The page gets the keys and labels from here.
SPORTS = [
    ("run", "Running", ("running",)),
    ("bike", "Cycling", ("cycling", "biking", "virtual_ride", "bmx")),
    ("swim", "Swimming", ("swimming",)),
    ("walk", "Walking", ("walking",)),
    ("hike", "Hiking", ("hiking", "mountaineering")),
    ("strength", "Strength", ("strength",)),
    ("disc_golf", "Disc golf", ("disc_golf",)),
    ("yoga", "Yoga", ("yoga", "pilates", "breathwork", "meditation")),
    ("cardio", "Gym & cardio", ("cardio", "hiit", "elliptical", "stair", "rowing", "fitness_equipment", "floor_climbing", "jump_rope", "boxing")),
    ("winter", "Winter sports", ("ski", "snowboard", "snowshoe", "skating")),
]
SPORT_LABELS = [[key, label] for key, label, _ in SPORTS] + [["other", "Other"]]


def sport_of(activity_type):
    t = activity_type or ""
    return next((sport for sport, _, keys in SPORTS if any(k in t for k in keys)), "other")


# Garmin's primary training effect label -> (name, load focus group)
EFFECTS = {
    "RECOVERY": ("Recovery", "low"), "AEROBIC_BASE": ("Base", "low"),
    "TEMPO": ("Tempo", "high"), "LACTATE_THRESHOLD": ("Threshold", "high"), "VO2MAX": ("VO2 max", "high"),
    "ANAEROBIC_CAPACITY": ("Anaerobic", "anaerobic"), "SPRINT": ("Sprint", "anaerobic"),
}
FOCUS = {label: group for label, (_, group) in EFFECTS.items()}

# The distances for personal bests and race goals: metres -> label. Best efforts are worked out for all of them;
# Garmin keeps its own fastest splits for all but 400 m, and predicts race times for 5 km to marathon.
DISTANCES = {400: "400 m", 1000: "1 km", 1609: "1 mile", 5000: "5 km", 10000: "10 km", 21098: "Half marathon", 42195: "Marathon"}
# Garmin's fastest splits: metres -> label
BEST_EFFORTS = {m: label for m, label in DISTANCES.items() if m != 400}


def run_focus(a, threshold):
    """low / high / anaerobic, from Garmin's label; falls back to average HR vs. the aerobic threshold."""
    if a.get("te_label") in FOCUS:
        return FOCUS[a["te_label"]]
    if a.get("avg_hr"):
        return "low" if a["avg_hr"] < threshold else "high"
    return None


TRIMP_SCALE_MIN = 10  # activities with both Garmin's load and heart rate needed to put TRIMP on Garmin's scale


def trimp(duration_s, avg_hr, rest_hr, max_hr):
    """Banister TRIMP, used only when Garmin has no training load."""
    if not duration_s or not avg_hr or max_hr <= rest_hr:
        return 0.0
    hrr = max(0.0, min(1.0, (avg_hr - rest_hr) / (max_hr - rest_hr)))
    return duration_s / 60 * hrr * 0.64 * math.exp(1.92 * hrr)


# Form as % of fitness: (name, lower bound). The page names today's band, and the chart's tooltip each day's.
FORM_ZONES = [("Rested", 25), ("Fresh", 5), ("Balanced", -10), ("Building", -30), ("Overloaded", None)]


def _form_pct(ctl, atl):
    return round((ctl - atl) / ctl * 100, 1) if ctl >= 5 else None


def _form_state(pct):
    if pct is None:
        return "Warming up"  # not enough training history yet for form to mean much
    return next(name for name, low in FORM_ZONES if low is None or pct > low)


# Garmin's training statuses -> the name the page shows (others are shown as Garmin names them)
TRAINING_STATUS = {"NO_STATUS": "No status"}


# Garmin's load focus zones: phrase prefix -> (key, name, what to add when short of it)
LOAD_ZONES = {
    "AEROBIC_LOW": ("low", "low aerobic", "easy and long sessions at a conversational pace"),
    "AEROBIC_HIGH": ("high", "high aerobic", "tempo, threshold or VO2 max intervals"),
    "ANAEROBIC": ("anaerobic", "anaerobic", "short, very hard intervals of 30 seconds to 2 minutes, or sprints"),
}


def _load_focus_text(phrase):
    """Garmin's phrase ("AEROBIC_HIGH_SHORTAGE", "BALANCED", ...) -> (title, advice, zone key)."""
    phrase = phrase or ""
    for prefix, (key, name, tip) in LOAD_ZONES.items():
        if phrase.startswith(prefix + "_SHORTAGE"):
            return f"Short on {name}", f"Your {name} load is below its optimal range. Add more {tip}.", key
        if phrase.startswith(prefix + "_FOCUS"):
            return f"Focused on {name}", f"Most of your load is {name}. Mix in the other zones to keep improving.", key
    if phrase.startswith("BALANCED"):
        return "Balanced", "All three zones are in their optimal range. Keep training the way you are.", None
    return phrase.replace("_", " ").capitalize() or "No load focus yet", "", None


def _load_focus(days, today):
    recent = [d for d in days if d.get("load_low") is not None and d["date"] >= (today - timedelta(days=RECENT_DAYS)).isoformat()]
    if not recent:
        return None
    cur = recent[-1]
    title, advice, key = _load_focus_text(cur.get("load_focus"))
    zones = [{"key": k, "label": name.capitalize(), "value": cur.get(f"load_{k}"),
              "min": cur.get(f"load_{k}_min"), "max": cur.get(f"load_{k}_max")}
             for k, name, _ in LOAD_ZONES.values()]
    return {"title": title, "advice": advice, "zone": key, "zones": zones, "as_of": cur["date"]}


def _training_status(days, today):
    """Garmin's latest training status, plus one entry per day for the last four weeks."""
    by_date = {d["date"]: d for d in days}
    history = []
    for i in range(27, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        history.append({"date": d, "status": (by_date.get(d) or {}).get("training_status")})

    recent = [d for d in days if d.get("training_status") and d["date"] >= (today - timedelta(days=RECENT_DAYS)).isoformat()]
    if not recent:
        return None
    cur = recent[-1]
    code = cur["training_status"]
    return {
        "code": code, "label": TRAINING_STATUS.get(code) or code.replace("_", " ").capitalize(), "as_of": cur["date"],
        "since": cur.get("training_status_since"),
        "acute_load": cur.get("acute_load"), "acute_min": cur.get("acute_load_min"),
        "acute_max": cur.get("acute_load_max"), "acwr_status": cur.get("acwr_status"),
        "history": history,
    }


def _vo2max(days, today):
    """Garmin's VO2 max at the end of each week, plus where it stood 3 and 12 months ago."""
    points = [(d["date"], d["vo2max"]) for d in days if d.get("vo2max")]
    if len(points) < 2:
        return None
    first = date.fromisoformat(points[0][0])
    week_end = first + timedelta(days=6 - first.weekday())  # Sunday of the first week
    weeks, i, value = [], 0, None
    while True:
        end = min(week_end, today)
        while i < len(points) and points[i][0] <= end.isoformat():
            value = points[i][1]
            i += 1
        weeks.append({"week": (week_end - timedelta(days=6)).isoformat(), "vo2max": value})
        if week_end >= today:
            break
        week_end += timedelta(days=7)

    def as_of(day):
        before = [v for d, v in points if d <= day.isoformat()]
        return before[-1] if before else None

    year_ago = (today - timedelta(days=365)).isoformat()
    return {
        "now": points[-1][1],
        "peak": max(v for d, v in points if d >= year_ago) if points[-1][0] >= year_ago else points[-1][1],
        "ago_3m": as_of(today - timedelta(days=91)), "ago_12m": as_of(today - timedelta(days=365)),
        "weeks": weeks[-53:],  # the chart shows the last year
    }


# Garmin's predicted distances: metres -> daily column
PREDICTED = {5000: "pred_5k", 10000: "pred_10k", 21098: "pred_half", 42195: "pred_marathon"}


def predicted_time(day, metres):
    """Garmin's prediction for the distance on one day; other distances are scaled from the nearest one (Riegel)."""
    known = [(m, day.get(col)) for m, col in PREDICTED.items() if day.get(col)]
    if not known:
        return None
    m, t = min(known, key=lambda k: abs(math.log(metres / k[0])))
    return round(t * (metres / m) ** 1.06)


def _race(goal, days, acts, today):
    """The race goal for the overview: target, Garmin's prediction and its last six months, and the result once run.
    Shown until a week after race day."""
    race_day = date.fromisoformat(goal["date"])
    if today > race_day + timedelta(days=7):
        return None
    m = goal["metres"]
    since = (today - timedelta(days=182)).isoformat()
    history = [{"date": d["date"], "s": predicted_time(d, m)} for d in days if d["date"] >= since]
    history = [h for h in history if h["s"]]
    # The result: the run on race day closest to the distance, timed over the distance where it's known
    result = None
    runs = [a for a in acts if a["date"] == goal["date"] and is_run(a["type"]) and (a["distance_m"] or 0) >= m * 0.95]
    if runs:
        a = min(runs, key=lambda r: abs(r["distance_m"] - m))
        best = (a.get("efforts") or {}).get(int(m))
        # Elapsed time, as a race is timed: stops count
        result = {"s": round(best or (a["elapsed_s"] or a["duration_s"]) * m / a["distance_m"]), "id": a["id"]}
    return {**goal, "days_to_go": (race_day - today).days, "predicted_s": history[-1]["s"] if history else None,
            "history": history, "result": result}


ACTIVITY_SELECT = (
    "id, start_local, date, type, name, distance_m, duration_s, avg_hr, max_hr, avg_speed, elev_gain, "
    "training_load, aerobic_te, anaerobic_te, te_label, "
    "json_extract(raw, '$.activeSets') AS sets, json_extract(raw, '$.totalReps') AS reps, "
    "json_extract(raw, '$.locationName') AS location, json_extract(raw, '$.elevationLoss') AS elev_loss, "
    "json_extract(raw, '$.maxSpeed') AS max_speed, json_extract(raw, '$.elapsedDuration') AS elapsed_s, "
    "json_extract(raw, '$.workoutId') AS workout_id, "
    + ", ".join(f"json_extract(raw, '$.fastestSplit_{m}') AS best_{m}" for m in BEST_EFFORTS)
)


def trimp_scale(acts, rest_hr):
    """TRIMP and Garmin's training load are on different scales. Over the activities that have both, this is how many
    Garmin load points one TRIMP point is worth, so activities without Garmin's load count the same in fitness."""
    ratios = [a["training_load"] / t for a in acts if (a["training_load"] or 0) > 0
              for t in [trimp(a["duration_s"], a["avg_hr"], rest_hr, config.MAX_HR)] if t > 0]
    return median(ratios) if len(ratios) >= TRIMP_SCALE_MIN else 1.0


def _add_load(a, rest_hr, scale):
    """Garmin's training load, or TRIMP (on Garmin's scale) when Garmin has none; plus the run's load focus."""
    load = a["training_load"]
    if load is None:
        load = trimp(a["duration_s"], a["avg_hr"], rest_hr, config.MAX_HR) * scale
    a["load"] = round(load or 0, 1)
    a["focus"] = run_focus(a, config.AEROBIC_THRESHOLD) if is_run(a["type"]) else None


def _summary(a):
    """What the activity list (and the top of the popup) shows for one activity."""
    return {
        "id": a["id"], "date": a["date"], "start": a["start_local"], "type": a["type"], "sport": sport_of(a["type"]),
        "name": a["name"], "km": round((a["distance_m"] or 0) / 1000, 2), "duration_s": a["duration_s"],
        "speed": a["avg_speed"], "elev_m": a["elev_gain"], "descent_m": a["elev_loss"], "avg_hr": a["avg_hr"], "load": a["load"],
        "sets": a["sets"], "reps": a["reps"], "location": a["location"],
        "focus": a["focus"], "te_label": a["te_label"], "max_speed": a["max_speed"],
        # Fastest time over set distances within the activity ({metres: seconds}), for the personal bests:
        # worked out from the run's own data where synced (400 m included), otherwise Garmin's fastest splits
        "best": a.get("efforts") or {m: a[f"best_{m}"] for m in BEST_EFFORTS if a.get(f"best_{m}")} or None,
    }


# The popup's extra stats: our key -> Garmin's field in the activity summary
DETAIL_FIELDS = {
    "calories": "calories", "bmr_calories": "bmrCalories", "elapsed_s": "elapsedDuration", "max_speed": "maxSpeed", "gap_speed": "avgGradeAdjustedSpeed",
    "cadence": "averageRunningCadenceInStepsPerMinute", "max_cadence": "maxRunningCadenceInStepsPerMinute",
    "bike_cadence": "averageBikingCadenceInRevPerMinute", "max_bike_cadence": "maxBikingCadenceInRevPerMinute",
    "steps": "steps", "stride_cm": "avgStrideLength", "gct_ms": "avgGroundContactTime", "gct_balance": "avgGroundContactBalance",
    "vert_osc_cm": "avgVerticalOscillation", "vert_ratio": "avgVerticalRatio",
    "power": "avgPower", "max_power": "maxPower", "norm_power": "normPower",
    "elev_loss": "elevationLoss", "min_elev": "minElevation", "max_elev": "maxElevation",
    "avg_elev": "avgElevation", "max_vert_speed": "maxVerticalSpeed",
    "resp": "avgRespirationRate", "min_resp": "minRespirationRate", "max_resp": "maxRespirationRate",
    "aerobic_msg": "aerobicTrainingEffectMessage", "anaerobic_msg": "anaerobicTrainingEffectMessage",
    "vo2max": "vO2MaxValue", "body_battery": "differenceBodyBattery", "water_ml": "waterEstimated",
    "moderate_min": "moderateIntensityMinutes", "vigorous_min": "vigorousIntensityMinutes", "lap_count": "lapCount",
    "pool_m": "poolLength", "lengths": "activeLengths", "strokes": "strokes", "swolf": "averageSwolf",
    "stroke_m": "avgStrokeDistance", "swim_cadence": "averageSwimCadenceInStrokesPerMinute",
    "min_temp": "minTemperature", "max_temp": "maxTemperature",
}


def build_activity(db_path, activity_id):
    """One activity for the popup: its summary, every extra stat Garmin has, its route and its laps."""
    conn = db.connect(db_path)
    try:
        row = conn.execute(f"SELECT {ACTIVITY_SELECT}, raw FROM activities WHERE id = ?", (activity_id,)).fetchone()
        if not row:
            return None
        rests = [r[0] for r in conn.execute("SELECT resting_hr FROM daily WHERE resting_hr IS NOT NULL")]
        both = [dict(r) for r in conn.execute(
            "SELECT duration_s, avg_hr, training_load FROM activities WHERE training_load > 0 AND avg_hr > 0")]
        track = db.get_track(conn, activity_id)
        laps = db.get_laps(conn, activity_id)
        efforts = db.get_efforts(conn, activity_id)
    finally:
        conn.close()
    a = dict(row)
    raw = json.loads(a.pop("raw") or "null") or {}
    rest_hr = median(rests) if rests else 55
    _add_load(a, rest_hr, trimp_scale(both, rest_hr))
    stats = {key: raw.get(field) for key, field in DETAIL_FIELDS.items()}
    stats.update(max_hr=a["max_hr"], aerobic_te=a["aerobic_te"], anaerobic_te=a["anaerobic_te"])
    hr_zones = [raw.get(f"hrTimeInZone_{i}") for i in range(1, 6)]
    power_zones = [raw.get(f"powerTimeInZone_{i}") for i in range(1, 6)]
    return {
        **_summary(a),
        "stats": {k: v for k, v in stats.items() if v is not None},
        "hr_zones": hr_zones if any(hr_zones) else None,
        "power_zones": power_zones if any(power_zones) else None,
        # The run's own best efforts (400 m included), else Garmin's fastest splits until they're worked out
        "best": [{"label": label, "m": m, "s": efforts[m]} for m, label in DISTANCES.items() if m in efforts] if efforts
                else [{"label": label, "m": m, "s": raw[f"fastestSplit_{m}"]}
                      for m, label in BEST_EFFORTS.items() if raw.get(f"fastestSplit_{m}")],
        "track": track or None,
        "laps": laps or [],
    }


def build_dashboard(db_path=None):
    conn = db.connect(db_path or config.DB_PATH)
    try:
        acts = [dict(r) for r in conn.execute(
            f"SELECT {ACTIVITY_SELECT} FROM activities WHERE date IS NOT NULL AND date != '' ORDER BY start_local")]
        track = db.get_track(conn, acts[-1]["id"]) if acts else None
        # The newest activity of each sport the Workout page has, with its route, for the top of that page
        last_of = {}
        for a in reversed(acts):
            if sport_of(a["type"]) in workouts.ALL_SPORTS and sport_of(a["type"]) not in last_of:
                last_of[sport_of(a["type"])] = a
        last_tracks = {k: db.get_track(conn, a["id"]) for k, a in last_of.items()}
        archive = review.load_archive(conn)
        efforts = db.all_efforts(conn)
        days = [dict(r) for r in conn.execute("SELECT * FROM daily ORDER BY date")]
        goal = db.get_meta(conn, "race_goal")
        profile = json.loads(db.get_meta(conn, "training_profile") or "{}")
        sent = json.loads(db.get_meta(conn, "workouts_sent") or "[]")
        plan_cfg = json.loads(db.get_meta(conn, "training_plan") or "null")
        plan_sent = json.loads(db.get_meta(conn, "plan_sent") or "[]")
        changes = chat_changes.load(conn)
        # Laps of the plan's runs, to see how the intervals went
        plan_laps = {r["activity_id"]: json.loads(r["laps"]) for r in conn.execute(
            "SELECT l.activity_id, l.laps FROM laps l JOIN activities a ON a.id = l.activity_id WHERE a.date >= ?",
            (plan_cfg["start"],))} if plan_cfg else {}
        meta = {
            "last_sync_at": db.get_meta(conn, "last_sync_at"),
            "last_error": db.get_meta(conn, "last_error") or None,
            "last_error_at": db.get_meta(conn, "last_error_at"),
        }
    finally:
        conn.close()

    today = date.today()
    rests = [d["resting_hr"] for d in days if d.get("resting_hr")]
    rest_hr = median(rests) if rests else 55

    # Load per activity and per day
    load_by_day = {}
    scale = trimp_scale(acts, rest_hr)
    for a in acts:
        a["efforts"] = efforts.get(a["id"])
        _add_load(a, rest_hr, scale)
        load_by_day[a["date"]] = load_by_day.get(a["date"], 0) + a["load"]

    # Fitness / fatigue / form
    fitness = []
    if acts:
        first = date.fromisoformat(acts[0]["date"])
        k_ctl = 1 - math.exp(-1 / CTL_DAYS)
        k_atl = 1 - math.exp(-1 / ATL_DAYS)
        ctl = atl = 0.0
        d = first
        while d <= today:
            load = load_by_day.get(d.isoformat(), 0)
            ctl += (load - ctl) * k_ctl
            atl += (load - atl) * k_atl
            fitness.append({"date": d.isoformat(), "ctl": round(ctl, 1), "atl": round(atl, 1), "form_pct": _form_pct(ctl, atl)})
            d += timedelta(days=1)
    now = fitness[-1] if fitness else {"ctl": 0, "form_pct": None}

    # Health trends for the health page (the last year; the page shows 90 days or all of it)
    cutoff = (today - timedelta(days=364)).isoformat()
    trends = [{k: d.get(k) for k in ("date", "hrv_last_night", "hrv_low", "hrv_high", "resting_hr", "sleep_s",
                                       "sleep_score", "bb_high", "bb_low", "stress_avg", "steps",
                                       "cal_total", "cal_active", "cal_resting")}
              for d in days if d["date"] >= cutoff]

    # Today: newest value of each field from the last week, with the day it's from (the page marks older ones)
    recent = [d for d in days if d["date"] >= (today - timedelta(days=RECENT_DAYS)).isoformat()]
    recent.sort(key=lambda d: d["date"], reverse=True)

    def latest(field):
        return next(((d[field], d["date"]) for d in recent if d.get(field) is not None), (None, None))

    today_vals, today_dates = {}, {}
    for f in ("readiness", "hrv_last_night", "hrv_low", "hrv_high", "sleep_s", "sleep_score", "resting_hr",
              "bb_high", "bb_low", "stress_avg", "steps", "cal_total", "cal_active", "cal_resting"):
        today_vals[f], today_dates[f] = latest(f)
    today_vals["dates"] = today_dates
    week_rhr = [d["resting_hr"] for d in days if d.get("resting_hr") and d["date"] > (today - timedelta(days=7)).isoformat()]
    today_vals["resting_hr_7d"] = round(sum(week_rhr) / len(week_rhr)) if week_rhr else None

    # Every activity, newest first, so the page can filter by sport and total up this week and this year
    recent_acts = [_summary(a) for a in reversed(acts)]

    status, focus = _training_status(days, today), _load_focus(days, today)
    race = _race(json.loads(goal), days, acts, today) if goal else None
    focus_phrase = next((d["load_focus"] for d in recent if d.get("load_focus")), None)
    pred_5k = next((d["pred_5k"] for d in recent if d.get("pred_5k")), None)
    # This morning's recovery time, sleep and HRV (an older value says nothing about today)
    morning = next((d for d in recent if d["date"] == today.isoformat()), {})
    body = {k: morning.get(k) for k in ("recovery_h", "sleep_score", "hrv_status")}
    today_vals["recovery_h"] = body["recovery_h"]
    workout_list = workouts.recommend(
        recent_acts, today, readiness=today_vals["readiness"], status=status and status["code"], focus=focus_phrase,
        focus_title=focus and focus["title"], race=race, profile=profile, pred_5k=pred_5k, sent=sent, **body)

    # The training plan; on its days its session replaces the suggested one for its sport, on the days between it's a
    # rest day; its swims come after the suggested sports
    plan = plans.build(plan_cfg, recent_acts, plan_laps, profile, today, plan_sent, changes) if plan_cfg else None
    level = workouts.strain(today_vals["readiness"], status and status["code"], **body)
    for pw in plans.todays(plan, today) if plan else []:
        if level >= 2 and pw["kind"] != "rest":
            if today_vals["readiness"] is not None:
                pw["why"].append(f"Readiness {round(today_vals['readiness'])}")
            if body["recovery_h"]:
                pw["why"].append(f"Recovery {round(body['recovery_h'])} h")
            pw["why"].append("Consider rest instead" if level == 3 else
                             f"Consider a recovery {'run' if pw['sport'] == 'run' else 'session'} or rest instead")
        if any(w["sport"] == pw["sport"] for w in workout_list):
            workout_list = [pw if w["sport"] == pw["sport"] else w for w in workout_list]
        elif pw["sport"] == "run":
            workout_list.insert(0, pw)
        else:
            workout_list.append(pw)
    # Changes made in the chat, outside the plan's sessions (the plan has those): today's replaces the suggestion, later
    # ones are listed under it
    in_plan = lambda c: (plan and not plan["over"] and c["sport"] in plan["sports"]
                         and plan_cfg["start"] <= c["date"] <= plan_cfg["date"])
    targets = workouts.Targets(profile, pred_5k)
    upcoming = []
    for c in changes:
        if c["date"] < today.isoformat() or in_plan(c):
            continue
        if c["date"] == today.isoformat():
            w = chat_changes.workout(c, targets, sent)
            if any(x["sport"] == c["sport"] for x in workout_list):
                workout_list = [w if x["sport"] == c["sport"] else x for x in workout_list]
            else:
                workout_list.append(w)
        else:
            x = chat_changes.session(c, targets)
            upcoming.append({"date": c["date"], "sport": c["sport"], "title": x["title"] if x else "Rest day",
                             "total_s": x["total_s"] if x else 0, "km": x["km"] if x else None})

    # Whether each sport's newest activity was run from a workout the dashboard sent, so it can be reviewed
    last_by_sport = {}
    for k, a in last_of.items():
        summary = next(x for x in recent_acts if x["id"] == a["id"])
        last_by_sport[k] = {**summary, "track": last_tracks[k] or None,
                            "review": review.planned({**summary, "workout_id": a["workout_id"]}, archive, sent, plan_sent,
                                                     plan, workout_list) is not None}

    if race and plan and plan["projection"] and plan_cfg["goal"] == "race" and plan_cfg["date"] == race["date"]:
        race["plan"] = {k: plan["projection"][k] for k in ("low", "high")}

    return {
        "generated": today.isoformat(),
        "meta": meta,
        "has_data": bool(acts or days),
        "form": {"state": _form_state(now["form_pct"]), "ctl": now["ctl"], "pct": now["form_pct"]},
        "form_zones": [{"name": n, "min": low} for n, low in FORM_ZONES],
        "today": today_vals,
        "training_status": status,
        "load_focus": focus,
        "vo2max": _vo2max(days, today),
        "race": race,
        "workouts": workout_list,
        "workouts_ahead": upcoming,  # later days changed in the chat, outside a plan
        "plan": plan,
        "fitness": fitness[-365:],  # the chart shows 120 days or the whole year
        "trends": trends,
        "activities": recent_acts,
        # The newest activity, with its GPS track when it has one (for the map at the top)
        "last_activity": {**recent_acts[0], "track": track or None} if acts else None,
        "last_by_sport": last_by_sport,  # the newest run, ride and swim, for the top of the Workout page
        "activities_from": acts[0]["date"] if acts else None,  # the first synced activity, for "this year" totals
        # Names the page shows, from the tables above
        "sports": SPORT_LABELS,
        "effects": EFFECTS,
        "distances": DISTANCES,
        "race_distances": list(PREDICTED),
    }
