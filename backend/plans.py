"""A training plan: weeks of runs, and rides and swims if picked, on the days the athlete picked,
toward a goal (a race, a triathlon, general fitness, a bigger base, a speed block, a return to running or holding
fitness), with the time it projects. Paces follow how the runs go: a run better than planned makes the next paces (and
the projection) faster, a worse one slower, and a run on plan changes nothing. Rides and swims are done or missed;
rides follow Garmin's FTP as it changes.

Everything is worked out again from the stored setup and the synced runs on every build, so nothing else is stored."""
import hashlib
import json
import math
from datetime import date, timedelta

import changes as chat_changes
import multisport
import workouts
from workouts import LT_FROM_5K, VARIANTS, _clamp, _flat, _repeat, _round5, _run_steps, _step, _variant_steps, km_of

GOALS = {"race": "Race", "triathlon": "Triathlon", "fitness": "General fitness", "base": "Base builder",
         "speed": "Speed block", "comeback": "Return to running", "maintain": "Maintenance"}
WEEK_RANGE = {"race": (2, 52), "triathlon": (6, 52), "fitness": (2, 52), "base": (4, 52), "speed": (6, 12),
              "comeback": (4, 16), "maintain": (2, 52)}  # how long each plan type can be, in weeks
PROJECTS = {"race", "fitness", "speed", "maintain"}  # the plan types that project a time over their distance
RACES = {"race", "triathlon"}  # the plan types that end in a race day, with a taper before it
# The sports each plan type needs; the rest of its sports are up to the athlete
NEEDS = {"race": {"run"}, "speed": {"run"}, "comeback": {"run"}, "triathlon": {"swim", "bike", "run"}}
# Sports whose sessions follow only the plan's phases, with no hard sessions: rides and swims next to a running goal
SUPPORT_GOALS = {"race", "speed", "comeback"}
SPORT_ORDER = ("swim", "bike", "run", "tri")  # a day's sessions in this order: a brick run after the ride

# Triathlon distances: the name, the run leg (which sets the taper and long run caps), weekly run km and the longest
# long run it builds toward, the run's race pace as a share of threshold speed, the weekly bike hours and longest ride,
# and the weekly swim km
TRI = {"sprint": "Sprint", "olympic": "Olympic", "half": "70.3", "full": "Ironman"}
TRI_RUN_M = {"sprint": 5000, "olympic": 10000, "half": 21098, "full": 42195}
TRI_RUN_KM = {"sprint": 30, "olympic": 35, "half": 45, "full": 55}
TRI_LONG_KM = {"sprint": 12, "olympic": 16, "half": 19, "full": 28}
TRI_RUN_SPEED = {"sprint": 0.98, "olympic": 0.94, "half": 0.88, "full": 0.84}
TRI_BIKE_H = {"sprint": 5, "olympic": 6, "half": 8, "full": 11}
TRI_LONG_H = {"sprint": 1.5, "olympic": 2, "half": 3.5, "full": 5}
TRI_SWIM_KM = {"sprint": 6, "olympic": 8, "half": 10, "full": 12}
# The same for the other plan types that build: (weekly bike hours, longest ride, weekly swim km)
BUILD_OTHER = {"base": (10, 3, 12), "fitness": (8, 2.5, 10)}
PHASES = {"base": "Base", "build": "Build", "peak": "Peak", "taper": "Taper", "steady": "Steady"}
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

WEEKLY_GAIN = 0.005  # planned threshold speed gain per training week
MAX_GAIN = 0.08  # in all, over the plan
TAPER_GAIN = 0.015  # freshness on race day after the taper
RAMP = 1.08  # weekly volume grows at most this much
DELOAD = 0.75  # every 4th week
TAPER_VOLUME = (0.75, 0.6, 0.45)  # of peak volume, the last taper week (race week) last
EASY_MIN_KM = 4
MAX_GROWTH = 2.5  # the weekly volume never goes past this times the starting volume
LONG_STEP = (0.5, 1.5)  # km the long run grows in a normal week, at least and at most
LONG_SHARE = 0.5  # of the week's km, at most
LONG_DOWN = {False: (0.7,), True: (0.75, 0.55)}  # the long run after its peak, as a share of it, for races up to 10 km and longer
TOUCH_REPS = 3  # race week's race-pace session: this many km at race pace

# Adaptation: each run moves the fitness factor this share of the way toward what it showed (half for easy runs,
# whose heart rate says less), unless within the deadband ("on plan"); the factor moves at most WEEK_CAP a week and
# stays within FACTOR_LIMITS
SMOOTHING = 0.3
DEADBAND = 0.01
WEEK_CAP = 0.015
FACTOR_LIMITS = (0.85, 1.15)
WORK_LAP_SHARE = 0.9  # a work rep is a lap at least this share of the work target's slow speed,
WORK_LAP_LENGTH = (0.8, 1.2)  # and about as long as the work step (laps as the watch records the plan's workout)
HR_MODEL_DAYS = 60  # easy runs this long before the plan set the expected pace at each heart rate
HARD_KINDS = ("threshold", "vo2", "anaerobic", "race_pace")
KEY_KINDS = HARD_KINDS + ("long",)
TITLES = {**workouts.TITLES, "race_pace": "Race pace intervals", "race": "Race"}
# The kinds of long run, and what they're called
LONG_VARIANTS = {"long": "Long run", "progressive": "Progressive long run", "fast_finish": "Fast-finish long run",
                 "surges": "Long run with surges", "race_finish": "Race-pace finish long run",
                 "mp_blocks": "Marathon-pace long run"}


def taper_weeks(metres):
    return 1 if metres <= 10000 else 2 if metres <= 21098 else 3


def taper_count(cfg):
    """Taper weeks: by distance for a race (a triathlon by its run leg), one for a speed block, none for the other plan
    types."""
    return taper_weeks(cfg["metres"]) if cfg["goal"] in RACES else 1 if cfg["goal"] == "speed" else 0


def ceiling_km(cfg):
    """The weekly km a plan builds toward: by race distance (a fitness plan as for 10 km), never past MAX_GROWTH times
    the start; a base builder to twice the start (60 km at most); a speed block holds about where it starts, and
    maintenance and a return to running exactly."""
    goal, m, start = cfg["goal"], cfg["metres"], cfg["start_km"]
    if goal == "triathlon":
        return max(start, min(TRI_RUN_KM[cfg["tri"]], start * MAX_GROWTH))
    if goal in ("race", "fitness"):
        top = 45 if goal == "fitness" else 35 if m <= 5000 else 45 if m <= 10000 else 50 if m <= 21098 else 70
        return max(start, min(top, start * MAX_GROWTH))
    if goal == "base":
        return max(start, min(60, start * 2))
    return start * 1.1 if goal == "speed" else start


