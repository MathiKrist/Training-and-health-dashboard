"""The chat on the Workout page: Claude, with tools that change the suggested or planned sessions.

Claude is reached one of two ways: through the Anthropic API when ANTHROPIC_API_KEY is set (billed per message, on
Haiku unless CHAT_MODEL says sonnet), otherwise through the Claude Code program on this machine, run in the background
for each message (covered by its Claude subscription). Either way each message's tokens go to data/chat_usage.csv,
priced as the API would charge them on Haiku and on Sonnet, to compare the two before switching.

Each message gets the current numbers (today's workout, the plan's coming weeks, recent runs) as a note, so the chat
always talks about what the page shows. There's one conversation a day: kept on the server for the API (it only ever
grows, so Claude's reasoning in earlier turns stays valid), as a resumed session for Claude Code."""
import csv
import json
import logging
import os
import shutil
import subprocess
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic

import changes as chat_changes
import config
import db
import workouts

log = logging.getLogger("coach")

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5"}
MODEL = MODELS.get(config.CHAT_MODEL, MODELS["haiku"])
# US dollars per million tokens: input, output, cache write (5 min), cache read. Haiku costs 5x more past 100K tokens of
# prompt. Opus is here for Sonnet's fallback on a declined answer.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
}
LONG_PROMPT = {"claude-haiku-5-5": (100_000, 5)}  # prompt tokens past which the prices go up, and by how much
USAGE_LOG = config.BASE_DIR / "data" / "chat_usage.csv"
# cost_usd is what the API charged (empty with Claude Code); haiku_usd and sonnet_usd are what the same tokens cost on
# the API with each model, to compare before switching
USAGE_FIELDS = ["time", "backend", "model", "rounds", "input", "cache_write", "cache_read", "output", "cost_usd",
                "haiku_usd", "sonnet_usd"]
CLI_MODEL = "sonnet"  # Claude Code's name for its latest Sonnet
CLI_TIMEOUT = 180  # seconds Claude Code gets per answer
CLI_DIR = config.BASE_DIR / "data" / "coach"  # where Claude Code runs, away from the project's own files
MAX_ROUNDS = 8  # tool rounds per message (with Claude Code: answers after changes failed)
LOG_KEY = "coach_chat"
_lock = threading.Lock()  # one message at a time: the conversation and the changes are shared by every device

SYSTEM = """You are the coach in a personal training dashboard. The athlete talks to you from the Workout page, which \
shows today's suggested session per sport (running and cycling) and, if they have one, a training plan toward a race, \
a triathlon or general fitness: runs, and rides and swims if they picked those. The sessions come from Garmin's numbers: readiness, training status, load focus, lactate \
threshold and race predictions.

Your job is to change sessions when the athlete asks: shorten one, swap it for another kind, move a plan session to \
another day, make a rest day, add a session later in the week. Make each change with a tool (set_workout, \
set_rest_day, move_workout, undo_change); never just describe a change without making it. Each message comes with a \
note holding the current schedule and numbers; trust it over anything said earlier in the chat.

How to change things well:
- Prefer standard sessions (kind + minutes): their paces come from the athlete's threshold and adapt with the plan. \
Use custom steps only for something a standard session can't express.
- Keep the week's intent. If a key session (intervals, long run, long ride) can't happen today, move it to a free day \
rather than drop it, and avoid two hard days in a row or a hard day right before the long run or ride. Swims are \
best changed with minutes; their steps can't set a pace. When shortening, keep the warm-up \
and cool-down and cut the main set. Warm-ups and cool-downs go by feel: they never get a target.
- If the athlete says they lack time today, shorten today's session; if the work matters for the week, put the rest on \
a later easy or free day.
- Moving a session onto a day that already has one replaces it. Say so, and move or drop that one deliberately.
- Changes can only be made from today on. Undo a change by name of its day when asked to go back to the original.
- Mind low readiness, long recovery time, poor sleep, overreaching and a race close by: don't add intensity then. A \
rest day or a short recovery session is a good answer on such a day.
- If something is unclear (which day, how much time they have), ask one short question instead of guessing.

Reply in plain text without markdown: no headings, no bold, no tables. Be brief: say what you changed, with day, \
session and length, in a sentence or two, and why if it isn't obvious. Paces are min:sec per km."""

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["warmup", "interval", "recovery", "cooldown", "repeat"]},
        "label": {"type": "string", "description": "Short name shown for the step, e.g. 'Threshold' or 'Easy jog'."},
        "minutes": {"type": "number", "description": "Length of the step (0.5 = 30 s). Not for repeats."},
        "target": {"type": "string", "enum": list(chat_changes.TARGETS),
                   "description": "Pace or heart rate target by name; resolved to the athlete's own paces or zones."},
        "note": {"type": "string", "description": "Optional short cue, e.g. 'Fast but relaxed'."},
        "times": {"type": "integer", "description": "For a repeat: how many times its steps are done."},
        "steps": {"type": "array", "items": {"type": "object"}, "description": "For a repeat: its steps (no nested repeats)."},
    },
    "required": ["type"],
}

