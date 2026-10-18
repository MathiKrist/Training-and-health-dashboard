"""Serve the dashboard on your local network.

    python backend/app.py          # real Garmin data (run backend/login.py first)
    python backend/app.py --demo   # fake data, to try the dashboard without Garmin
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import changes as chat_changes
import coach
import config
import db
import metrics
import plans
import review
import sync
import workouts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("app")

parser = argparse.ArgumentParser()
parser.add_argument("--demo", action="store_true", help="use generated demo data instead of Garmin")
args, _ = parser.parse_known_args()

DEMO = args.demo
DB_PATH = config.BASE_DIR / "data" / "demo.db" if DEMO else config.DB_PATH
STATIC = config.BASE_DIR / "frontend"


def _sync_loop():
    while True:
        log.info("Background sync: %s", sync.run_sync(DB_PATH))
        time.sleep(max(5, config.SYNC_INTERVAL_MINUTES) * 60)


@asynccontextmanager
async def lifespan(_app):
    if DEMO:
        import demo
        demo.seed(DB_PATH)
    else:
        threading.Thread(target=_sync_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# ---- Login: with DASHBOARD_PASSWORD set, every page asks for it first (and for DASHBOARD_USERNAME, when that's set too)

SESSION_COOKIE = "training_session"
SESSION_DAYS = 365
# Reachable before logging in, with the fonts and the app icons. Phones fetch the app manifest without the login cookie
OPEN_PATHS = {"/login", "/static/styles.css", "/static/js/theme.js", "/static/manifest.json", "/sw.js"}
OPEN_PREFIX = ("/static/vendor/fonts/", "/static/icons/")


def _session_token():
    """The cookie value a logged-in browser holds. It's derived from the login details, so changing them logs
    every device out."""
    key = config.DASHBOARD_PASSWORD.encode()
    return hmac.new(key, f"training-session:{config.DASHBOARD_USERNAME}".encode(), hashlib.sha256).hexdigest()


def _logged_in(request):
    return secrets.compare_digest(request.cookies.get(SESSION_COOKIE, ""), _session_token())


@app.middleware("http")
async def login_gate(request: Request, call_next):
    path = request.url.path
    if config.DASHBOARD_PASSWORD and path not in OPEN_PATHS and not path.startswith(OPEN_PREFIX) and not _logged_in(request):
        if request.method == "GET" and not path.startswith(("/api/", "/static/")):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"  # revalidate, so code changes show up without a hard refresh
    return response


def login_page(error=""):
    """The login page, with the username field only when a username is set, and the error from the last try."""
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    if config.DASHBOARD_USERNAME:
        html = html.replace('<label id="userField" hidden>', '<label id="userField">').replace(
            'id="username" autocomplete="username"', 'id="username" name="username" autocomplete="username" required')
    if error:
        html = html.replace('<p class="form-error" id="loginError" hidden></p>', f'<p class="form-error" id="loginError" role="alert">{error}</p>')
    return html


@app.get("/login")
def login_form(request: Request):
    if not config.DASHBOARD_PASSWORD or _logged_in(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(login_page())


@app.post("/login")
async def login(request: Request):
    form = parse_qs((await request.body()).decode())
    password = (form.get("password") or [""])[0]
    username = (form.get("username") or [""])[0].strip()
    ok = secrets.compare_digest(password.encode(), config.DASHBOARD_PASSWORD.encode())
    if config.DASHBOARD_USERNAME:
        ok = secrets.compare_digest(username.encode(), config.DASHBOARD_USERNAME.encode()) and ok
    if not ok:
        await asyncio.sleep(1)  # slows down guessing
        what = "username or password" if config.DASHBOARD_USERNAME else "password"
        return HTMLResponse(login_page(f"Wrong {what}. Try again."), status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, _session_token(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/sw.js")
def service_worker():
    """Served from the root rather than /static, so it covers the whole dashboard; it's what lets phones install it."""
    return FileResponse(STATIC / "sw.js", media_type="text/javascript")


# The dashboard's numbers, kept until the database changes or the day does: every open tab asks every 5 minutes
_dashboard_cache = {"key": None, "data": None}