def long_peak_km(goal, metres, tri=None):
    """The longest long run, by race distance; 0 for the plan types whose long run holds steady."""
    if goal == "triathlon":
        return TRI_LONG_KM[tri]
    if goal in ("fitness", "base"):
        return 16 if goal == "fitness" else 18
    if goal != "race":
        return 0
    return 12 if metres <= 5000 else 16 if metres <= 10000 else 19 if metres <= 21098 else 32


def long_cap_min(metres):
    return 110 if metres <= 10000 else 150 if metres <= 21098 else 210


def monday(d):
    return d - timedelta(days=d.weekday())


def race_time(lt_speed, metres):
    """Seconds over the distance from threshold speed: 5 km speed from the threshold, then Riegel, as the race goal
    scales Garmin's predictions."""
    t5k = 5000 / (lt_speed / LT_FROM_5K)
    return t5k * (metres / 5000) ** 1.06


def time_range(s, weeks_left, metres):
    """(low, high) around a projected time: wider the further off race day is, rounded to whole minutes for the half
    and longer, 10 s below."""
    spread = 0.01 + 0.0015 * max(0, weeks_left)
    step = 60 if metres > 10000 else 10
    return step * math.floor(s * (1 - spread) / step), step * math.ceil(s * (1 + spread) / step)


# ---- The plan's weeks: phases, volume and sessions

def layout(cfg):
    """[(week start, phase, deload)] from the plan's first Monday to the week of its end date."""
    first, last = monday(date.fromisoformat(cfg["start"])), monday(date.fromisoformat(cfg["date"]))
    n = (last - first).days // 7 + 1
    goal = cfg["goal"]
    taper = min(taper_count(cfg), max(0, n - 2))
    train = n - taper
    if goal in RACES:
        base, build = max(1, round(train * 0.4)), max(0, round(train * 0.35))
    elif goal == "speed":  # no base: build, then peak
        base, build = 0, max(1, round(train * 0.5))
    else:
        base = max(1, round(train * 0.5))
        build = train - base
    out = []
    for i in range(n):
        phase = "base" if i < base else "build" if i < base + build else "peak" if i < train else "taper"
        phase = {"maintain": "steady", "comeback": "base"}.get(goal, phase)
        # Every 4th week is lighter, up to the long run's peak week and the last before the taper (or the plan's end);
        # a return to running builds steadily without
        deload = (phase != "taper" and goal != "comeback" and (i + 1) % 4 == 0
                  and i < min(train - 1, long_peak_week(cfg, n)))
        out.append((first + timedelta(weeks=i), phase, deload))
    return out


def volumes(cfg, weeks):
    """Planned km per week: up RAMP a week from the starting volume until the taper, stopping at the distance's
    ceiling (or MAX_GROWTH times the start, if lower); less in deload weeks (which don't count as a step up) and taper
    weeks, which take a share of the highest week."""
    return ramp(cfg["start_km"], ceiling_km(cfg), weeks)


def ramp(start, ceiling, weeks):
    """A weekly volume (km or hours) from `start` toward `ceiling`, as volumes() builds the run's."""
    out, level, tapers = [], start, [w for w in weeks if w[1] == "taper"]
    for i, (_, phase, deload) in enumerate(weeks):
        if phase == "taper":
            out.append(level * TAPER_VOLUME[len(TAPER_VOLUME) - len(tapers) + tapers.index(weeks[i])])
        elif deload:
            out.append(level * DELOAD)
        else:
            level = min(ceiling, level * RAMP) if i else level
            out.append(level)
    return out


def long_peak_week(cfg, n):
    """The week (index) of the longest long run: 3 weeks before race week for the half and longer, 2 for shorter races,
    the last week for a fitness plan."""
    if cfg["goal"] not in RACES:
        return n - 1
    return max(0, n - 1 - len(LONG_DOWN[cfg["metres"] > 10000]) - 1)


def long_runs(cfg, weeks, kms):
    """The long run's km each week: from 30% of the starting week, up a steady step each normal week so it reaches the
    distance's longest in its peak week (long_peak_week); never more than LONG_SHARE of its week. Deload weeks take
    DELOAD of it without stepping; after the peak it steps down by LONG_DOWN of the longest, to race week."""
    race = cfg["goal"] in RACES
    first = max(6 if race else 5, cfg["start_km"] * 0.3)
    top = max(first, long_peak_km(cfg["goal"], cfg["metres"], cfg.get("tri")))
    peak = long_peak_week(cfg, len(weeks))
    down = LONG_DOWN[cfg["metres"] > 10000]
    steps = sum(1 for i, w in enumerate(weeks) if 0 < i <= peak and not w[2])
    step = min(LONG_STEP[1], max(LONG_STEP[0], (top - first) / steps)) if steps and top > first else 0
    out, level, longest = [], first, first
    for i, (w, km) in enumerate(zip(weeks, kms)):
        if i > peak:
            out.append(max(first, longest * down[min(i - peak, len(down)) - 1]))
            continue
        if i and not w[2]:
            level = min(top, level + step)
        long_km = max(first, min(level * (DELOAD if w[2] else 1), km * LONG_SHARE))
        longest = max(longest, long_km)
        out.append(long_km)
    return out


def _circular(a, b):
    return min((a - b) % 7, (b - a) % 7)


def session_days(days, long_day, hard, avoid=()):
    """{weekday: kind}: the long session on its day (if any), the hard sessions as far from it, from each other and
    from the `avoid` days (other sports' key sessions) as the days allow, easy sessions on the rest."""
    plan = {} if long_day is None else {long_day: "long"}
    free = sorted(d for d in days if d != long_day)
    for kind in hard:
        if not free:
            break
        best = max(free, key=lambda d: (min((_circular(d, x) for x in [*plan, *avoid]), default=7), -d))
        plan[best] = kind
        free.remove(best)
    for d in free:
        plan[d] = "easy"
    return plan


def hard_kinds(cfg, phase, n_days, week_index):
    """The week's hard sessions, first the one that stays when there's room for only one.

    Race and general fitness: one in the base phase and the taper or with three days or fewer, two otherwise. Speed
    (anaerobic) sessions come only where the goal calls for speed, never in the base phase or the taper: every 3rd
    build or peak week toward a 5 or 10 km race (VO2 max the others), every 6th build week toward a half or marathon,
    every 4th build week for general fitness. A base builder has one threshold session a week, a speed block two (VO2
    max, then speed or threshold), maintenance one (threshold, speed, threshold, VO2 max in turn), and a return to
    running none."""
    goal, i = cfg["goal"], week_index
    if goal == "comeback":
        return []
    if goal == "triathlon":  # one, as the rides and swims have theirs: race pace in the peak of a 70.3 or Ironman
        if phase == "peak" and cfg["metres"] > 10000:
            return ["race_pace"]
        return ["vo2" if phase == "build" and i % 3 == 2 else "threshold"]
    if goal == "maintain":
        return [("threshold", "anaerobic", "threshold", "vo2")[i % 4]]
    if goal == "base":
        return ["threshold"]
    if goal == "speed":
        kinds = ["vo2"] if phase == "taper" else ["vo2", "anaerobic" if i % 2 == 0 else "threshold"]
        return kinds if n_days >= 3 else kinds[:1]
    short = goal == "race" and cfg["metres"] <= 10000
    # Which weeks get speed in place of VO2 max: away from the lighter 4th weeks
    speed_week = i % 3 == 2 if short else i % 6 == 4 if goal == "race" else i % 4 == 1
    if phase in ("base", "taper"):
        kinds = ["threshold"]
    elif phase == "build":
        second = "anaerobic" if speed_week else "vo2"
        kinds = ["threshold", second] if i % 2 == 0 else [second, "threshold"]
    else:  # peak: race pace for the half and longer
        kinds = (["race_pace", "threshold"] if goal == "race" and not short
                 else ["anaerobic" if short and speed_week else "vo2", "threshold"])
    return kinds[:1] if phase in ("base", "taper") or n_days <= 3 else kinds[:2]