DAY = {"type": "string", "description": "YYYY-MM-DD"}
SPORT = {"type": "string", "enum": list(workouts.ALL_SPORTS)}
REASON = {"type": "string", "description": "A few words the page shows with the change, e.g. 'Short on time'."}

TOOLS = [
    {
        "name": "set_workout",
        "description": "Put a session on a day for a sport, in place of whatever is suggested or planned there. Either "
                       "a standard session (kind and minutes; interval kinds size the main set to fit the minutes) or "
                       "custom steps.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": DAY, "sport": SPORT,
                "kind": {"type": "string", "enum": list(chat_changes.KINDS),
                         "description": "Session kind. 'custom' and 'race_pace' need steps."},
                "minutes": {"type": "number", "description": "Total length, for a standard session."},
                "title": {"type": "string", "description": "Optional title; standard sessions get one."},
                "steps": {"type": "array", "items": STEP_SCHEMA, "description": "Custom steps, in order."},
                "reason": REASON,
            },
            "required": ["date", "sport", "kind", "reason"],
        },
    },
    {
        "name": "set_rest_day",
        "description": "Make a day a rest day for a sport: nothing suggested or planned.",
        "input_schema": {"type": "object", "properties": {"date": DAY, "sport": SPORT, "reason": REASON},
                         "required": ["date", "sport", "reason"]},
    },
    {
        "name": "move_workout",
        "description": "Move the session scheduled on one day to another, leaving a rest day behind. Replaces "
                       "whatever is on the new day.",
        "input_schema": {"type": "object", "properties": {"sport": SPORT, "from_date": DAY, "to_date": DAY, "reason": REASON},
                         "required": ["sport", "from_date", "to_date", "reason"]},
    },
    {
        "name": "undo_change",
        "description": "Remove a chat change from a day, so the suggested or planned session is back.",
        "input_schema": {"type": "object", "properties": {"date": DAY, "sport": SPORT}, "required": ["date", "sport"]},
    },
]


def _claude_code():
    """The Claude Code program, or None. Looked for on PATH and where its installer puts it."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home() / ".local" / "bin"
    return next((str(p) for p in (home / "claude.exe", home / "claude") if p.is_file()), None)


def backend():
    """How the chat reaches Claude: "api", "claude_code", or None when it can't."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return "api"
    return "claude_code" if _claude_code() else None


# ---- The numbers the chat gets with each message

def _mmss(s):
    return f"{int(s) // 60}:{int(s) % 60:02d}"