def _db_version():
    """Changes with every write to the database (each commit touches the file or its write-ahead log) and each day."""
    files = [Path(f"{DB_PATH}{suffix}") for suffix in ("", "-wal")]
    return date.today(), *[(f.stat().st_mtime_ns, f.stat().st_size) if f.exists() else None for f in files]


def _dashboard_data():
    key = _db_version()
    if _dashboard_cache["key"] != key:
        _dashboard_cache.update(key=key, data=metrics.build_dashboard(DB_PATH))
    return _dashboard_cache["data"]


@app.get("/api/dashboard")
def dashboard(request: Request):
    data = {**_dashboard_data(), "demo": DEMO, "syncing": sync.is_running(),
            "login": bool(config.DASHBOARD_PASSWORD)}  # login shows the Log out button
    # An ETag, so a browser that already has this answer gets a short "not modified" instead
    body = json.dumps(data, separators=(",", ":")).encode()
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type="application/json", headers={"ETag": etag})


@app.get("/api/activity/{activity_id}")
def activity(activity_id: int):
    """One activity with all its stats, route and laps, for the popup."""
    if DEMO:
        import demo
        demo.fill_details(DB_PATH, activity_id)
        error = None
    else:
        error = sync.fetch_details(DB_PATH, activity_id)
    data = metrics.build_activity(DB_PATH, activity_id)
    if data is None:
        return JSONResponse({"error": "No such activity"}, status_code=404)
    data["details_error"] = error
    return data


@app.post("/api/race")
async def set_race(request: Request):
    """Save the race goal: {name, date (YYYY-MM-DD), metres, target_s}."""
    body = await request.json()
    try:
        goal = {
            "name": str(body.get("name") or "").strip()[:80] or "Race",
            "date": date.fromisoformat(body["date"]).isoformat(),
            "metres": float(body["metres"]),
            "target_s": int(body["target_s"]),
        }
        assert 100 <= goal["metres"] <= 1_000_000 and 0 < goal["target_s"] < 7 * 86400
    except (KeyError, TypeError, ValueError, AssertionError):
        return JSONResponse({"error": "Give a name, a date, a distance and a target time."}, status_code=400)
    conn = db.connect(DB_PATH)
    try:
        db.set_meta(conn, "race_goal", json.dumps(goal))
        conn.commit()
    finally:
        conn.close()
    return {"status": "saved"}


@app.delete("/api/race")
def remove_race():
    conn = db.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM meta WHERE key = 'race_goal'")
        conn.commit()
    finally:
        conn.close()
    return {"status": "removed"}


@app.post("/api/workout/{sport}")
async def send_workout(sport: str, request: Request):
    """Put today's workout for the sport in the Garmin calendar, so the watch has it after its next sync. Only ever
    called by the page's Send to watch button. The page sends the title it shows, so a workout that has changed
    since (after a sync) isn't sent unseen."""
    title = (await request.json()).get("title")
    data = _dashboard_data()
    w = next((w for w in data["workouts"] if w["sport"] == sport), None)
    if not w or w["title"] != title:
        return JSONResponse({"error": "Today's workout has changed. Look it over and press again."}, status_code=409)
    if not w["steps"]:
        return JSONResponse({"error": "There's no workout to send today."}, status_code=400)
    if DEMO:
        return JSONResponse({"error": "Demo mode doesn't send anything to Garmin."}, status_code=400)
    today = date.today()
    try:
        client = sync._client()
        created = client.upload_workout(workouts.to_garmin(w, today))
        client.schedule_workout(created["workoutId"], today.isoformat())
    except Exception as e:
        log.error("Sending the workout failed: %s", e)
        return JSONResponse({"error": f"Garmin didn't take the workout: {e}"}, status_code=502)
    conn = db.connect(DB_PATH)
    try:
        sent = [x for x in json.loads(db.get_meta(conn, "workouts_sent") or "[]") if x.get("date") == today.isoformat()]
        sent.append({"date": today.isoformat(), "sport": sport, "title": title, "id": created["workoutId"]})
        db.set_meta(conn, "workouts_sent", json.dumps(sent))
        if w.get("plan"):  # the plan page shows it as sent too
            plan_sent = json.loads(db.get_meta(conn, "plan_sent") or "[]")
            plan_sent.append({"date": today.isoformat(), "sport": sport, "key": w["key"], "id": created["workoutId"]})
            db.set_meta(conn, "plan_sent", json.dumps(plan_sent))
        # Kept as sent, with why it was picked, for reviewing the run done from it
        context = [data["plan"]["config"]["name"]] + w["why"] if w.get("plan") and data["plan"] else w["why"]
        review.remember(conn, created["workoutId"], w, today, context, today)
        conn.commit()
    finally:
        conn.close()
    return {"status": "sent"}


