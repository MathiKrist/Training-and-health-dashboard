"""Today's workout per sport: what to train, worked out from Garmin's own numbers, as steps the page shows and the
watch can run.

The session comes from readiness, recovery time, sleep, HRV, training status, load focus, the last hard session and the
race goal; a rest day when the body says so. Run paces come
from Garmin's lactate threshold pace (or the race predictor), bike targets from the heart rate zones."""
from datetime import date, timedelta
from statistics import median

import config

SPORTS = {"run": "Running", "bike": "Cycling"}  # the sports a workout can be made for, in this order when neither is main
ALL_SPORTS = {**SPORTS, "swim": "Swimming"}  # and the ones a training plan can hold too
TYPICAL_MIN = {"run": 45, "bike": 75}  # the usual session length when there are no recent ones to go by
LONG_MAX_MIN = {"run": 120, "bike": 240}
SHORT = 0.6  # share of the usual length on a day to hold back a little
SHORT_MAX_MIN = {"run": 40, "bike": 60}
HRV_OFF = {"LOW", "UNBALANCED", "POOR"}  # Garmin HRV statuses that count against training hard
RECENT = 60  # days of activities that set the usual session length
HARD = {"TEMPO", "LACTATE_THRESHOLD", "VO2MAX", "ANAEROBIC_CAPACITY", "SPRINT"}  # Garmin training effects that count as hard
THRESHOLD = {"TEMPO", "LACTATE_THRESHOLD"}

# Run paces as a share of lactate threshold speed (slow end, fast end), close to Jack Daniels' training paces
RUN_PACE = {"recovery": (0.68, 0.74), "easy": (0.74, 0.82), "steady": (0.86, 0.90), "threshold": (0.97, 1.01),
            "tempo": (0.92, 0.96), "over": (1.02, 1.05), "under": (0.94, 0.97)}
# Intervals off the 5 km race pace (Garmin's prediction): VO2 max at it, anaerobic repeats faster
FIVE_K_PACE = {"vo2": (0.98, 1.02), "anaerobic": (1.06, 1.10)}
# Bike power as a share of FTP (low end, high end), and the heart rate zone in its place without an FTP
BIKE_POWER = {"recovery": (0.45, 0.55, 1), "endurance": (0.56, 0.75, 2), "tempo": (0.76, 0.87, 3),
              "sweet_spot": (0.88, 0.94, 3), "threshold": (0.95, 1.05, 4), "vo2": (1.06, 1.20, 5)}
LT_FROM_5K = 0.94  # threshold speed against 5 km race speed, when Garmin has no threshold
ZONE_FLOORS = (0.50, 0.60, 0.70, 0.80, 0.90)  # % of max HR, when Garmin's zones aren't synced

TITLES = {
    "recovery": "Recovery", "easy": "Easy", "long": "Long", "threshold": "Threshold intervals",
    "vo2": "VO2 max intervals", "anaerobic": "Anaerobic repeats", "shakeout": "Shakeout",
}
NOUN = {"run": "run", "bike": "ride"}


def _step(kind, label, s, intensity, target=None, note=None):
    if kind in ("warmup", "cooldown"):  # warming up and cooling down go by feel, never to a target
        target = None
    return {"kind": kind, "label": label, "s": round(s), "intensity": intensity, "target": target, "note": note}


def _repeat(n, *steps):
    return {"kind": "repeat", "n": n, "steps": list(steps)}