def _time(s):
    s = round(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else _mmss(s)


def _minutes(s):
    return f"{round(s / 60)} min" if s >= 60 else f"{round(s)} s"


def _target(t):
    if not t:
        return ""
    if t["type"] == "pace":
        return f" @ {_mmss(t['fast'])}-{_mmss(t['slow'])}/km"
    if t["type"] == "power":
        return f" @ {t['lo']}-{t['hi']} W"
    return f" @ zone {t['zone']} ({t['lo']}-{t['hi']} bpm)"


def steps_text(steps):
    parts = []
    for s in steps:
        if s["kind"] == "repeat":
            parts.append(f"{s['n']} x [" + ", ".join(steps_text([x]) for x in s["steps"]) + "]")
        else:
            parts.append(f"{_minutes(s['s'])} {s['label']}{_target(s['target'])}" + (f" ({s['note']})" if s.get("note") else ""))
    return "; ".join(parts)


def _session_line(s):
    size = ", ".join(x for x in (_minutes(s["total_s"]) if s.get("total_s") else "",
                                 f"{s['km']} km" if s.get("km") else "") if x)
    day = date.fromisoformat(s["date"]).strftime("%a %d %b")
    extra = f" [{s['status']}" + (", changed in chat" if s.get("changed") else "") + \
            (f", moved from {s['moved_from']}" if s.get("moved_from") else "") + "]"
    return f"- {day} ({s['date']}) {s.get('sport', 'run')}: {s['title']} ({s['kind']}, {size}){extra}" + \
        (f": {steps_text(s['steps'])}" if s["steps"] and s["status"] in ("today", "upcoming") else "")


def state(d, today):
    """The system note sent with each message: today, the numbers behind the suggestion, the schedule, recent training."""
    out = [f"Now: {today.strftime('%A %d %B %Y')} ({today.isoformat()}), {datetime.now().strftime('%H:%M')}."]
    t = d["today"]
    vals = [f"readiness {round(t['readiness'])}" if t.get("readiness") is not None else None,
            f"recovery time left on waking {round(t['recovery_h'])} h" if t.get("recovery_h") is not None else None,
            f"HRV {round(t['hrv_last_night'])} ms" if t.get("hrv_last_night") else None,
            f"sleep {t['sleep_s'] / 3600:.1f} h" if t.get("sleep_s") else None,
            f"sleep score {round(t['sleep_score'])}" if t.get("sleep_score") is not None else None,
            f"resting HR {round(t['resting_hr'])}" if t.get("resting_hr") else None]
    if d.get("training_status"):
        vals.append(f"training status {d['training_status']['label']}")
    if d.get("load_focus"):
        vals.append(f"load focus: {d['load_focus']['title']}")
    out.append("Latest numbers: " + (", ".join(v for v in vals if v) or "none synced"))
    if d.get("race"):
        r = d["race"]
        out.append(f"Race goal: {r['name']}, {r['date']} ({r['days_to_go']} days to go), {r['metres'] / 1000:g} km, "
                   f"target {_time(r['target_s'])}" + (f", Garmin predicts {_time(r['predicted_s'])}" if r.get("predicted_s") else ""))

    out.append("\nToday's workout per sport, as the page shows it:")
    for w in d["workouts"]:
        size = ", ".join(x for x in (_minutes(w["total_s"]) if w["total_s"] else "", f"{w['km']} km" if w.get("km") else "") if x)
        out.append(f"- {w['sport']}: {w['title']}" + (f" ({size})" if size else "") + f". Why: {'; '.join(w['why'])}." +
                   (f" Steps: {steps_text(w['steps'])}." if w["steps"] else "") + (" Already sent to the watch." if w.get("sent") else ""))

    p = d.get("plan")
    if p and not p["over"]:
        c = p["config"]
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        days = lambda x: ", ".join(names[i] for i in x["days"]) + (f" (long on {names[x['long_day']]})"
                                                                    if x.get("long_day") is not None else "")
        sports = "; ".join(f"{k} on {days(x)}" for k, x in c["sports"].items())
        what = f"{p['tri_label']} triathlon" if p.get("tri_label") else f"{c['metres'] / 1000:g} km"
        out.append(f"\nTraining plan: {c['name']}, {p['goal_label']}, {what} on {c['date']}. Sports: {sports}."
                   + (f" Planned run km for each week: {', '.join(f'{w['km_planned']:g}' for w in p['weeks'])}."
                      if "run" in c["sports"] else "")
                   + (f" Planned hours for each week, all sports: {', '.join(f'{w['hours_planned']:g}' for w in p['weeks'])}."
                      if p.get("multi") else "")
                   + (f" Week {p['week_index'] + 1} of {len(p['weeks'])}." if p["week_index"] is not None else ""))
        if p.get("projection"):
            out.append(f"The plan projects {_time(p['projection']['s'])} on its end date.")
        since, until = (today - timedelta(days=today.weekday())).isoformat(), (today + timedelta(days=21)).isoformat()
        out.append("Plan sessions this week and the next three (days not listed are rest days):")
        for w in p["weeks"]:
            out += [_session_line(s) for s in w["sessions"] if since <= s["date"] <= until]
    else:
        out.append("\nNo training plan. Only today's session is suggested; later days have nothing unless changed in chat.")
    if d.get("workouts_ahead"):
        out.append("Later days changed in chat (outside a plan):")
        for x in d["workouts_ahead"]:
            out.append(f"- {x['date']} {x['sport']}: {x['title']}" + (f", {_minutes(x['total_s'])}" if x["total_s"] else ""))

    # Weekly run km over a longer stretch than the activity list, for the usual volume
    monday = today - timedelta(days=today.weekday())
    weeks = [monday - timedelta(weeks=i) for i in range(8, -1, -1)]
    km = [sum(a.get("km") or 0 for a in d["activities"] if a["sport"] == "run"
              and w.isoformat() <= a["date"] < (w + timedelta(days=7)).isoformat()) for w in weeks]
    out.append("\nRun km per week (Monday to Sunday) over the last 8 weeks and this one so far: "
               + ", ".join(f"{w.strftime('%d %b')} {k:.1f}" for w, k in zip(weeks, km)) + ".")

    since = (today - timedelta(days=14)).isoformat()
    acts = [a for a in d["activities"] if a["date"] >= since]
    out.append("\nActivities in the last 14 days, newest first:" if acts else "\nNo activities in the last 14 days.")
    for a in acts[:25]:
        bits = [a["name"] or a["type"], f"{a['km']} km" if a.get("km") else "",
                _minutes(a["duration_s"]) if a.get("duration_s") else "",
                f"{_mmss(1000 / a['speed'])}/km" if a.get("speed") and a["sport"] == "run" else "",
                f"avg HR {round(a['avg_hr'])}" if a.get("avg_hr") else "", a.get("te_label") or ""]
        out.append(f"- {a['date']} {a['sport']}: " + ", ".join(b for b in bits if b))
    return "\n".join(out)


# ---- The tools

def _scheduled(d, sport, day, stored):
    """The session on a day for a sport, as worked out (steps with paces), or None."""
    if day == d["generated"]:
        w = next((w for w in d["workouts"] if w["sport"] == sport), None)
        return w if w and w["steps"] else None
    c = next((c for c in stored if c["date"] == day and c["sport"] == sport), None)
    if c:
        return {"rest": True} if c["rest"] else c
    if d.get("plan") and sport in d["plan"]["sports"]:
        return next((s for w in d["plan"]["weeks"] for s in w["sessions"]
                     if s["date"] == day and s["sport"] == sport and s["steps"]), None)
    return None


def run_tool(name, args, d, conn, today):
    """Carry out one tool call against the stored changes. Returns (result text for Claude, line for the page or None).
    ValueError means the call was wrong; its message goes back to Claude."""
    stored = chat_changes.load(conn)
    if name == "undo_change":
        left = [c for c in stored if (c["date"], c["sport"]) != (args["date"], args["sport"])]
        if len(left) == len(stored):
            raise ValueError(f"There's no chat change on {args['date']} for {args['sport']}.")
        chat_changes.save(conn, left, today)
        return "Undone; the suggested or planned session is back.", f"{_day(args['date'])}: change undone"
    if name == "set_rest_day":
        c = chat_changes.rest(args["date"], args["sport"], args.get("reason"), today)
        new = [c]
    elif name == "set_workout":
        c = chat_changes.make(args["date"], args["sport"], kind=args["kind"], minutes=args.get("minutes"),
                              title=args.get("title"), steps=args.get("steps"), reason=args.get("reason"), today=today)
        new = [c]
    elif name == "move_workout":
        sport, src = args["sport"], chat_changes.check_day(args["from_date"], today)
        dst = chat_changes.check_day(args["to_date"], today)
        if src == dst:
            raise ValueError("from_date and to_date are the same day.")
        s = _scheduled(d, sport, src, stored)
        if not s or s.get("rest"):
            raise ValueError(f"Nothing is scheduled on {src} for {sport}.")
        moved = {**s, "date": dst, "reason": args.get("reason") or ""} if "rest" in s else \
            chat_changes.copy(s, dst, sport, args.get("reason"))
        new = [chat_changes.rest(src, sport, args.get("reason"), today), moved]
    else:
        raise ValueError(f"Unknown tool {name}.")
    replaced = [_scheduled(d, c["sport"], c["date"], stored) for c in new]
    for c in new:
        stored = chat_changes.put(stored, c)
    chat_changes.save(conn, stored, today)
    lines = [_change_line(c) for c in new]
    note = "; ".join(f"{_day(c['date'])} had {r['title']} before" for c, r in zip(new, replaced)
                     if r and not r.get("rest") and r.get("title") and not (name == "move_workout" and c is new[0]))
    return "Done: " + "; ".join(lines) + (f". Replaced: {note}." if note else "."), "; ".join(lines)


def _day(iso):
    return date.fromisoformat(iso).strftime("%a %d %b").replace(" 0", " ")


def _change_line(c):
    if c["rest"]:
        return f"{_day(c['date'])}: rest day" + ("" if c["sport"] == "run" else f" ({workouts.ALL_SPORTS[c['sport']].lower()})")
    return f"{_day(c['date'])}: {chat_changes.title_of(c)}" + (f", {c['minutes']} min" if c.get("minutes") else "")


# ---- The conversation

def _load(conn, today):
    chat = json.loads(db.get_meta(conn, LOG_KEY) or "null")
    return chat if chat and chat.get("date") == today.isoformat() else {"date": today.isoformat(), "messages": [], "log": []}


def history(db_path):
    conn = db.connect(db_path)
    try:
        return _load(conn, date.today())["log"]
    finally:
        conn.close()


# ---- What the API costs

def cost(model, input_tokens, output_tokens, cache_write, cache_read):
    """US dollars for one response."""
    price = PRICES.get(model, PRICES[MODEL])
    limit, factor = LONG_PROMPT.get(model, (None, 1))
    if limit and input_tokens + cache_write + cache_read > limit:
        price = tuple(p * factor for p in price)
    return (input_tokens * price[0] + output_tokens * price[1] + cache_write * price[2] + cache_read * price[3]) / 1e6


def _meter(how, model):
    """The tokens one message uses, over all its rounds. `cost` is what the API charges; None with Claude Code."""
    return {"backend": how, "model": model, "rounds": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
            "tokens": 0, "cost": 0.0 if how == "api" else None, "haiku": 0.0, "sonnet": 0.0}


def _count(used, model, input_tokens, output_tokens, cache_write, cache_read):
    """Add one response's tokens to the message's total, priced as charged and on each API model."""
    tokens = (input_tokens, output_tokens, cache_write, cache_read)
    for k, n in zip(("input", "output", "cache_write", "cache_read"), tokens):
        used[k] += n
    used["tokens"] += sum(tokens)
    used["haiku"] += cost(MODELS["haiku"], *tokens)
    used["sonnet"] += cost(MODELS["sonnet"], *tokens)
    if used["cost"] is not None:
        used["cost"] += cost(model, *tokens)


def _finish(used):
    """Log the message's tokens: the meter, or None when nothing answered."""
    if not used["rounds"]:
        return None
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        new = not USAGE_LOG.exists()
        with open(USAGE_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=USAGE_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({**used, "time": datetime.now().isoformat(timespec="seconds"),
                        "cost_usd": "" if used["cost"] is None else f"{used['cost']:.6f}",
                        "haiku_usd": f"{used['haiku']:.6f}", "sonnet_usd": f"{used['sonnet']:.6f}"})
    except OSError as e:  # the file open in Excel, say: the answer still counts
        log.warning("Couldn't write the chat usage log: %s", e)
    return used


def usage(today=None):
    """How the chat runs and this month's spend: {backend, model (on the API), month_cost (what the API charged),
    month_haiku and month_sonnet (what every message this month would cost on the API with each)}."""
    month = (today or date.today()).isoformat()[:7]
    totals = {"cost_usd": 0.0, "haiku_usd": 0.0, "sonnet_usd": 0.0}
    if USAGE_LOG.exists():
        with open(USAGE_LOG, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["time"].startswith(month):
                    for k in totals:
                        totals[k] += float(r.get(k) or 0)
    how = backend()
    return {"backend": how, "model": MODEL if how == "api" else None, "month_cost": round(totals["cost_usd"], 4),
            "month_haiku": round(totals["haiku_usd"], 4), "month_sonnet": round(totals["sonnet_usd"], 4)}


def clear(db_path):
    conn = db.connect(db_path)
    try:
        conn.execute("DELETE FROM meta WHERE key = ?", (LOG_KEY,))
        conn.commit()
    finally:
        conn.close()


def send(db_path, text, dashboard):
    """Answer one message from the page: {log: the chat so far, changed: whether a session changed}. `dashboard`
    gives the dashboard's current numbers (they change as the chat changes sessions)."""
    with _lock:
        today = date.today()
        conn = db.connect(db_path)
        try:
            chat = _load(conn, today)
            chat["log"].append({"role": "user", "text": text})
            made = []

            def apply(name, args):
                """Carry out one change Claude asked for: (the text Claude gets back, whether it went through)."""
                try:
                    result, line = run_tool(name, args, dashboard(), conn, today)
                    conn.commit()
                    made.append(line)
                    return result, True
                except (ValueError, KeyError, TypeError) as e:
                    return f"Not changed: {e}", False

            how = _send_api if backend() == "api" else _send_cli
            reply, error, used = how(chat, text, dashboard, today, apply)
            chat["log"].append({"role": "assistant", "text": reply if not error else "", "error": error, "changes": made,
                                **({"usage": {k: used[k] if used[k] is None else round(used[k], 6)
                                              for k in ("tokens", "cost", "haiku", "sonnet")}} if used else {})})
            db.set_meta(conn, LOG_KEY, json.dumps(chat))
            conn.commit()
            return {"log": chat["log"], "changed": bool(made), **usage(today)}
        finally:
            conn.close()


# ---- Through the Anthropic API: Claude calls the tools itself

def _content(blocks):
    """The assistant turn to keep in the history. After a fallback to another model mid-answer, the declined model's
    reasoning and tool calls before the switch are left out."""
    blocks = [b.to_dict() for b in blocks]
    last = max((i for i, b in enumerate(blocks) if b["type"] == "fallback"), default=None)
    if last is None:
        return blocks
    return [b for i, b in enumerate(blocks)
            if i > last or b["type"] not in ("thinking", "redacted_thinking", "tool_use")]


def _create(client, messages):
    """One response. Sonnet falls back to another model when it declines; Haiku has no such fallback."""
    args = dict(model=MODEL, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages,
                output_config={"effort": "medium"}, cache_control={"type": "ephemeral"})
    if MODEL == MODELS["haiku"]:
        return client.messages.create(**args)
    return client.beta.messages.create(**args, betas=["server-side-fallback-2026-07-01"], fallbacks="default")


def _send_api(chat, text, dashboard, today, apply):
    """(reply, error, tokens used) for one message, with the conversation kept in chat["messages"]."""
    messages, start = chat["messages"], len(chat["messages"])
    messages.append({"role": "user", "content": text})
    messages.append({"role": "system", "content": state(dashboard(), today)})
    reply, error = "", None
    used = _meter("api", MODEL)
    client = anthropic.Anthropic()
    try:
        for _ in range(MAX_ROUNDS):
            response = _create(client, messages)
            u = response.usage
            used["rounds"] += 1
            _count(used, response.model, u.input_tokens or 0, u.output_tokens or 0, u.cache_creation_input_tokens or 0,
                   u.cache_read_input_tokens or 0)
            if response.stop_reason == "refusal":
                raise RuntimeError("Claude declined to answer that.")
            content = _content(response.content)
            messages.append({"role": "assistant", "content": content})
            reply = "\n\n".join(b["text"] for b in content if b["type"] == "text").strip()
            calls = [b for b in content if b["type"] == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                break
            results = []
            for call in calls:
                result, ok = apply(call["name"], call["input"])
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": result,
                                **({} if ok else {"is_error": True})})
            messages.append({"role": "user", "content": results})
        else:
            reply = reply or "That took more steps than expected; check the changes below."
    except anthropic.AuthenticationError:
        error = "The Anthropic API key in .env was refused."
    except anthropic.RateLimitError:
        error = "Too many requests to Claude just now. Try again in a minute."
    except anthropic.APIConnectionError:
        error = "Couldn't reach Claude. Is the internet connection up?"
    except anthropic.APIStatusError as e:
        log.error("Chat failed: %s", e)
        error = f"Claude answered with an error ({e.status_code})."
    except RuntimeError as e:
        error = str(e)
    if error or messages[-1]["role"] != "assistant":
        # The turn didn't finish: the history keeps complete turns only, so the next message starts clean (the
        # changes made stay, and the next message's note shows them)
        del messages[start:]
    return reply, error, _finish(used)


# ---- Through Claude Code: it answers in a fixed shape, a reply and the changes to make, which are then carried out

def _cli_schema():
    """The answer's shape: the reply, and the changes as {tool, ...that tool's arguments}."""
    fields = {}
    for tool in TOOLS:
        fields.update(tool["input_schema"]["properties"])
    action = {"type": "object", "properties": {"tool": {"type": "string", "enum": [t["name"] for t in TOOLS]}, **fields},
              "required": ["tool"]}
    return {"type": "object", "required": ["reply", "actions"], "properties": {
        "reply": {"type": "string", "description": "What the athlete reads."},
        "actions": {"type": "array", "items": action, "description": "The changes to make, in order; empty for none."}}}


CLI_SYSTEM = SYSTEM + """

Here the tools aren't called directly. Your answer is JSON: "reply", the text the athlete reads, and "actions", the \
tool calls to make, each {"tool": name, ...its arguments}. They're carried out after you answer; if any fail you're \
told why and can answer again. Write the reply as if the changes are made. The tools:
""" + "\n".join(f"- {t['name']}({', '.join(t['input_schema']['properties'])}): {t['description']}" for t in TOOLS) + """
Custom steps: {"type": warmup|interval|recovery|cooldown, "label", "minutes", "target", "note"}, or \
{"type": "repeat", "times", "steps": [...]}."""


class CliError(Exception):
    pass


def _run_cli(prompt, session, system=None, schema=None):
    """One answer from Claude Code: (the structured answer, its session id, its tokens per model). The chat's system
    prompt and answer shape unless given others."""
    args = [_claude_code(), "-p", "--output-format", "json", "--model", CLI_MODEL, "--effort", "medium",
            "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
            "--system-prompt", system or CLI_SYSTEM, "--json-schema", json.dumps(schema or _cli_schema())]
    if session:
        args += ["--resume", session]
    CLI_DIR.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=CLI_DIR,
                              timeout=CLI_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise CliError("Claude Code took too long to answer. Try again.")
    except OSError as e:
        raise CliError(f"Couldn't start Claude Code: {e}")
    try:
        out = json.loads(done.stdout)
    except ValueError:
        log.error("Claude Code failed (%s): %s %s", done.returncode, done.stdout[-500:], done.stderr[-500:])
        said = (done.stdout + done.stderr).lower()
        if "log in" in said or "login" in said or "/login" in said:
            raise CliError("Claude Code isn't logged in. Run claude in a terminal and log in, then try again.")
        raise CliError("Claude Code couldn't answer. Is it installed and logged in?")
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        log.error("Claude Code answered with an error: %s", str(out)[:500])
        detail = out.get("result") or out.get("api_error_status") or out.get("subtype")
        raise CliError(f"Claude Code couldn't answer: {detail}" if detail else "Claude Code couldn't answer.")
    return out["structured_output"], out.get("session_id"), out.get("modelUsage") or {}


def _send_cli(chat, text, dashboard, today, apply):
    """(reply, error, tokens used) for one message, continuing the day's Claude Code session. Its subscription pays,
    so the tokens are only priced as they would be on the API."""
    prompt = f"<dashboard_note>\n{state(dashboard(), today)}\n</dashboard_note>\n\nThe athlete says:\n{text}"
    reply = ""
    used = _meter("claude_code", f"claude-{CLI_MODEL}")
    try:
        for _ in range(MAX_ROUNDS):
            try:
                answer, chat["session"], models = _run_cli(prompt, chat.get("session"))
            except CliError as e:
                if not chat.get("session") or "too long" in str(e):
                    raise
                # The saved session may be gone (deleted, or made on another machine): start a new one
                answer, chat["session"], models = _run_cli(prompt, None)
            used["rounds"] += 1
            for model, u in models.items():
                used["model"] = model
                _count(used, model, u.get("inputTokens") or 0, u.get("outputTokens") or 0,
                       u.get("cacheCreationInputTokens") or 0, u.get("cacheReadInputTokens") or 0)
            reply = str(answer.get("reply") or "").strip()
            failed = []
            for action in answer.get("actions") or []:
                args = {k: v for k, v in action.items() if k != "tool"}
                result, ok = apply(action.get("tool"), args)
                if not ok:
                    failed.append(f"{json.dumps(action)}: {result}")
            if not failed:
                break
            prompt = ("These changes failed and weren't made:\n" + "\n".join(failed) +
                      "\nThe others went through. Fix the failed ones, or say in the reply why not.")
    except CliError as e:
        return "", str(e), _finish(used)
    return reply, None, _finish(used)


# ---- One answer without tools or a conversation (the run review)

NOTE_SCHEMA = {"type": "object", "required": ["note"],
               "properties": {"note": {"type": "string", "description": "What the athlete reads."}}}


def ask(system, prompt):
    """(text, error, tokens used) for one question, through the API or Claude Code as the chat goes. Its tokens are
    logged with the chat's."""
    if backend() == "api":
        used = _meter("api", MODEL)
        try:
            response = anthropic.Anthropic().messages.create(
                model=MODEL, max_tokens=4000, system=system, messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low"})
            u = response.usage
            used["rounds"] += 1
            _count(used, response.model, u.input_tokens or 0, u.output_tokens or 0, u.cache_creation_input_tokens or 0,
                   u.cache_read_input_tokens or 0)
            if response.stop_reason == "refusal":
                return "", "Claude declined to answer that.", _finish(used)
            return "\n\n".join(b.text for b in response.content if b.type == "text").strip(), None, _finish(used)
        except anthropic.AuthenticationError:
            error = "The Anthropic API key in .env was refused."
        except anthropic.RateLimitError:
            error = "Too many requests to Claude just now. Try again in a minute."
        except anthropic.APIConnectionError:
            error = "Couldn't reach Claude. Is the internet connection up?"
        except anthropic.APIStatusError as e:
            log.error("Asking Claude failed: %s", e)
            error = f"Claude answered with an error ({e.status_code})."
        return "", error, _finish(used)
    used = _meter("claude_code", f"claude-{CLI_MODEL}")
    try:
        answer, _, models = _run_cli(prompt, None, system, NOTE_SCHEMA)
    except CliError as e:
        return "", str(e), _finish(used)
    used["rounds"] += 1
    for model, u in models.items():
        used["model"] = model
        _count(used, model, u.get("inputTokens") or 0, u.get("outputTokens") or 0,
               u.get("cacheCreationInputTokens") or 0, u.get("cacheReadInputTokens") or 0)
    return str(answer.get("note") or "").strip(), None, _finish(used)