def _delete_from_garmin(ids):
    """Delete workouts from Garmin Connect, which takes them off its calendar too; the watch drops them at its next
    sync. One already gone on Garmin is skipped. Returns how many went."""
    client, gone = sync._client(), 0
    for i in ids:
        try:
            client.delete_workout(i)
            gone += 1
        except Exception as e:  # deleted on Garmin already, most likely
            log.info("Couldn't delete the workout %s: %s", i, e)
    return gone


def _forget_sent(conn, ids):
    """Take workouts out of both sent lists, so the page shows them as not sent."""
    for key in ("workouts_sent", "plan_sent"):
        kept = [x for x in json.loads(db.get_meta(conn, key) or "[]") if x["id"] not in ids]
        db.set_meta(conn, key, json.dumps(kept))


@app.delete("/api/workout/{sport}")
def remove_workout(sport: str):
    """Take today's workout for the sport off Garmin and the watch. Only ever called by the page's Remove from watch
    link."""
    if DEMO:
        return JSONResponse({"error": "Demo mode doesn't change anything on Garmin."}, status_code=400)
    today = date.today().isoformat()
    conn = db.connect(DB_PATH)
    try:
        ids = {x["id"] for x in json.loads(db.get_meta(conn, "workouts_sent") or "[]")
               if x["date"] == today and x.get("sport") == sport}
        ids |= {x["id"] for x in json.loads(db.get_meta(conn, "plan_sent") or "[]")
                if x["date"] == today and x.get("sport", "run") == sport}
        if not ids:
            return JSONResponse({"error": "Today's workout isn't on the watch."}, status_code=400)
        try:
            gone = _delete_from_garmin(ids)
        except Exception as e:
            log.error("Removing the workout failed: %s", e)
            return JSONResponse({"error": f"Couldn't reach Garmin: {e}"}, status_code=502)
        _forget_sent(conn, ids)
        conn.commit()
    finally:
        conn.close()
    return {"status": "removed", "count": gone}


@app.delete("/api/plan/send")
def remove_plan_week():
    """Take the plan's sessions from today on off Garmin and the watch. Only ever called by the page's Remove from
    watch link; sessions already past stay."""
    if DEMO:
        return JSONResponse({"error": "Demo mode doesn't change anything on Garmin."}, status_code=400)
    today = date.today().isoformat()
    conn = db.connect(DB_PATH)
    try:
        ids = {x["id"] for x in json.loads(db.get_meta(conn, "plan_sent") or "[]") if x["date"] >= today}
        if not ids:
            return JSONResponse({"error": "None of the plan's sessions are on the watch."}, status_code=400)
        try:
            gone = _delete_from_garmin(ids)
        except Exception as e:
            log.error("Removing the plan's sessions failed: %s", e)
            return JSONResponse({"error": f"Couldn't reach Garmin: {e}"}, status_code=502)
        _forget_sent(conn, ids)
        conn.commit()
    finally:
        conn.close()
    return {"status": "removed", "count": gone}


