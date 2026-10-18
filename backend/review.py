"""How a run went against the workout it was run from: today's suggested session or a plan session, sent to the watch
from the dashboard.

Garmin marks an activity done from a scheduled workout with that workout's id, and the dashboard keeps each workout
it sends under its id (the archive), as it went to the watch: a plan's paces adapt later, so the plan's own session
can differ by the time it's reviewed. Each step is set against the lap the watch recorded for it."""
import json
from datetime import date, timedelta

import coach
import db
import plans
from workouts import _flat

ARCHIVE_KEY = "workouts_archive"
ARCHIVE_DAYS = 90  # how long a sent workout is kept for its review
NOTES_KEY = "run_reviews"
NOTES_KEPT = 30
SPORTS = ("run", "bike")  # laps line up with the steps on runs and rides; a swim's lengths don't

SYSTEM = """You are the coach in a personal training dashboard. The athlete has just done a session the dashboard \
sent to their watch, either today's suggested session or one from their training plan, and asks for your feedback on it.

You get the session's purpose and context, the planned steps with their targets, what was done in each step (from the \
watch's laps), and the numbers that follow from it. Write 2-3 sentences: how the run went against the targets (cite \
the numbers that matter, such as the reps and their paces), what it contributes to the week or the plan, and one \
concrete thing to take into the next session of this kind. Warm-ups and cool-downs go by feel and have no target; \
don't judge their pace. Be direct and specific, and don't repeat the numbers back as a list.

Reply in plain text without markdown: no headings, no bold, no lists. Paces are min:sec per km."""


# ---- Remembering what was sent

def load_archive(conn):
    return json.loads(db.get_meta(conn, ARCHIVE_KEY) or "{}")


def remember(conn, workout_id, w, day, context, today):
    """Keep a workout sent to Garmin under its id, as it went: its steps, and why it was picked (`context`)."""
    archive = load_archive(conn)
    keep = (today - timedelta(days=ARCHIVE_DAYS)).isoformat()
    archive = {k: v for k, v in archive.items() if v["date"] >= keep}
    archive[str(workout_id)] = {"date": day.isoformat(), "sport": w["sport"], "title": w["title"], "kind": w.get("kind"),
                                "steps": w["steps"], "total_s": w.get("total_s"), "km": w.get("km"), "why": context,
                                "plan": bool(w.get("plan"))}
    db.set_meta(conn, ARCHIVE_KEY, json.dumps(archive))


def plan_context(plan, day):
    """Why a plan session is on its day: the plan's name, its week and the phase."""
    for i, wk in enumerate(plan["weeks"]):
        if wk["start"] <= day < (date.fromisoformat(wk["start"]) + timedelta(days=7)).isoformat():
            return [plan["config"]["name"], f"Plan week {i + 1} of {len(plan['weeks'])}",
                    plans.PHASES[wk["phase"]] + (" · deload" if wk["deload"] else "")]
    return [plan["config"]["name"]]


def _plan_session(plan, activity_id):
    """The plan session the activity counted for, or None."""
    for wk in (plan or {}).get("weeks") or []:
        for s in wk["sessions"]:
            if (s.get("actual") or {}).get("id") == activity_id:
                return s
    return None


def planned(a, archive, sent, plan_sent, plan, todays):
    """The session the activity was run from, as sent to the watch, or None when it wasn't one the dashboard sent.
    `a` has the activity's id, date, sport and Garmin's workout_id. Workouts sent before the archive was kept are
    found in the lists of sent workouts: today's from today's sessions (`todays`), a plan's from the plan."""
    wid = a.get("workout_id")
    if not wid or a.get("sport") not in SPORTS:
        return None
    if str(wid) in archive:
        return archive[str(wid)]
    if any(x.get("id") == wid for x in sent):
        w = next((w for w in todays if w["sport"] == a["sport"] and w.get("steps")), None)
        if w and a["date"] == date.today().isoformat():
            return {"date": a["date"], "sport": w["sport"], "title": w["title"], "kind": w["kind"], "steps": w["steps"],
                    "total_s": w["total_s"], "km": w.get("km"), "why": w["why"], "plan": bool(w.get("plan"))}
    if any(x.get("id") == wid for x in plan_sent):
        s = _plan_session(plan, a["id"])
        if s:
            return {"date": s["date"], "sport": s["sport"], "title": s["title"], "kind": s["kind"], "steps": s["steps"],
                    "total_s": s["total_s"], "km": s["km"], "why": plan_context(plan, s["date"]), "plan": True}
    return None


# ---- Each step against its lap

def _off(lap, target):
    """How far outside the target the lap was: (amount, side), (0, None) within it, None without a target or data."""
    if not target:
        return None
    if target["type"] == "pace":
        if not lap.get("speed"):
            return None
        p, margin = 1000 / lap["speed"], target["slow"] * plans.DEADBAND  # a second or two off counts as on target
        return (round(target["fast"] - p), "fast") if p < target["fast"] - margin else \
            (round(p - target["slow"]), "slow") if p > target["slow"] + margin else (0, None)
    v, lo, hi = (lap.get("power"), target["lo"], target["hi"]) if target["type"] == "power" else \
        (lap.get("avg_hr"), target["lo"], target["hi"])
    if not v:
        return None
    return (round(v - hi), "high") if v > hi + 0.5 else (round(lo - v), "low") if v < lo - 0.5 else (0, None)