class Paces(workouts.Targets):
    """The workout targets from the plan's threshold speed for the week, and its race pace."""

    def __init__(self, profile, lt, race_speed):
        super().__init__({**profile, "lt_speed": lt}, None)
        self.race_speed = race_speed

    def race(self):
        if not self.race_speed:
            return None
        return {"type": "pace", "slow": round(1000 / (self.race_speed * 0.985)), "fast": round(1000 / (self.race_speed * 1.015))}


def _race_pace_steps(total, t):
    n = max(2, min(5, round((total - 25) / 12)))
    return [_step("warmup", "Warm-up", 900, "easy"),
            _repeat(n, _step("interval", "Race pace", 600, "high", t.race() or t.hr(4)),
                    _step("recovery", "Easy jog", 120, "rest")),
            _step("cooldown", "Cool-down", 600, "easy")]


def _touch_steps(t):
    """Race week: a little race pace to stay sharp, 15 min easy then TOUCH_REPS × 1 km with jogs."""
    rep = round(1000 / t.race_speed) if t.race_speed else 300
    return [_step("warmup", "Warm-up", 900, "easy"),
            _repeat(TOUCH_REPS, _step("interval", "Race pace", rep, "high", t.race() or t.hr(4)),
                    _step("recovery", "Easy jog", 120, "rest")),
            _step("cooldown", "Cool-down", 300, "easy")]


def variant(kind, phase, week, light):
    """Which named session a hard kind is this week: threshold sessions are tempo and progression runs in the base
    phase, cruise intervals and over/unders after; VO2 max and speed sessions rotate. Lighter and taper weeks keep to
    the plainest."""
    if kind == "threshold":
        return "cruise" if light else (("tempo", "progression") if phase == "base" else ("cruise", "over_under"))[week % 2]
    if kind == "vo2":
        return "3min" if light else ("1k", "800", "3min", "pyramid")[week % 4]
    return ("400", "600", "400_sets")[week % 3]


def run_walk(day, kind, progress, t):
    """A return-to-running session: run/walk, from 2 min run and 1 min walk to 10 min run by 80% of the plan, then
    running all the way. Easy days grow from 20 to 30 min, the long day from 25 to 40."""
    total = round((25 + 15 * progress) if kind == "long" else (20 + 10 * progress))
    long = kind == "long"
    if progress >= 0.8:
        steps = _run_steps("long" if long else "easy", total, t)
        title = "Long run" if long else "Easy run"
    else:
        run = round(2 + 8 * progress / 0.8)
        steps = [_repeat(max(2, round(total / (run + 1))), _step("interval", "Run", run * 60, "easy", t.run("easy", 2)),
                         _step("recovery", "Walk", 60, "rest"))]
        title = "Long run/walk" if long else "Run/walk"
    easy = t.pace("easy")
    run_s = sum(s["s"] for s in _flat(steps) if s["intensity"] == "easy")
    walk_s = sum(s["s"] for s in _flat(steps) if s["intensity"] == "rest")
    km = round(run_s / ((easy["slow"] + easy["fast"]) / 2) + walk_s / 600, 1) if easy else None  # walking 10 min/km
    return {"date": day.isoformat(), "kind": kind, "title": title, "steps": steps,
            "total_s": run_s + walk_s, "km": km}


def long_variant(cfg, week):
    """Which long run it is in the plan's week (index): all easy in the base phase, lighter weeks and the taper;
    race-specific in the peak weeks of a half (race pace finish) or marathon (marathon pace blocks); otherwise a
    rotation through the build and peak weeks, with surges for the shorter goals and more plain long runs for the
    longer ones."""
    goal, m = cfg["goal"], cfg["metres"]
    weeks = layout(cfg)
    _, phase, deload = weeks[week]
    if deload or phase in ("base", "taper") or goal == "comeback":
        return "long"
    if phase == "peak" and goal in RACES and m > 10000:
        return "mp_blocks" if m > 21098 else "race_finish"
    short = goal in ("fitness", "speed") or (goal in RACES and m <= 10000)
    n = sum(1 for _, p, d in weeks[:week] if p not in ("base", "taper") and not d)
    return (("long", "surges", "progressive", "fast_finish") if short
            else ("long", "progressive", "long", "fast_finish"))[n % 4]


def _long_variant_steps(v, total, t):
    """A long run's steps, `total` minutes in all."""
    easy = lambda m, label="Long run": _step("interval", label, m * 60, "easy", t.run("easy", 2))
    if v == "progressive":
        steady, fast = _round5(total * 0.25), _round5(total * 0.15)
        return [easy(total - steady - fast), _step("interval", "Steady", steady * 60, "high", t.run("steady", 3)),
                _step("interval", "Tempo", fast * 60, "high", t.run("tempo", 3))]
    if v == "fast_finish":
        return [easy(total - 15), _step("interval", "Fast finish", 15 * 60, "high", t.run("tempo", 3))]
    if v == "surges" and total >= 45:
        n = _clamp(round(total / 12), 6, 8)
        first = total - n * 3 - 10
        return [easy(first), _repeat(n, _step("interval", "Surge", 60, "high", t.run("vo2", 5)),
                                     _step("recovery", "Easy", 120, "easy", t.run("easy", 2))), easy(10, "Easy")]
    if v == "race_finish":
        finish = _round5(total * 0.2)
        return [easy(total - finish), _step("interval", "Race pace finish", finish * 60, "high", t.race() or t.hr(4))]
    if v == "mp_blocks":
        n = 3 if total >= 140 else 2 if total >= 80 else 1
        rest = total - 30 - n * 25
        steps = [easy(30), _repeat(n, _step("interval", "Marathon pace", 20 * 60, "high", t.race() or t.hr(3)),
                                   _step("recovery", "Easy", 5 * 60, "easy", t.run("easy", 2)))]
        return steps + ([easy(rest, "Easy")] if rest > 0 else [])
    return _run_steps("long", total, t)