def _plan_sports(body):
    """The plan's sports from the form, checked: {sport: setup}. Runs on 2 days or more, with the long run on one of
    them; a long ride day, when given, on a ride day. A page from before plans had other sports sends runs only."""
    given = body.get("sports") or {"run": {k: body[k] for k in ("days", "long_day", "start_km")}}
    out = {}
    for sport, x in given.items():
        days = sorted({int(d) for d in x["days"]})
        assert sport in workouts.ALL_SPORTS and days and all(0 <= d <= 6 for d in days)
        if sport == "run":
            out[sport] = {"days": days, "long_day": int(x["long_day"]), "start_km": float(x["start_km"])}
            assert len(days) >= 2 and out[sport]["long_day"] in days and 0 < out[sport]["start_km"] <= 300
        elif sport == "bike":
            long_day = None if x.get("long_day") in (None, "") else int(x["long_day"])
            out[sport] = {"days": days, "long_day": long_day, "start_h": float(x["start_h"])}
            assert (long_day is None or long_day in days) and 0 < out[sport]["start_h"] <= 40
        else:
            out[sport] = {"days": days, "start_km": float(x["start_km"]), "pool": int(x.get("pool") or 25)}
            assert out[sport]["pool"] in (25, 50) and 0 < out[sport]["start_km"] <= 60
    return out


def _plan_from(body, old, profile, pred_5k, today):
    """The plan setup from the form, checked. Editing a plan keeps its start and starting fitness, so its history and
    adaptation carry on. A base builder, a return to running and a plan without runs have no distance (10 km sets
    their limits); a speed block is for 5 or 10 km; a triathlon's distance sets its run leg; only a race and a
    triathlon have a name, and only a race a target time."""
    goal = body["goal"]
    race, tri = goal == "race", body.get("tri") if goal == "triathlon" else None
    sports = _plan_sports(body)
    run = sports.get("run")
    named = str(body.get("name") or "").strip()[:80] if race or tri else ""
    cfg = {
        "goal": goal,
        "name": named or (f"{plans.TRI[tri]} triathlon" if tri else plans.GOALS[goal]),
        "metres": float(plans.TRI_RUN_M[tri]) if tri else 10000.0 if goal in ("base", "comeback") or not run else float(body["metres"]),
        "date": date.fromisoformat(body["date"]).isoformat(),
        "target_s": int(body["target_s"]) if race and body.get("target_s") else None,
        "sports": sports,
        # The run's, where the run's sessions are worked out from
        "days": run["days"] if run else [],
        "long_day": run["long_day"] if run else None,
        "start_km": run["start_km"] if run else 0,
        "start": old["start"] if old else today.isoformat(),
        "fitness0": old["fitness0"] if old else plans.fitness_now(profile, pred_5k),
    }
    if tri:
        cfg["tri"] = tri
    weeks = (date.fromisoformat(cfg["date"]) - date.fromisoformat(cfg["start"])).days / 7
    lo, hi = plans.WEEK_RANGE[goal]
    assert 1000 <= cfg["metres"] <= 100_000 and lo <= weeks <= hi and (goal != "speed" or cfg["metres"] in (5000, 10000))
    assert cfg["date"] > today.isoformat() and (cfg["target_s"] or 1) > 0
    assert plans.NEEDS.get(goal, set()) <= set(sports) and (goal != "triathlon" or tri in plans.TRI)
    return cfg


@app.post("/api/plan")
async def set_plan(request: Request):
    """Save the training plan: {goal, name, metres, date, target_s or null, days: [0-6, Monday first], long_day,
    start_km}. A race plan also sets the race goal, with the projected time as target when none is given."""
    body = await request.json()
    today = date.today()
    conn = db.connect(DB_PATH)
    try:
        old = json.loads(db.get_meta(conn, "training_plan") or "null")
        profile = json.loads(db.get_meta(conn, "training_profile") or "{}")
        row = conn.execute("SELECT pred_5k FROM daily WHERE pred_5k IS NOT NULL ORDER BY date DESC LIMIT 1").fetchone()
        try:
            cfg = _plan_from(body, old, profile, row and row["pred_5k"], today)
        except (KeyError, TypeError, ValueError, AssertionError):
            return JSONResponse({"error": "Give an end date within the plan type's length (a speed block 6 to 12 weeks, a "
                                          "return to running 4 to 16, a base builder 4 to 52, a triathlon 6 to 52, the "
                                          "others 2 to 52), a distance where it asks for one, and for each sport its "
                                          "days and how much you do now: at least 2 run days with the long run on one "
                                          "of them. A race, speed block and return to running need runs; a triathlon "
                                          "needs swims, rides and runs."}, status_code=400)
        if cfg["goal"] == "race":
            target = cfg["target_s"] or (plans.build(cfg, [], {}, profile, today)["projection"] or {}).get("s")
            if not target:
                return JSONResponse({"error": "Garmin has no threshold or race prediction yet to project a time from. "
                                              "Enter a target time."}, status_code=400)
            db.set_meta(conn, "race_goal", json.dumps({"name": cfg["name"], "date": cfg["date"], "metres": cfg["metres"],
                                                       "target_s": int(target)}))
        db.set_meta(conn, "training_plan", json.dumps(cfg))
        conn.commit()
    finally:
        conn.close()
    return {"status": "saved"}


