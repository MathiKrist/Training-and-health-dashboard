"""The sessions of a training plan's other sports, next to running: rides and swims, and a triathlon's brick runs.
Each gives (title, steps); when they go in the week is worked out in plans.py.

Rides take watts from Garmin's FTP when it has one, heart rate zones otherwise. Swims go by distance and feel, with
the time each step takes from the athlete's own swims."""
from workouts import _clamp, _repeat, _round5, _step

# ---- Rides

RIDES = {"easy": "Endurance ride", "long": "Long ride", "race_long": "Race-effort long ride", "sweet_spot": "Sweet spot",
         "threshold": "Threshold ride", "vo2": "VO2 max ride", "opener": "Opener ride"}
# A triathlon's bike effort, as a share of FTP, by distance (with the heart rate zone in its place)
RACE_POWER = {"sprint": (0.90, 0.98, 4), "olympic": (0.85, 0.92, 4), "half": (0.72, 0.80, 3), "full": (0.65, 0.73, 2)}


def ride(kind, total, t, week=0, tri=None):
    """(title, steps) of a ride about `total` minutes long. A long ride with `tri` set is race effort."""
    endurance = t.bike("endurance")
    spin = lambda s: _step("recovery", "Easy spin", s, "rest")
    if kind in ("easy", "long") and not (kind == "long" and tri):
        return RIDES[kind], [_step("interval", RIDES[kind], total * 60, "easy", endurance)]
    if kind == "long":  # race effort: blocks at the race's power in the middle of an endurance ride
        block = 30 if total >= 150 else 20
        n = _clamp((total - 40) // (block + 10), 1, 4)
        rest = max(10, total - 20 - n * (block + 10))
        return RIDES["race_long"], [_step("interval", "Endurance", 20 * 60, "easy", endurance),
                                    _repeat(n, _step("interval", "Race effort", block * 60, "high", t.bike(None, RACE_POWER[tri])),
                                            _step("recovery", "Endurance", 600, "easy", endurance)),
                                    _step("interval", "Endurance", rest * 60, "easy", endurance)]
    if kind == "opener":
        return RIDES["opener"], [_step("warmup", "Easy", 20 * 60, "easy"),
                                 _repeat(3, _step("interval", "Hard", 60, "high", t.bike("vo2")), spin(120)),
                                 _step("cooldown", "Easy", 11 * 60, "easy")]
    warm, cool = 15, 10
    main = total - warm - cool
    if kind == "sweet_spot":  # 3 × 12 min one week, 2 × 20 the next
        rep, n, rec = (12, _clamp(main // 16, 2, 4), 4) if week % 2 == 0 else (20 if main >= 50 else 15, 2, 5)
        work = _repeat(n, _step("interval", "Sweet spot", rep * 60, "high", t.bike("sweet_spot")), spin(rec * 60))
    elif kind == "threshold":  # 4 × 8 min, or 3 × 10
        rep, rec = (8, 4) if week % 2 == 0 else (10, 5)
        work = _repeat(_clamp(main // (rep + rec), 2, 4), _step("interval", "Threshold", rep * 60, "high", t.bike("threshold")),
                       spin(rec * 60))
    else:  # VO2 max: 4 min hard, 4 easy
        work = _repeat(_clamp(main // 8, 3, 6), _step("interval", "Hard", 240, "high", t.bike("vo2")), spin(240))
    return RIDES[kind], [_step("warmup", "Warm-up", warm * 60, "easy"), work,
                         _step("cooldown", "Cool-down", cool * 60, "easy")]


# ---- Swims

SWIMS = {"easy": "Endurance swim", "technique": "Technique swim", "threshold": "Threshold swim", "speed": "Speed swim",
         "race_pace": "Race-pace swim", "opener": "Opener swim"}
SWIM_PACE = 130  # s per 100 m at a steady swim, without swims to go by
# How long 100 m takes at each effort, against a steady swim
EFFORT = {"easy": 1.08, "steady": 1.0, "drill": 1.25, "hard": 0.94, "fast": 0.88}


def _swim(label, m, effort, pace, note=None, intensity=None):
    """A swim step of `m` metres; its seconds are what the distance takes at the effort."""
    step = _step("interval", label, m / 100 * pace * EFFORT[effort],
                 intensity or {"easy": "easy", "steady": "easy", "drill": "easy"}.get(effort, "high"), None, note)
    return {**step, "m": m}


def _rest(s):
    return _step("recovery", "Rest", s, "rest", None, "On the wall")


def swim(kind, metres, pace):
    """(title, steps) of a swim about `metres` long; `pace` is the athlete's seconds per 100 m at a steady swim."""
    if kind == "opener":
        return SWIMS["opener"], [_swim("Warm-up", 400, "easy", pace), _repeat(4, _swim("Fast", 50, "fast", pace), _rest(20)),
                                 _repeat(4, _swim("Race effort", 100, "hard", pace), _rest(15)), _swim("Cool-down", 300, "easy", pace)]
    warm = 300 if metres >= 1500 else 200
    main = max(400, int(round((metres - warm - 200) / 100)) * 100)
    out = [_swim("Warm-up", warm, "easy", pace, "Easy, any stroke")]
    if kind == "technique":
        n = max(2, (main - 400) // 100)
        out += [_repeat(8, _swim("Drill", 50, "drill", pace, "Catch-up, single-arm or fingertip drag"), _rest(15)),
                _repeat(n, _swim("Swim", 100, "steady", pace, "Long, smooth strokes"), _rest(15))]
    elif kind == "threshold":
        n = _clamp(round(main * 0.7 / 100), 6, 15)
        out.append(_repeat(n, _swim("Threshold", 100, "hard", pace, "A pace you could hold for 20–30 min"), _rest(15)))
        if main - n * 100 >= 100:
            out.append(_swim("Easy", main - n * 100, "easy", pace))
    elif kind == "speed":
        if main > 600:
            out.append(_swim("Steady", main - 600, "steady", pace))
        out.append(_repeat(12, _swim("Fast", 50, "fast", pace, "Fast, holding your stroke"), _rest(30)))
    elif kind == "race_pace":
        rep = 500 if main >= 1500 else 300
        out.append(_repeat(_clamp(round(main / rep), 2, 4), _swim("Race effort", rep, "hard", pace, "The effort you'd race at"),
                           _rest(30)))
    else:  # endurance
        rep = 400 if main >= 1200 else 200
        out.append(_repeat(max(1, round(main / rep)), _swim("Steady", rep, "steady", pace, "Steady, long strokes"), _rest(20)))
    return SWIMS[kind], out + [_swim("Cool-down", 200, "easy", pace)]


def swim_metres(steps):
    """The metres a swim covers."""
    return sum(s["n"] * swim_metres(s["steps"]) if s["kind"] == "repeat" else s.get("m") or 0 for s in steps)


# ---- A triathlon's run off the bike

BRICK_MIN = {"sprint": 15, "olympic": 20, "half": 25, "full": 30}


def brick(tri, t):
    """(title, steps) of a brick run: straight off the bike, the first 10 minutes at the race's run pace."""
    total = BRICK_MIN[tri]
    return "Brick run", [_step("interval", "Off the bike", 600, "high", t.race() or t.run("steady", 3)),
                         _step("interval", "Easy", (total - 10) * 60, "easy", t.run("easy", 2))]


def ride_minutes(hours):
    return _round5(hours * 60)