def _row(step, lap):
    off = _off(lap, step["target"])
    return {"label": step["label"], "kind": step["kind"], "intensity": step["intensity"], "planned_s": step["s"],
            "m": step.get("m"), "target": step["target"], "note": step.get("note"),
            "done_s": lap.get("duration_s"), "km": lap.get("km"), "speed": lap.get("speed"), "power": lap.get("power"),
            "hr": lap.get("avg_hr"), "off": off[0] if off else None, "side": off[1] if off else None}


def compare(steps, laps, a):
    """(rows, paired): one row per step with its lap when the laps pair with the steps; otherwise one row for the
    whole run, set against the target only when the session is a single step."""
    paired = plans.pair_laps(steps, laps or [])
    if paired:
        return [_row(s, l) for l, s in paired], True
    flat = list(_flat(steps))
    whole = {"duration_s": a.get("duration_s"), "km": a.get("km"), "speed": a.get("speed"), "avg_hr": a.get("avg_hr")}
    step = flat[0] if len(flat) == 1 else {"label": "Whole session", "kind": "interval", "intensity": "easy",
                                           "s": sum(s["s"] for s in flat), "target": None}
    return [_row(step, whole)], False


def build(a, session, laps, plan):
    """The review's numbers: the steps against their laps, how many hit their target, and for a plan run the plan's
    verdict and the projected time it moved."""
    rows, paired = compare(session["steps"], laps, a)
    aimed = [r for r in rows if r["off"] is not None]
    work = [r for r in aimed if r["intensity"] in ("high", "anaerobic")]
    counted = work or aimed
    out = {
        "id": a["id"], "sport": session["sport"], "title": session["title"], "date": session["date"],
        "why": session.get("why") or [], "plan": session.get("plan", False), "paired": paired, "rows": rows,
        "on_target": [sum(r["off"] == 0 for r in counted), len(counted)] if counted else None,
        "reps": bool(work),
        "planned": {"s": session.get("total_s"), "km": session.get("km")},
        "done": {"s": a.get("duration_s"), "km": a.get("km"), "hr": a.get("avg_hr")},
        "effect": {"label": a.get("te_label"), "aerobic": a.get("aerobic_te"), "anaerobic": a.get("anaerobic_te")},
        "verdict": None, "projection": None,
    }
    s = _plan_session(plan, a["id"])
    if s:
        out["verdict"] = s["actual"].get("verdict")
        change = (plan.get("projection") or {}).get("change")
        if change and change["date"] == s["date"] and change["title"] == s["title"]:
            out["projection"] = {"from_s": change["from_s"], "to_s": plan["projection"]["s"]}
    return out


# ---- What the coach is asked

def _pace(speed):
    return f"{coach._mmss(1000 / speed)}/km" if speed else "?"


def _done(r, sport):
    parts = [coach._minutes(r["done_s"]) if r["done_s"] else ""]
    if r["km"]:
        parts.append(f"{r['km']:.2f} km")
    if sport == "run" and r["speed"]:
        parts.append(_pace(r["speed"]))
    if r["power"]:
        parts.append(f"{round(r['power'])} W")
    if r["hr"]:
        parts.append(f"{round(r['hr'])} bpm")
    off = "" if r["off"] is None else " - on target" if r["off"] == 0 else \
        f" - {r['off']} {'s/km' if (r['target'] or {}).get('type') == 'pace' else 'W' if r['target']['type'] == 'power' else 'bpm'} {r['side']}"
    return ", ".join(p for p in parts if p) + off


def prompt(r):
    """The review as text for the coach."""
    lines = [f"Session: {r['title']} ({r['sport']}), {r['date']}",
             f"Why this session: {' · '.join(r['why'])}" if r["why"] else "",
             f"Planned: {coach._minutes(r['planned']['s'] or 0)}" + (f", about {r['planned']['km']} km" if r["planned"]["km"] else ""),
             f"Done: {coach._minutes(r['done']['s'] or 0)}, {r['done']['km']} km" + (f", average {round(r['done']['hr'])} bpm" if r["done"]["hr"] else ""),
             "", "Step by step (planned -> done):" if r["paired"] else "The laps don't line up with the steps; the whole run:"]
    for x in r["rows"]:
        lines.append(f"- {coach.steps_text([{**x, 's': x['planned_s']}])} -> {_done(x, r['sport'])}")
    if r["on_target"]:
        lines.append(f"\n{r['on_target'][0]} of {r['on_target'][1]} {'reps' if r['reps'] else 'steps'} on target")
    if r["verdict"]:
        lines.append(f"The plan judges it: {r['verdict']}")
    if r["projection"]:
        lines.append(f"It moved the plan's projected race time from {coach._time(r['projection']['from_s'])} "
                     f"to {coach._time(r['projection']['to_s'])}")
    e = r["effect"]
    if e["label"] or e["aerobic"]:
        lines.append(f"Garmin's training effect: {(e['label'] or '').replace('_', ' ').lower() or '-'}, aerobic "
                     f"{e['aerobic'] or 0}, anaerobic {e['anaerobic'] or 0}")
    return "\n".join(l for l in lines if l is not None).strip()


# ---- The coach's notes, kept so a review opened again costs nothing

def get_note(conn, activity_id):
    return json.loads(db.get_meta(conn, NOTES_KEY) or "{}").get(str(activity_id))


def save_note(conn, activity_id, note):
    notes = json.loads(db.get_meta(conn, NOTES_KEY) or "{}")
    notes[str(activity_id)] = note
    db.set_meta(conn, NOTES_KEY, json.dumps(dict(list(notes.items())[-NOTES_KEPT:])))