@app.delete("/api/plan")
def remove_plan():
    """End the plan. The race goal stays."""
    conn = db.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM meta WHERE key IN ('training_plan', 'plan_sent')")
        conn.commit()
    finally:
        conn.close()
    return {"status": "removed"}


@app.post("/api/plan/send")
async def send_plan_week(request: Request):
    """Put the plan's next 7 days in the Garmin calendar. Only ever called by the page's Send week button, with the
    key of the week it shows, so sessions changed since (after a sync) aren't sent unseen. A session sent before and
    changed since (its paces adapted) replaces the old one on Garmin."""
    key = (await request.json()).get("key")
    plan = _dashboard_data()["plan"]
    today = date.today()
    week = plans.coming_week(plan, today) if plan else []
    if not week or plans.week_key(week) != key:
        return JSONResponse({"error": "The plan has changed. Look it over and press again."}, status_code=409)
    if DEMO:
        return JSONResponse({"error": "Demo mode doesn't send anything to Garmin."}, status_code=400)
    conn = db.connect(DB_PATH)
    try:
        sent = json.loads(db.get_meta(conn, "plan_sent") or "[]")
        client = sync._client()
        swim = plan["config"]["sports"].get("swim") or {}
        for s in week:
            if any(x["date"] == s["date"] and x["key"] == s["key"] for x in sent):
                continue
            for old in [x for x in sent if x["date"] == s["date"] and x.get("sport", "run") == s["sport"]]:
                try:
                    client.delete_workout(old["id"])
                except Exception as e:  # already deleted on Garmin, most likely
                    log.info("Couldn't delete the old workout %s: %s", old["id"], e)
                sent.remove(old)
            w = {"sport": s["sport"], "title": s["title"], "steps": s["steps"], "total_s": s["total_s"],
                 "why": [plan["config"]["name"]], "pool": swim.get("pool")}
            day = date.fromisoformat(s["date"])
            created = client.upload_workout(workouts.to_garmin(w, day))
            client.schedule_workout(created["workoutId"], s["date"])
            sent.append({"date": s["date"], "sport": s["sport"], "key": s["key"], "id": created["workoutId"]})
            db.set_meta(conn, "plan_sent", json.dumps(sent))  # saved per session, so a failure halfway keeps what went
            review.remember(conn, created["workoutId"], {**s, "plan": True}, day, review.plan_context(plan, s["date"]), today)
            conn.commit()
    except Exception as e:
        log.error("Sending the plan's week failed: %s", e)
        return JSONResponse({"error": f"Garmin didn't take the workouts: {e}"}, status_code=502)
    finally:
        conn.close()
    return {"status": "sent"}


def _review(activity_id):
    """(the activity's review, the activity), or (None, None) when it wasn't run from a workout the dashboard sent."""
    data = _dashboard_data()
    conn = db.connect(DB_PATH)
    try:
        row = conn.execute(f"SELECT {metrics.ACTIVITY_SELECT} FROM activities WHERE id = ?", (activity_id,)).fetchone()
        if not row:
            return None, None
        a = next((x for x in data["activities"] if x["id"] == activity_id), None)
        if not a:
            return None, None
        a = {**a, "workout_id": row["workout_id"], "aerobic_te": row["aerobic_te"], "anaerobic_te": row["anaerobic_te"]}
        session = review.planned(a, review.load_archive(conn), json.loads(db.get_meta(conn, "workouts_sent") or "[]"),
                                 json.loads(db.get_meta(conn, "plan_sent") or "[]"), data["plan"], data["workouts"])
        if not session:
            return None, None
        r = review.build(a, session, db.get_laps(conn, activity_id), data["plan"])
        r["note"] = review.get_note(conn, r)
        return r, a
    finally:
        conn.close()