def make_session(kind, day, cfg, phase, deload, t, km=None, touch=False, week=0):
    """One session: its steps, length and distance. `km` sets the length of easy and long runs; `touch` makes a race
    pace session race week's short one; `week` picks the named variant of a hard session."""
    easy = t.pace("easy")
    easy_pace = (easy["slow"] + easy["fast"]) / 120 if easy else 6.5  # min/km
    if kind == "race" and cfg["goal"] == "triathlon":
        return {"date": day.isoformat(), "sport": "tri", "kind": "race", "title": cfg["name"], "steps": [],
                "total_s": None, "km": None}
    if kind == "race":
        s = round(cfg["metres"] / t.race_speed) if t.race_speed else None
        return {"date": day.isoformat(), "kind": "race", "title": cfg["name"], "steps": [], "total_s": s,
                "km": round(cfg["metres"] / 1000, 1)}
    if kind in ("easy", "long"):
        cap = long_cap_min(cfg["metres"]) if kind == "long" else 90
        total = _round5(min(km * easy_pace, cap))
        lv = long_variant(cfg, week) if kind == "long" else None
        steps = _long_variant_steps(lv, total, t) if kind == "long" else _run_steps("easy", total, t)
    elif kind == "shakeout":
        steps = _run_steps("shakeout", 25, t)
    else:
        light = deload or phase == "taper"
        total = 40 if light else {"base": 45, "build": 50, "peak": 55, "steady": 50}[phase]
        if kind == "race_pace":
            steps = _touch_steps(t) if touch else _race_pace_steps(total, t)
        else:
            v = variant(kind, phase, week, light)
            steps = _variant_steps(v, total, t)
    if touch:
        title = "Race pace touch"
    elif kind in HARD_KINDS and kind != "race_pace":
        title = VARIANTS[v]
    elif kind == "long":
        title = "Race-pace long run" if lv == "mp_blocks" and cfg["goal"] == "triathlon" else LONG_VARIANTS[lv]
    else:
        title = f"{TITLES[kind]} run" if kind in ("easy", "long", "shakeout") else TITLES[kind]
    flat = list(_flat(steps))
    return {"date": day.isoformat(), "kind": kind, "title": title, "steps": steps,
            "total_s": sum(s["s"] for s in flat), "km": km_of(steps, t) or (round(km, 1) if km else None)}


def race_week(cfg):
    """{date: kind} for the plan days in the 5 days before a race: the one closest to 4 days out (3 at the least) is
    a touch of race pace, the last one a shakeout, and the rest rest days (None)."""
    end = date.fromisoformat(cfg["date"])
    before = [end - timedelta(days=k) for k in range(5, 0, -1) if (end - timedelta(days=k)).weekday() in cfg["days"]]
    roles = dict.fromkeys(before)
    far = [d for d in before if (end - d).days >= 3]
    touch = min(far, key=lambda d: (abs((end - d).days - 4), -(end - d).days)) if far else None
    if touch:
        roles[touch] = "race_pace"
    shake = next((d for d in reversed(before) if d != touch), None)
    if shake:
        roles[shake] = "shakeout"
    return roles


def week_sessions(cfg, i, week, km, t, first_day, long_km, avoid=()):
    """The week's runs, in date order. Days before the plan started and after its end are left out. The 5 days
    before a race hold only a race pace touch and a shakeout (race_week), and race day is the race. When the long run,
    the hard sessions and the shortest easy runs come to more than the week's km, the second hard session is easy.
    The hard sessions keep away from the `avoid` days too (the long ride)."""
    return [{"sport": "run", **s} for s in _week_runs(cfg, i, week, km, t, first_day, long_km, avoid)]


def _week_runs(cfg, i, week, km, t, first_day, long_km, avoid):
    start, phase, deload = week
    end = date.fromisoformat(cfg["date"])
    days = session_days(cfg["days"], cfg["long_day"], hard_kinds(cfg, phase, len(cfg["days"]), i), avoid)
    race = cfg["goal"] in RACES
    roles = race_week(cfg) if race else {}
    out = []
    for wd in sorted(days):
        day = start + timedelta(days=wd)
        if day < first_day or day > end:
            continue
        kind = days[wd]
        if race and day == end:
            kind = "race"
        elif day in roles:
            if roles[day] is None:
                continue
            kind = roles[day]
        out.append([day, kind])
    if race and start <= end < start + timedelta(days=7) and all(d != end for d, _ in out):
        out.append([end, "race"])
        out.sort()

    if cfg["goal"] == "comeback":
        n = len(layout(cfg))
        return [run_walk(day, kind, i / max(1, n - 1), t) for day, kind in out]

    def hard(day, kind):
        return make_session(kind, day, cfg, phase, deload, t, touch=roles.get(day) == "race_pace", week=i)

    hard_runs = {d: hard(d, k) for d, k in out if k in HARD_KINDS}
    n_easy = sum(1 for _, k in out if k == "easy")
    # Room for the long run: two hard sessions only when they leave the easy runs their shortest within the week's km
    # The week's first hard kind stays; the second goes
    kinds = hard_kinds(cfg, phase, len(cfg["days"]), i)
    hard_order = sorted(hard_runs, key=lambda d: kinds.index(hard_runs[d]["kind"]) if hard_runs[d]["kind"] in kinds else len(kinds))
    if len(hard_order) > 1 and long_km + sum(s["km"] or 0 for s in hard_runs.values()) + n_easy * EASY_MIN_KM > km * 1.05:
        second = hard_order[1]
        del hard_runs[second]
        next(x for x in out if x[0] == second)[1] = "easy"
        n_easy += 1
    hard_km = sum(s["km"] or 0 for s in hard_runs.values())
    # An easy run is never longer than 80% of the long run, even when the week then comes out shorter
    easy_km = max(EASY_MIN_KM, min(long_km * 0.8, (km - long_km - hard_km) / n_easy)) if n_easy else 0
    return [hard_runs[day] if day in hard_runs else make_session(kind, day, cfg, phase, deload, t,
                                                                  long_km if kind == "long" else easy_km, week=i)
            for day, kind in out]


# ---- The other sports' weeks: rides and swims

def _other(day, sport, kind, title, steps, **extra):
    return {"date": day.isoformat(), "sport": sport, "kind": kind, "title": title, "steps": steps,
            "total_s": sum(s["s"] for s in _flat(steps)), "km": None, **extra}


def _in_plan(cfg, day, first_day):
    """Whether a day of the week is within the plan; a race day holds only the race."""
    end = date.fromisoformat(cfg["date"])
    return first_day <= day <= end and not (cfg["goal"] in RACES and day == end)


def _build_other(cfg):
    """(weekly bike hours, longest ride, weekly swim km) the plan builds toward, or None where it holds: maintenance,
    and next to a running goal."""
    if cfg["goal"] == "triathlon":
        return TRI_BIKE_H[cfg["tri"]], TRI_LONG_H[cfg["tri"]], TRI_SWIM_KM[cfg["tri"]]
    return BUILD_OTHER.get(cfg["goal"])