def _round5(minutes):
    return max(5, int(5 * round(minutes / 5)))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Targets:
    """Pace and heart rate targets from the athlete's profile."""

    def __init__(self, profile, pred_5k):
        lt = profile.get("lt_speed")
        five = 5000 / pred_5k if pred_5k else None
        self.lt = lt or (five * LT_FROM_5K if five else None)
        self.five = five or (self.lt / LT_FROM_5K if self.lt else None)
        floors = profile.get("zones") or [round(config.MAX_HR * f) for f in ZONE_FLOORS] + [config.MAX_HR]
        self.floors = floors  # zone 1-5 floors, then max HR
        self.ftp = profile.get("ftp")

    def pace(self, kind):
        """{type: pace, slow, fast} in seconds per km, or None without a threshold."""
        if kind in RUN_PACE and self.lt:
            lo, hi = RUN_PACE[kind]
            return {"type": "pace", "slow": round(1000 / (self.lt * lo)), "fast": round(1000 / (self.lt * hi))}
        if kind in FIVE_K_PACE and self.five:
            lo, hi = FIVE_K_PACE[kind]
            return {"type": "pace", "slow": round(1000 / (self.five * lo)), "fast": round(1000 / (self.five * hi))}
        return None

    def hr(self, zone):
        f = self.floors
        return {"type": "hr", "zone": zone, "lo": f[zone - 1], "hi": f[zone] - (1 if zone < 5 else 0)}

    def run(self, kind, zone):
        """A run target: the pace when it's known, the heart rate zone otherwise."""
        return self.pace(kind) or self.hr(zone)

    def bike(self, kind, share=None):
        """A ride target: watts from the FTP when Garmin has one, the heart rate zone otherwise. `share` gives the
        (low, high) share of FTP for a kind not in BIKE_POWER (race effort), with its zone."""
        lo, hi, zone = share or BIKE_POWER[kind]
        if self.ftp:
            return {"type": "power", "lo": round(self.ftp * lo), "hi": round(self.ftp * hi)}
        return self.hr(zone)


def _run_steps(kind, total, t):
    if kind == "recovery":
        return [_step("interval", "Recovery run", total * 60, "easy", t.run("recovery", 1))]
    if kind in ("easy", "long"):
        return [_step("interval", f"{TITLES[kind]} run", total * 60, "easy", t.run("easy", 2))]
    if kind == "shakeout":
        return [_step("warmup", "Warm-up", 600, "easy"),
                _repeat(4, _step("interval", "Stride", 20, "anaerobic", None, "Fast but relaxed"),
                        _step("recovery", "Easy", 100, "rest")),
                _step("cooldown", "Cool-down", 300, "easy")]
    warm, cool = 15, 10
    if kind == "threshold":
        n = _clamp(round((total - warm - cool) / 10), 2, 4)
        main = _repeat(n, _step("interval", "Threshold", 480, "high", t.run("threshold", 4)),
                       _step("recovery", "Easy jog", 120, "rest"))
    elif kind == "vo2":
        n = _clamp(round((total - warm - cool) / 6), 3, 6)
        main = _repeat(n, _step("interval", "Hard", 180, "high", t.run("vo2", 5)),
                       _step("recovery", "Easy jog", 180, "rest"))
    else:  # anaerobic
        main = _repeat(8, _step("interval", "Fast", 45, "anaerobic", t.pace("anaerobic"), None if t.five else "Fast, near all-out"),
                       _step("recovery", "Walk or jog", 135, "rest"))
    return [_step("warmup", "Warm-up", warm * 60, "easy"), main,
            _step("cooldown", "Cool-down", cool * 60, "easy")]


def _bike_steps(kind, total, t):
    if kind == "recovery":
        return [_step("interval", "Recovery ride", total * 60, "easy", t.bike("recovery"))]
    if kind in ("easy", "long", "shakeout"):
        return [_step("interval", f"{TITLES['easy' if kind == 'shakeout' else kind]} ride", total * 60, "easy", t.bike("endurance"))]
    warm, cool = 15, 10
    if kind == "threshold":
        n = _clamp(round((total - warm - cool) / 14), 2, 4)
        main = _repeat(n, _step("interval", "Threshold", 600, "high", t.bike("threshold")), _step("recovery", "Easy spin", 240, "rest"))
    elif kind == "vo2":
        n = _clamp(round((total - warm - cool) / 8), 3, 6)
        main = _repeat(n, _step("interval", "Hard", 240, "high", t.bike("vo2")), _step("recovery", "Easy spin", 240, "rest"))
    else:  # anaerobic: heart rate and power lag behind 30 s sprints, so they go by feel
        main = _repeat(8, _step("interval", "Sprint", 30, "anaerobic", None, "All-out"), _step("recovery", "Easy spin", 150, "rest"))
    return [_step("warmup", "Warm-up", warm * 60, "easy"), main, _step("cooldown", "Cool-down", cool * 60, "easy")]


def _flat(steps):
    for s in steps:
        if s["kind"] == "repeat":
            for _ in range(s["n"]):
                yield from s["steps"]
        else:
            yield s


def km_of(steps, t):
    """Distance from the middle of each step's pace range, steps without one at easy pace."""
    easy = t.pace("easy")
    if not easy:
        return None
    mid = lambda p: (p["slow"] + p["fast"]) / 2
    return round(sum(s["s"] / mid(s["target"] if (s["target"] or {}).get("type") == "pace" else easy) for s in _flat(steps)), 1)