@app.get("/api/review/{activity_id}")
def get_review(activity_id: int):
    """How a run went against the workout it was run from: each step against its lap, and the coach's note if it has
    been asked for before."""
    if DEMO:
        import demo
        demo.fill_details(DB_PATH, activity_id)
    else:
        sync.fetch_details(DB_PATH, activity_id)  # the laps, if they aren't stored yet
        sync.fetch_sent_workouts(DB_PATH)  # the workout it was run from, if it was sent before they were kept
    r, _ = _review(activity_id)
    if not r:
        return JSONResponse({"error": "This activity wasn't run from a workout the dashboard sent."}, status_code=404)
    return r


@app.post("/api/review/{activity_id}/note")
async def review_note(activity_id: int):
    """The coach's note on a reviewed run. Asked once; kept, so opening the review again costs nothing."""
    r, a = _review(activity_id)
    if not r:
        return JSONResponse({"error": "This activity wasn't run from a workout the dashboard sent."}, status_code=404)
    if r["note"]:
        return r["note"]
    if DEMO:
        return JSONResponse({"error": "Demo mode doesn't ask the coach."}, status_code=400)
    if not coach.backend():
        return JSONResponse({"error": "The coach needs Claude Code installed on the machine running the server, "
                                      "or ANTHROPIC_API_KEY in .env."}, status_code=400)
    text, error, used = await asyncio.to_thread(coach.ask, review.SYSTEM, review.prompt(r))
    if error:
        return JSONResponse({"error": error}, status_code=502)
    note = {"text": text, "usage": {k: used[k] if used[k] is None else round(used[k], 6)
                                    for k in ("tokens", "cost", "haiku", "sonnet")} if used else None}
    conn = db.connect(DB_PATH)
    try:
        review.save_note(conn, r, note)
        conn.commit()
    finally:
        conn.close()
    return note


@app.get("/api/chat")
def chat_log():
    """Today's chat on the Workout page, and whether it can run (it needs Claude Code on this machine or
    ANTHROPIC_API_KEY), with what the API has cost this month."""
    return {"enabled": coach.backend() is not None, "log": coach.history(DB_PATH), **coach.usage()}


@app.post("/api/chat")
async def chat(request: Request):
    """One message to the chat: {text}. Claude may change sessions while answering."""
    text = str((await request.json()).get("text") or "").strip()[:2000]
    if not text:
        return JSONResponse({"error": "Write a message first."}, status_code=400)
    if not coach.backend():
        return JSONResponse({"error": "The chat needs Claude Code installed on the machine running the server, "
                                      "or ANTHROPIC_API_KEY in .env."}, status_code=400)
    return await asyncio.to_thread(coach.send, DB_PATH, text, _dashboard_data)


@app.delete("/api/chat")
def new_chat():
    """Start the chat over. Changes it made stay."""
    coach.clear(DB_PATH)
    return {"status": "cleared"}


@app.delete("/api/changes")
def undo_changes():
    """Take back every change the chat made from today on: the suggested and planned sessions return."""
    conn = db.connect(DB_PATH)
    try:
        today = date.today()
        chat_changes.save(conn, [c for c in chat_changes.load(conn) if c["date"] < today.isoformat()], today)
        conn.commit()
    finally:
        conn.close()
    return {"status": "removed"}


@app.post("/api/sync")
def sync_now():
    if DEMO:
        return JSONResponse({"status": "Demo mode doesn't sync with Garmin."})
    if sync.is_running():
        return {"status": "already running"}
    threading.Thread(target=sync.run_sync, args=(DB_PATH,), daemon=True).start()
    return {"status": "started"}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    print(f"\n  Dashboard on this machine:  http://localhost:{config.PORT}")
    if config.HOST == "0.0.0.0":
        print(f"  From other devices on wifi: http://{lan_ip()}:{config.PORT}\n")
    if config.HOST not in ("127.0.0.1", "localhost") and not config.DASHBOARD_PASSWORD:
        print("  WARNING: anyone on this network can open the dashboard and see your health data.\n"
              "  Set DASHBOARD_PASSWORD in .env to ask for a password.\n")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