def bike_hours(cfg, weeks):
    """Planned riding hours per week: up to the goal's hours (MAX_GROWTH times the start at most, twice for a base
    builder), ramping as the runs do; held where it starts otherwise."""
    start, top = cfg["sports"]["bike"]["start_h"], _build_other(cfg)
    growth = 2 if cfg["goal"] == "base" else MAX_GROWTH
    return ramp(start, max(start, min(top[0], start * growth)) if top else start, weeks)


def swim_km(cfg, weeks):
    """Planned swim km per week, as bike_hours()."""
    start, top = cfg["sports"]["swim"]["start_km"], _build_other(cfg)
    growth = 2 if cfg["goal"] == "base" else MAX_GROWTH
    return ramp(start, max(start, min(top[2], start * growth)) if top else start, weeks)


def long_rides(cfg, weeks, hours):
    """The long ride's hours each week, as long_runs() does the long run's: from 40% of the starting week (an hour at
    least), up a steady step each normal week to the goal's longest ride by the long run's peak week, at most half the
    week's riding; lighter in deload weeks, stepping down after the peak. Where the riding holds, so does the long
    ride."""
    first = max(1.0, cfg["sports"]["bike"]["start_h"] * 0.4)
    top = max(first, _build_other(cfg)[1] if _build_other(cfg) else first)
    peak = long_peak_week(cfg, len(weeks))
    down = LONG_DOWN[cfg["metres"] > 10000]
    steps = sum(1 for i, w in enumerate(weeks) if 0 < i <= peak and not w[2])
    step = (top - first) / steps if steps else 0
    out, level, longest = [], first, first
    for i, (w, h) in enumerate(zip(weeks, hours)):
        if i > peak:
            out.append(max(first, longest * down[min(i - peak, len(down)) - 1]))
            continue
        if i and not w[2]:
            level = min(top, level + step)
        long_h = max(first, min(level * (DELOAD if w[2] else 1), h * 0.5))
        longest = max(longest, long_h)
        out.append(long_h)
    return out


def bike_hard(cfg, phase, n_days, week):
    """The week's hard rides: none next to a running goal or with one ride a week; otherwise one (sweet spot in the
    base phase, threshold and VO2 max in turn after, rotating in maintenance), two in the build and peak of a plan of
    rides only with 4 ride days or more."""
    if cfg["goal"] in SUPPORT_GOALS or n_days < 2:
        return []
    if cfg["goal"] == "maintain":
        kinds = [("sweet_spot", "threshold", "vo2")[week % 3]]
    elif phase == "base":
        kinds = ["sweet_spot"]
    elif phase == "build":
        kinds = ["threshold", "vo2"] if week % 2 == 0 else ["vo2", "threshold"]
    else:
        kinds = ["threshold", "vo2"]
    alone = not {"run", "swim"} & set(cfg["sports"])
    return kinds[:2] if alone and n_days >= 4 and phase in ("build", "peak") else kinds[:1]


def bike_week(cfg, i, week, hours, long_h, t, first_day, avoid):
    """The week's rides: the long ride on its day, the hard rides away from the other key sessions, endurance rides on
    the rest, together about the week's hours. A 70.3 or Ironman's long rides in the peak weeks are at race effort."""
    start, phase, deload = week
    b = cfg["sports"]["bike"]
    days = session_days(b["days"], b.get("long_day"), bike_hard(cfg, phase, len(b["days"]), i), avoid)
    hard_min = 60 if deload or phase in ("base", "taper") else 75
    long_min = multisport.ride_minutes(long_h) if b.get("long_day") is not None else 0
    n_hard = sum(k not in ("easy", "long") for k in days.values())
    n_easy = sum(k == "easy" for k in days.values())
    easy_min = _round5(_clamp((hours * 60 - long_min - n_hard * hard_min) / n_easy, 45,
                              max(60, long_min * 0.8) if long_min else 180)) if n_easy else 0
    race = cfg["tri"] if (cfg["goal"] == "triathlon" and cfg["tri"] in ("half", "full") and phase == "peak"
                          and not deload) else None
    out = []
    for wd, kind in sorted(days.items()):
        day = start + timedelta(days=wd)
        if _in_plan(cfg, day, first_day):
            minutes = long_min if kind == "long" else easy_min if kind == "easy" else hard_min
            title, steps = multisport.ride(kind, minutes, t, i, race if kind == "long" else None)
            out.append(_other(day, "bike", kind, title, steps))
    return out


def swim_kinds(cfg, phase, deload, n_days):
    """The week's swims other than endurance swims: next to a running goal a technique swim in the base phase only;
    otherwise technique in the base phase and lighter weeks, threshold after, race pace in a triathlon's peak, and a
    speed or second threshold swim with 3 swim days or more. With 2 swim days or more, one is an endurance swim."""
    if cfg["goal"] in SUPPORT_GOALS:
        return ["technique"] if phase == "base" else []
    if deload or phase == "base":
        kinds = ["technique"]
    elif phase == "peak":
        kinds = ["race_pace", "threshold"] if cfg["goal"] == "triathlon" else ["threshold", "speed"]
    elif phase == "taper":
        kinds = ["threshold"]
    else:
        kinds = ["threshold", "speed"]
    return kinds[:max(1, n_days - 1)]


def swim_week(cfg, i, week, km, pace, first_day):
    """The week's swims, as far apart as the days allow, each about an even share of the week's km."""
    start, phase, deload = week
    sw = cfg["sports"]["swim"]
    days = session_days(sw["days"], None, swim_kinds(cfg, phase, deload, len(sw["days"])))
    each = max(800, round(km * 1000 / len(sw["days"]) / 100) * 100)
    out = []
    for wd, kind in sorted(days.items()):
        day = start + timedelta(days=wd)
        if _in_plan(cfg, day, first_day):
            title, steps = multisport.swim(kind, each, pace)
            out.append(_other(day, "swim", kind, title, steps, km=multisport.swim_metres(steps) / 1000))
    return out


def openers(cfg, sessions, t, pace):
    """The 5 days before a race: one short opener ride and swim each, on the day closest to 3 days out (2 at the
    least), and no other rides or swims."""
    if cfg["goal"] not in RACES:
        return sessions
    end = date.fromisoformat(cfg["date"])
    out = lambda s: (end - date.fromisoformat(s["date"])).days
    keep = [s for s in sessions if not (s["sport"] in ("bike", "swim") and 0 < out(s) <= 5)]
    for sport in ("bike", "swim"):
        near = [s for s in sessions if s["sport"] == sport and 2 <= out(s) <= 5]
        if near:
            day = date.fromisoformat(min(near, key=lambda s: (abs(out(s) - 3), out(s)))["date"])
            title, steps = multisport.ride("opener", 40, t) if sport == "bike" else multisport.swim("opener", 1500, pace)
            keep.append(_other(day, sport, "opener", title, steps,
                               km=multisport.swim_metres(steps) / 1000 if sport == "swim" else None))
    return keep