def _ago(n):
    return "today" if n == 0 else "yesterday" if n == 1 else f"{n} days ago"


def strain(readiness, status, recovery_h=None, sleep_score=None, hrv_status=None):
    """How much to hold back today, from Garmin's morning numbers: 0 train as usual, 1 short and easy, 2 recovery
    session, 3 rest. One mild sign shortens the session; two of them, or one strong sign, make it a recovery one."""
    low = lambda v, limit: v is not None and v < limit
    recovery_h = recovery_h or 0
    over = status == "OVERREACHING"
    if low(readiness, 20) or recovery_h >= 48 or (over and low(readiness, 50)):
        return 3
    mild = sum((low(readiness, 50), recovery_h >= 12, low(sleep_score, 60), hrv_status in HRV_OFF))
    if low(readiness, 35) or recovery_h >= 24 or over or mild >= 2:
        return 2
    return 1 if mild else 0


def _session(today, readiness, status, focus, focus_title, last_hard, last_threshold, days_to_go,
             recovery_h=None, sleep_score=None, hrv_status=None):
    """(kind, why, volume factor) for today, the same for every sport."""
    why = []
    if days_to_go is not None and 0 <= days_to_go <= 10:
        why.append("Race day" if days_to_go == 0 else f"Race in {days_to_go} day{'s' * (days_to_go != 1)}")
    if status == "OVERREACHING":
        why.append("Overreaching")
    if readiness is not None:
        why.append(f"Readiness {round(readiness)}")
    if recovery_h:
        why.append(f"Recovery {round(recovery_h)} h")
    if sleep_score is not None and sleep_score < 60:
        why.append(f"Sleep score {round(sleep_score)}")
    if hrv_status in HRV_OFF:
        why.append(f"HRV {hrv_status.lower()}")
    since_hard = (today - last_hard).days if last_hard else None
    if since_hard is not None and since_hard <= 7:
        why.append(f"Last hard session {_ago(since_hard)}")
    level = strain(readiness, status, recovery_h, sleep_score, hrv_status)
    if level == 3:
        return "rest", why, 0
    if days_to_go is not None and 0 <= days_to_go <= 2:
        return "shakeout", why, 1
    if level == 2:
        return "recovery", why, 1
    if level == 1:
        return "easy", why, SHORT
    if since_hard is not None and since_hard < 2:
        return "easy", why, 1
    taper = 0.7 if days_to_go is not None and days_to_go <= 10 else 1
    focus = focus or ""
    if focus.startswith("AEROBIC_HIGH_SHORTAGE") or focus.startswith("AEROBIC_LOW_FOCUS"):
        recent_threshold = last_threshold and (today - last_threshold).days < 7
        kind = "vo2" if recent_threshold else "threshold"
    elif focus.startswith("ANAEROBIC_SHORTAGE"):
        kind = "anaerobic"
    elif focus.startswith(("AEROBIC_LOW_SHORTAGE", "AEROBIC_HIGH_FOCUS", "ANAEROBIC_FOCUS")):
        kind = "easy" if taper < 1 else "long"
    else:  # balanced, or no load focus yet: a hard session every third day or so
        kind = "threshold" if since_hard is None or since_hard >= 3 else "easy"
    if focus and focus_title:
        why.insert(0, focus_title)
    return kind, why, taper