def bricks(cfg, week, sessions, t):
    """A triathlon's build and peak weeks: an easy run on the day of the long ride or a hard ride is a brick run,
    straight off the bike."""
    if cfg["goal"] != "triathlon" or week[1] not in ("build", "peak") or week[2]:
        return sessions
    ride_days = {s["date"] for s in sessions if s["sport"] == "bike" and s["kind"] != "easy"}
    out = []
    for s in sessions:
        if s["sport"] == "run" and s["kind"] == "easy" and s["date"] in ride_days:
            title, steps = multisport.brick(cfg["tri"], t)
            s = {**_other(date.fromisoformat(s["date"]), "run", "brick", title, steps), "km": km_of(steps, t)}
        out.append(s)
    return out


def swim_pace(acts, today):
    """Seconds per 100 m at a steady swim: the median of the last 90 days' swims, or SWIM_PACE without any."""
    since = (today - timedelta(days=90)).isoformat()
    paces = sorted(100 / a["speed"] for a in acts if a["sport"] == "swim" and a["date"] >= since and a.get("speed"))
    return _clamp(paces[len(paces) // 2], 70, 240) if paces else multisport.SWIM_PACE


def plan_week(cfg, i, week, t, first_day, vols, pace):
    """All the week's sessions, every sport, in date order and each day's in SPORT_ORDER. `vols` holds the week's
    volumes: run km and long run km, bike hours and long ride hours, swim km."""
    sports = cfg["sports"]
    long_ride = sports.get("bike", {}).get("long_day")
    out = []
    if "run" in sports:
        out += week_sessions(cfg, i, week, vols["run"], t, first_day, vols["long_run"],
                             avoid=() if long_ride is None else (long_ride,))
    if "bike" in sports:
        key = {date.fromisoformat(s["date"]).weekday() for s in out if s["kind"] not in ("easy", "shakeout", "race")}
        out += bike_week(cfg, i, week, vols["bike"], vols["long_ride"], t, first_day, key)
    if "swim" in sports:
        out += swim_week(cfg, i, week, vols["swim"], pace, first_day)
    out = bricks(cfg, week, openers(cfg, out, t, pace), t)
    return sorted(out, key=lambda s: (s["date"], SPORT_ORDER.index(s["sport"])))


# ---- How the runs went

def hr_model(acts, start, zones):
    """Expected speed (m/s) at an average heart rate, from easy runs in the HR_MODEL_DAYS before the plan: a line
    fitted through them when it slopes up, otherwise speed per beat. None without enough runs."""
    since = (start - timedelta(days=HR_MODEL_DAYS)).isoformat()
    ceiling = zones[3] if zones else None  # zone 4's floor
    pts = [(a["avg_hr"], a["speed"]) for a in acts
           if a["sport"] == "run" and since <= a["date"] < start.isoformat() and a.get("avg_hr") and a.get("speed")
           and (a.get("duration_s") or 0) >= 1200 and (not ceiling or a["avg_hr"] < ceiling)]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx, my = sum(h for h, _ in pts) / n, sum(v for _, v in pts) / n
    var = sum((h - mx) ** 2 for h, _ in pts)
    slope = sum((h - mx) * (v - my) for h, v in pts) / var if var else 0
    if n >= 6 and slope > 0:
        return lambda hr: my + slope * (hr - mx)
    per_beat = sorted(v / h for h, v in pts)[n // 2]
    return lambda hr: per_beat * hr


def _is_work(s):
    return s["kind"] == "interval" and (s["target"] or {}).get("type") == "pace" and s["intensity"] in ("high", "anaerobic")


def _work_step(steps):
    """The first paced work step."""
    return next((s for s in _flat(steps) if _is_work(s)), None)


def pair_laps(steps, laps):
    """[(lap, its step)] for every step, or None when they don't pair. A watch running a sent workout records a lap
    per step, so with as many laps as steps (or one more, for a lap after the end) they pair in order."""
    flat = list(_flat(steps))
    return list(zip(laps, flat)) if laps and len(laps) in (len(flat), len(flat) + 1) else None


def _work_laps(steps, laps):
    """[(lap, its step)] for the work steps. Laps paired with the steps (pair_laps) are each judged against their own
    step's target (over/unders, pyramids). Otherwise the laps about as long as the first work step and near its pace
    count."""
    paired = pair_laps(steps, laps)
    if paired:
        return [(l, s) for l, s in paired if _is_work(s) and l.get("speed") and l.get("duration_s")]
    step = _work_step(steps)
    if not step:
        return []
    lo, hi = step["s"] * WORK_LAP_LENGTH[0], step["s"] * WORK_LAP_LENGTH[1]
    return [(l, step) for l in laps if l.get("speed") and lo <= (l.get("duration_s") or 0) <= hi
            and l["speed"] >= 1000 / step["target"]["slow"] * WORK_LAP_SHARE]


def judge(session, run, laps, model, gain, factor, t):
    """(what the run says the fitness factor is, the run's actual pace for the page), or (None, pace) when the run
    can't say: no heart rate for an easy run, no laps for intervals."""
    pace = round(1000 / run["speed"]) if run.get("speed") else None
    kind = session["kind"]
    if kind in ("easy", "long") and not _work_step(session["steps"]):
        # A run at zone 4 or harder wasn't easy, and the heart rate model only knows easy runs
        if not (model and run.get("avg_hr") and run.get("speed")) or run["avg_hr"] >= t.floors[3]:
            return None, pace
        return run["speed"] / (model(run["avg_hr"]) * gain), pace
    if kind in HARD_KINDS + ("long",):
        work = _work_laps(session["steps"], laps or [])
        if not work:
            return None, pace
        secs = sum(l["duration_s"] for l, _ in work)
        speed = sum(l["speed"] * l["duration_s"] for l, _ in work) / secs
        # Each rep against the middle of its own target, weighted by time
        ratio = sum(l["speed"] * (s["target"]["slow"] + s["target"]["fast"]) / 2000 * l["duration_s"] for l, s in work) / secs
        seen = factor * ratio
        hrs = [l["avg_hr"] for l, _ in work if l.get("avg_hr")]
        ceiling = t.floors[4] if kind in ("threshold", "race_pace") else None  # zone 5's floor
        if ceiling and hrs and sum(hrs) / len(hrs) > ceiling:
            seen = min(seen, factor)  # faster, but only by working harder than the session asks
        return seen, round(1000 / speed)
    return None, pace


def _match(sessions, runs):
    """{session index: run}: the longest run on the session's day, else one a day either side on a day without its
    own session."""
    by_day = {}
    for r in runs:
        by_day.setdefault(r["date"], []).append(r)
    planned = {s["date"] for s in sessions}
    used, out = set(), {}
    for i, s in enumerate(sessions):
        d = date.fromisoformat(s["date"])
        options = by_day.get(s["date"], [])
        if not options:
            for near in (d - timedelta(days=1), d + timedelta(days=1)):
                if near.isoformat() not in planned and monday(near) == monday(d):
                    options = [r for r in by_day.get(near.isoformat(), []) if r["id"] not in used]
                    if options:
                        break
        options = [r for r in options if r["id"] not in used]
        if options:
            r = max(options, key=lambda r: r.get("km") or 0)
            used.add(r["id"])
            out[i] = r
    return out


def activity_sport(a):
    """The plan sport an activity counts for: a triathlon's own activity (Garmin's multisport) for its race day."""
    return "tri" if any(k in (a.get("type") or "") for k in ("multi_sport", "triathlon")) else a["sport"]


def match_all(sessions, acts):
    """{session index: activity}, as _match() pairs runs with runs, for each sport on its own."""
    out = {}
    for sport in {s["sport"] for s in sessions}:
        idx = [i for i, s in enumerate(sessions) if s["sport"] == sport]
        found = _match([sessions[i] for i in idx], [a for a in acts if activity_sport(a) == sport])
        out.update({idx[k]: a for k, a in found.items()})
    return out


def _is_key(s):
    return s["sport"] in ("run", "bike") and s["kind"] in KEY_KINDS + ("sweet_spot",)


def reshuffle(sessions, today):
    """A key session (a hard or long run or ride) missed this week moves to the next plan day of its sport from today
    on, in place of an easy one, which is dropped; a missed easy session is just missed. An activity on that day counts
    for the moved session. Days changed in the chat stay as they are."""
    for s in sessions:
        if s["status"] != "missed" or not _is_key(s) or s.get("changed"):
            continue
        spot = next((x for x in sessions if x["sport"] == s["sport"] and x["kind"] == "easy"
                     and x["date"] >= today.isoformat() and not x.get("moved_from") and not x.get("changed")), None)
        if spot:
            moved = {**s, "date": spot["date"], "status": spot["status"], "run": spot["run"],
                     "moved_from": s["date"]}
            sessions[sessions.index(spot)] = moved
            s["status"] = "moved"
    return sessions


def apply_changes(sessions, changes, t):
    """The week's sessions with the chat's changes: a changed day's session of the sport replaced, removed for a rest
    day, or added on a day without one."""
    days = {(c["date"], c["sport"]): c for c in changes}
    out = [s for s in sessions if (s["date"], s["sport"]) not in days]
    out += [{**x, "sport": c["sport"]} for c in days.values() for x in [chat_changes.session(c, t)] if x]
    return sorted(out, key=lambda s: (s["date"], SPORT_ORDER.index(s["sport"])))


# ---- The whole plan

def fitness_now(profile, pred_5k):
    """Threshold speed (m/s) to start a plan from: Garmin's lactate threshold, or from its 5 km prediction."""
    return workouts.Targets(profile, pred_5k).lt


def normalize(cfg):
    """The plan setup with its sports: a plan saved before plans had other sports is runs only. The run's days, long
    day and starting km are also kept at the top, as the run's sessions are worked out from there."""
    if "sports" not in cfg:
        return {**cfg, "sports": {"run": {"days": cfg["days"], "long_day": cfg["long_day"], "start_km": cfg["start_km"]}}}
    run = cfg["sports"].get("run") or {"days": [], "long_day": None, "start_km": 0}
    return {**cfg, "days": run["days"], "long_day": run["long_day"], "start_km": run["start_km"]}


def multi(cfg):
    """Whether the plan holds more than runs."""
    return set(cfg["sports"]) != {"run"}


def build(cfg, acts, laps_by_id, profile, today, sent=(), changes=()):
    """The plan as the page shows it. `acts` are the activity summaries (any order); `changes` are the chat's, of which
    the ones within the plan, for its sports, count."""
    cfg = normalize(cfg)
    sports = cfg["sports"]
    start = date.fromisoformat(cfg["start"])
    end = date.fromisoformat(cfg["date"])
    weeks = layout(cfg)
    n = len(weeks)
    vols = [{} for _ in weeks]
    if "run" in sports:
        kms = volumes(cfg, weeks)
        for v, km, long_km in zip(vols, kms, long_runs(cfg, weeks, kms)):
            v.update(run=km, long_run=long_km)
    if "bike" in sports:
        hours = bike_hours(cfg, weeks)
        for v, h, long_h in zip(vols, hours, long_rides(cfg, weeks, hours)):
            v.update(bike=h, long_ride=long_h)
    if "swim" in sports:
        for v, km in zip(vols, swim_km(cfg, weeks)):
            v["swim"] = km
    pace = swim_pace(acts, today)
    lt0 = cfg.get("fitness0")
    zones = profile.get("zones") or workouts.Targets(profile, None).floors
    model = hr_model(acts, start, zones)
    done = [a for a in acts if activity_sport(a) in {*sports, "tri"} and start.isoformat() <= a["date"] <= end.isoformat()]
    train_weeks = sum(1 for w in weeks if w[1] != "taper")
    race = cfg["goal"] == "race"
    sent_keys = {(x["date"], x["key"]) for x in sent}
    projects = cfg["goal"] in PROJECTS and "run" in sports

    def gain(i):
        return 1 + min(MAX_GAIN, WEEKLY_GAIN * i)

    def projection(factor):
        if not lt0 or not projects:
            return None
        lt = lt0 * gain(train_weeks) * factor * (1 + TAPER_GAIN if taper_count(cfg) else 1)
        return round(race_time(lt, cfg["metres"]))

    factor, change, out_weeks, this_week = 1.0, None, [], monday(today)
    for i, wk in enumerate(weeks):
        lt = lt0 * gain(i) * factor if lt0 else None
        race_s = projection(factor)
        # A triathlon's run is slower than an open race over its distance: a share of threshold speed
        race_speed = (cfg["metres"] / race_s if race_s and race
                      else lt * TRI_RUN_SPEED[cfg["tri"]] if lt and cfg["goal"] == "triathlon" else None)
        t = Paces(profile, lt, race_speed)
        week_start = wk[0]
        week_end = week_start + timedelta(days=6)
        sessions = plan_week(cfg, i, wk, t, start, vols[i], pace)
        sessions = apply_changes(sessions, [c for c in changes if c["sport"] in sports and max(week_start, start)
                                            <= date.fromisoformat(c["date"]) <= min(week_end, end)], t)
        week_acts = [a for a in done if monday(date.fromisoformat(a["date"])) == week_start]
        week_runs = [a for a in week_acts if a["sport"] == "run"]
        matched = match_all(sessions, week_acts)
        for j, s in enumerate(sessions):
            r = matched.get(j)
            s["status"] = "done" if r else "upcoming" if s["date"] > today.isoformat() else "today" if s["date"] == today.isoformat() else "missed"
            s["run"] = r
        if week_start == this_week:
            sessions = reshuffle(sessions, today)
        # How the done runs went, in date order, moving the factor for the weeks after
        week_factor = factor
        for s in sessions:
            r = s.pop("run", None)
            if s["status"] != "done":
                continue
            if s["sport"] != "run":  # only runs say anything about the threshold pace
                s["actual"] = {"id": r["id"], "km": r.get("km"), "pace": None, "hr": r.get("avg_hr"), "verdict": None,
                               "by_hr": False}
                continue
            seen, run_pace = judge(s, r, laps_by_id.get(r["id"]), model, gain(i), factor, t)
            if cfg["goal"] == "comeback":
                seen = None  # run/walk says nothing about threshold pace
            s["actual"] = {"id": r["id"], "km": r.get("km"), "pace": run_pace, "hr": r.get("avg_hr"),
                           "verdict": None if seen is None else "better" if seen > factor * (1 + DEADBAND)
                           else "worse" if seen < factor * (1 - DEADBAND) else "on plan",
                           # Judged by heart rate (an easy run, or a long run without faster parts), not by pace
                           "by_hr": s["kind"] in ("easy", "long") and not _work_step(s["steps"])}
            # A speed session gets its verdict, but its pace is sprint speed, not threshold: it doesn't move the factor
            if seen is not None and s["kind"] != "anaerobic" and abs(seen - factor) > factor * DEADBAND:
                before = projection(factor)
                factor += SMOOTHING * (0.5 if s["kind"] in ("easy", "long") else 1) * (seen - factor)
                factor = max(week_factor * (1 - WEEK_CAP), min(week_factor * (1 + WEEK_CAP), factor))
                factor = max(FACTOR_LIMITS[0], min(FACTOR_LIMITS[1], factor))
                if before and projection(factor) != before:
                    change = {"date": s["date"], "title": s["title"], "from_s": before}
        for s in sessions:
            s["key"] = session_key(s)
            s["sent"] = (s["date"], s["key"]) in sent_keys
            s["sendable"] = bool(s["steps"]) and s["status"] in ("today", "upcoming")
        by_sport = {}
        for s in sessions:
            if s["kind"] != "race" and s["status"] != "moved":
                by_sport[s["sport"]] = by_sport.get(s["sport"], 0) + (s["total_s"] or 0)
        out_weeks.append({
            "start": week_start.isoformat(), "phase": wk[1], "deload": wk[2],
            "km_planned": round(sum(s["km"] or 0 for s in sessions if s["kind"] != "race" and s["sport"] == "run"), 1),
            "km_done": round(sum(r.get("km") or 0 for r in week_runs), 1),
            # Time for the plans with more than runs: planned per sport, and done in all
            "hours_planned": round(sum(by_sport.values()) / 3600, 1),
            "hours_done": round(sum(a.get("duration_s") or 0 for a in week_acts) / 3600, 1),
            "by_sport": by_sport,
            "sessions": sessions,
        })

    weeks_left = max(0, (end - today).days / 7)
    s = projection(factor)
    proj = None
    if s:
        low, high = time_range(s, weeks_left, cfg["metres"])
        # The last run that moved the projection, and what it was before
        proj = {"s": s, "low": low, "high": high, "change": change}
    plan = {
        "config": cfg, "goal_label": GOALS[cfg["goal"]], "weeks": out_weeks, "factor": round(factor, 4),
        "week_index": next((i for i, w in enumerate(weeks) if w[0] == this_week), None),
        "weeks_left": math.ceil(weeks_left), "projection": proj, "projects": projects,
        "phases": PHASES, "over": today > end, "multi": multi(cfg),
        "sports": {k: workouts.ALL_SPORTS[k] for k in SPORT_ORDER if k in sports},
        "tri_label": TRI.get(cfg.get("tri")),
    }
    coming = coming_week(plan, today)
    plan["send"] = {"key": week_key(coming), "count": len(coming), "sent": bool(coming) and all(s["sent"] for s in coming),
                    # Days from today on with a plan workout on Garmin (one sent before its paces changed counts too)
                    "on_watch": len({x["date"] for x in sent if x["date"] >= today.isoformat()})}
    return plan


def session_key(s):
    """Changes whenever what would be sent to the watch changes. A run's leaves its sport out, as before plans had
    other sports, so runs already sent keep their key."""
    what = [s["date"], s["title"], s["steps"]] + ([s["sport"]] if s["sport"] != "run" else [])
    return hashlib.sha256(json.dumps(what, sort_keys=True).encode()).hexdigest()[:16]


def coming_week(plan, today):
    """The sessions in the next 7 days, today first, that can go to the watch."""
    until = (today + timedelta(days=6)).isoformat()
    return [s for w in plan["weeks"] for s in w["sessions"] if today.isoformat() <= s["date"] <= until and s["sendable"]]


def week_key(sessions):
    return hashlib.sha256("|".join(s["key"] for s in sessions).encode()).hexdigest()[:16]


def todays(plan, today):
    """Today's session for each of the plan's sports as a workout for the Workout page (the shape workouts.recommend
    gives), or a rest day; a triathlon's race day under running."""
    if plan["over"]:
        return []
    day = today.isoformat()
    cfg = plan["config"]
    all_sessions = [(w, s) for w in plan["weeks"] for s in w["sessions"]]
    i = plan["week_index"]
    out = []
    for sport in plan["sports"]:
        mine = lambda s: s["sport"] == sport or (sport == "run" and s["sport"] == "tri")
        found = next(((w, s) for w, s in all_sessions if mine(s) and s["date"] == day and s["status"] in ("today", "done")), None)
        why = [f"Plan week {i + 1} of {len(plan['weeks'])}" if i is not None else "Plan"]
        base = {"sport": sport, "label": workouts.ALL_SPORTS[sport], "plan": True}
        if sport == "swim":
            base["pool"] = cfg["sports"]["swim"].get("pool", 25)
        if found:
            w, s = found
            why.append(PHASES[w["phase"]] + (" · deload" if w["deload"] else ""))
            if s.get("changed"):
                why.append("Changed in chat" + (f": {s['reason']}" if s["reason"] else ""))
            out.append({**base, "kind": s["kind"], "title": s["title"], "total_s": s["total_s"] or 0, "km": s["km"],
                        "why": why, "steps": s["steps"], "paces": sport == "run", "key": s["key"], "sent": s["sent"]})
            continue
        nxt = next((s for _, s in all_sessions if mine(s) and s["date"] > day and s["status"] == "upcoming"), None)
        out.append({**base, "kind": "rest", "title": "Rest day", "total_s": 0, "km": None, "why": why, "steps": [],
                    "paces": False, "sent": False, "next": {"date": nxt["date"], "title": nxt["title"]} if nxt else None})
    return out