def recommend(acts, today, *, readiness, status, focus, focus_title, race, profile, pred_5k, sent,
              recovery_h=None, sleep_score=None, hrv_status=None):
    """One workout per sport, main sport first. `acts` are the activity summaries, newest first. Recovery time, sleep
    score and HRV status are this morning's, or None."""
    since = (today - timedelta(days=RECENT)).isoformat()
    recent = [a for a in acts if a["date"] >= since]

    def last(labels, focus_groups):
        return next((date.fromisoformat(a["date"]) for a in acts
                     if a.get("te_label") in labels or a.get("focus") in focus_groups), None)

    last_hard = last(HARD, ("high", "anaerobic"))
    last_threshold = last(THRESHOLD, ())
    days_to_go = race.get("days_to_go") if race else None
    kind, why, volume = _session(today, readiness, status, focus, focus_title, last_hard, last_threshold, days_to_go,
                                 recovery_h, sleep_score, hrv_status)
    t = Targets(profile, pred_5k)

    counts = {s: sum(a["sport"] == s for a in recent) for s in SPORTS}
    order = sorted(SPORTS, key=lambda s: -counts[s])
    out = []
    for sport in order:
        if kind == "rest":
            out.append({"sport": sport, "label": SPORTS[sport], "kind": "rest", "title": "Rest day", "total_s": 0,
                        "km": None, "why": why, "steps": [], "paces": False, "sent": False})
            continue
        durations = [a["duration_s"] / 60 for a in recent if a["sport"] == sport and a.get("duration_s")]
        typical = median(durations) if durations else TYPICAL_MIN[sport]
        total = {"recovery": min(30 if sport == "run" else 45, typical), "long": min(typical * 1.4, LONG_MAX_MIN[sport]),
                 "shakeout": 25 if sport == "run" else 40}.get(kind, typical)
        total = _round5(total * (1 if kind in ("recovery", "shakeout") else volume))
        if volume == SHORT:
            total = min(total, SHORT_MAX_MIN[sport])
        steps = (_run_steps if sport == "run" else _bike_steps)(kind, total, t)
        total_s = sum(s["s"] for s in _flat(steps))
        km = km_of(steps, t) if sport == "run" else None
        title = f"{TITLES[kind]} {NOUN[sport]}" if kind in ("recovery", "easy", "long", "shakeout") else TITLES[kind]
        out.append({
            "sport": sport, "label": SPORTS[sport], "kind": kind, "title": title, "total_s": total_s, "km": km,
            "why": why, "steps": steps, "paces": sport == "run" and t.lt is not None,
            "sent": any(x.get("date") == today.isoformat() and x.get("sport") == sport and x.get("title") == title for x in sent),
        })
    return out


# ---- Garmin's workout format, for sending a workout to the watch

GARMIN_SPORT = {"run": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
                "bike": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
                "swim": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3}}
STEP_TYPE = {"warmup": (1, 1), "cooldown": (2, 2), "interval": (3, 3), "recovery": (4, 4), "rest": (5, 5)}


def _garmin_steps(steps, order, swim=False):
    out = []
    for s in steps:
        order += 1
        if s["kind"] == "repeat":
            children, last = _garmin_steps(s["steps"], order, swim)
            out.append({
                "type": "RepeatGroupDTO", "stepOrder": order,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                "numberOfIterations": s["n"], "smartRepeat": False, "workoutSteps": children,
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
                "endConditionValue": float(s["n"]),
            })
            order = last
            continue
        # In the pool a pause between reps is a rest (the watch waits on the wall), and a swim goes by distance
        kind = "rest" if swim and s["kind"] == "recovery" else s["kind"]
        type_id, display = STEP_TYPE[kind]
        step = {
            "type": "ExecutableStepDTO", "stepOrder": order,
            "stepType": {"stepTypeId": type_id, "stepTypeKey": kind, "displayOrder": display},
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
            "endConditionValue": float(s["s"]),
        }
        if s.get("m"):
            step.update(endCondition={"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True},
                        endConditionValue=float(s["m"]))
        t = s["target"]
        if t and t["type"] == "pace":  # in m/s, the slow end first
            step.update(targetType={"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6},
                        targetValueOne=round(1000 / t["slow"], 4), targetValueTwo=round(1000 / t["fast"], 4))
        elif t and t["type"] == "power":  # in watts
            step.update(targetType={"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2},
                        targetValueOne=float(t["lo"]), targetValueTwo=float(t["hi"]))
        elif t and t["type"] == "hr":  # the watch's own zone, so it follows the zones set on the watch
            step.update(targetType={"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4},
                        zoneNumber=t["zone"])
        else:
            step["targetType"] = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
        if s.get("note"):
            step["description"] = s["note"]
        out.append(step)
    return out, order


def to_garmin(w, day):
    sport = GARMIN_SPORT[w["sport"]]
    steps, _ = _garmin_steps(w["steps"], 0, w["sport"] == "swim")
    workout = {
        "workoutName": w["title"],
        "sportType": sport, "estimatedDurationInSecs": w["total_s"],
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}],
    }
    if w["sport"] == "swim":
        workout.update(poolLength=float(w.get("pool") or 25), poolLengthUnit={"unitId": 1, "unitKey": "meter", "factor": 100.0})
    if w["why"]:
        workout["description"] = " · ".join(w["why"])
    return workout
