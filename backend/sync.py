"""Pull activities and daily recovery data from Garmin Connect into SQLite.

Run on its own with `python backend/sync.py`, or let app.py run it on a timer.
"""
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta

from garminconnect import Garmin

import config
import db
import metrics

log = logging.getLogger("sync")
_lock = threading.Lock()
ACTIVITY_PAGE = 100  # activities per request when fetching the full history
TRACK_ACTIVITIES = 5  # GPS tracks are fetched for this many of the newest activities
HEALTH_DAYS_PER_SYNC = 45  # the health backfill fetches this many days per sync, so one sync never runs for long
EFFORT_RUNS_PER_SYNC = 40  # runs whose best efforts are worked out per sync (one request each)
PLAN_LAPS_PER_SYNC = 20  # runs in the training plan whose laps are fetched per sync
RECHECK_DAYS = 30  # once a day, activities this far back are fetched again, to pick up edits and deletions on Garmin
EFFORT_DISTANCES = tuple(metrics.DISTANCES)


def _get(d, *path):
    """Safe nested lookup: _get(x, "a", "b") -> x["a"]["b"] or None."""
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _client():
    client = Garmin()
    client.login(config.TOKEN_DIR)
    return client


def parse_activity(a):
    start = a.get("startTimeLocal") or ""
    return {
        "id": a.get("activityId"),
        "start_local": start,
        "date": start[:10],
        "type": _get(a, "activityType", "typeKey") or "other",
        "name": a.get("activityName"),
        "distance_m": a.get("distance"),
        "duration_s": a.get("movingDuration") or a.get("duration"),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "avg_speed": a.get("averageSpeed"),
        "elev_gain": a.get("elevationGain"),
        "training_load": a.get("activityTrainingLoad"),
        "aerobic_te": a.get("aerobicTrainingEffect"),
        "anaerobic_te": a.get("anaerobicTrainingEffect"),
        "te_label": a.get("trainingEffectLabel"),
        "raw": a,
    }


def _try(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # one missing endpoint shouldn't sink the whole day
        if "429" in str(e) or "Too many" in str(e):
            raise
        log.debug("%s%s failed: %s", fn.__name__, args, e)
        return None


def _primary(by_device):
    """Garmin keys some metrics by device; prefer the primary training device."""
    rows = [r for r in (by_device or {}).values() if isinstance(r, dict)]
    return next((r for r in rows if r.get("primaryTrainingDevice")), rows[0] if rows else {})


def fetch_training_status(client, day):
    ts = _try(client.get_training_status, day.isoformat()) or {}
    s = _primary(_get(ts, "mostRecentTrainingStatus", "latestTrainingStatusData"))
    acute = s.get("acuteTrainingLoadDTO") or {}
    lb = _primary(_get(ts, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap"))
    # The phrase ("PRODUCTIVE_3", "RECOVERY_2", ...) names the status; the suffix is just a message variant.
    phrase = s.get("trainingStatusFeedbackPhrase")
    status = "PAUSED" if s.get("trainingPaused") else re.sub(r"_\d+$", "", phrase) if phrase else None
    return {
        "training_status": status,
        "training_status_since": s.get("sinceDate"),
        "acute_load": acute.get("dailyTrainingLoadAcute"),
        "acute_load_min": acute.get("minTrainingLoadChronic"),
        "acute_load_max": acute.get("maxTrainingLoadChronic"),
        "acwr_status": acute.get("acwrStatus"),
        "vo2max": _get(ts, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue"),
        # Load focus: the last four weeks of load per zone, against Garmin's optimal range for each
        "load_low": lb.get("monthlyLoadAerobicLow"),
        "load_low_min": lb.get("monthlyLoadAerobicLowTargetMin"),
        "load_low_max": lb.get("monthlyLoadAerobicLowTargetMax"),
        "load_high": lb.get("monthlyLoadAerobicHigh"),
        "load_high_min": lb.get("monthlyLoadAerobicHighTargetMin"),
        "load_high_max": lb.get("monthlyLoadAerobicHighTargetMax"),
        "load_anaerobic": lb.get("monthlyLoadAnaerobic"),
        "load_anaerobic_min": lb.get("monthlyLoadAnaerobicTargetMin"),
        "load_anaerobic_max": lb.get("monthlyLoadAnaerobicTargetMax"),
        "load_focus": lb.get("trainingBalanceFeedbackPhrase"),
    }


def fetch_profile(client):
    """Lactate threshold and heart rate zones, for the workout targets: {lt_speed (m/s), lt_hr, zones: [zone 1-5 floors,
    max HR]}, or None when Garmin has neither."""
    lt = (_try(client.get_lactate_threshold) or {}).get("speed_and_heart_rate") or {}
    speed = lt.get("speed")
    # Garmin gives the threshold speed in tenths of m/s (0.30 for 3.0 m/s, 5:30 /km)
    if speed and speed < 1:
        speed *= 10
    rows = [r for r in (_try(client.get_heart_rate_zones) or []) if isinstance(r, dict)]
    z = next((r for r in rows if r.get("sport") == "RUNNING"), None) or next((r for r in rows if r.get("sport") == "DEFAULT"), None)
    zones = [z.get(f"zone{i}Floor") for i in range(1, 6)] + [z.get("maxHeartRateUsed")] if z else None
    profile = {"lt_speed": speed, "lt_hr": lt.get("heartRate"), "zones": zones if zones and all(zones) else None,
               "ftp": _ftp(_try(client.get_cycling_ftp))}
    return profile if any(profile.values()) else None


def _ftp(answer):
    """Cycling FTP in watts from Garmin's answer (one entry, or a list of them), or None."""
    entry = (answer[0] if answer else None) if isinstance(answer, list) else answer
    ftp = entry.get("functionalThresholdPower") if isinstance(entry, dict) else None
    return round(ftp) if isinstance(ftp, (int, float)) and 50 <= ftp <= 600 else None


def fetch_track(client, activity_id):
    """The activity's GPS points as [[lat, lon], ...], or None if the request failed."""
    details = _try(client.get_activity_details, activity_id, 1, 1000)
    if details is None:
        return None
    points = _get(details, "geoPolylineDTO", "polyline") or []
    return [[round(p["lat"], 6), round(p["lon"], 6)] for p in points
            if isinstance(p, dict) and p.get("lat") is not None and p.get("lon") is not None]


def fetch_laps(client, activity_id):
    """The activity's laps, or None if the request failed."""
    splits = _try(client.get_activity_splits, activity_id)
    if splits is None:
        return None
    return [{
        "km": round((l.get("distance") or 0) / 1000, 3),
        "duration_s": l.get("movingDuration") or l.get("duration"),
        "speed": l.get("averageMovingSpeed") or l.get("averageSpeed"),
        "max_speed": l.get("maxSpeed"),
        "avg_hr": l.get("averageHR"),
        "max_hr": l.get("maxHR"),
        "elev_gain": l.get("elevationGain"),
        "elev_loss": l.get("elevationLoss"),
        "cadence": l.get("averageRunCadence") or l.get("averageBikeCadence"),
        "power": l.get("averagePower"),
    } for l in splits.get("lapDTOs") or [] if isinstance(l, dict)]


def best_efforts(samples, distances=EFFORT_DISTANCES):
    """The fastest time over each distance within one activity, from (elapsed seconds, metres) samples.
    The start of each window is interpolated between samples, so the span is exactly the distance."""
    out = {}
    for m in distances:
        if not samples or samples[-1][1] - samples[0][1] < m:
            continue
        best, j = None, 0
        for i, (t, d) in enumerate(samples):
            if d - samples[0][1] < m:
                continue
            while samples[j + 1][1] <= d - m:
                j += 1
            (t0, d0), (t1, d1) = samples[j], samples[j + 1]
            start = t0 + (t1 - t0) * ((d - m - d0) / (d1 - d0) if d1 > d0 else 0)
            if best is None or t - start < best:
                best = t - start
        out[m] = round(best, 1)
    return out


def fetch_efforts(client, activity_id):
    """A run's best efforts from its second-by-second data, or None if the request failed."""
    details = _try(client.get_activity_details, activity_id, 100000, 1)
    if details is None:
        return None
    keys = {m.get("key"): m.get("metricsIndex") for m in details.get("metricDescriptors") or []}
    # Elapsed time, as Garmin's own fastest splits use: a stop in the middle of a 5 km counts against it
    ti, di = keys.get("sumElapsedDuration", keys.get("sumDuration")), keys.get("sumDistance")
    if ti is None or di is None:
        return {}
    samples, last = [], (-1, -1)
    for row in details.get("activityDetailMetrics") or []:
        vals = row.get("metrics") or []
        t, d = (vals[ti], vals[di]) if len(vals) > max(ti, di) else (None, None)
        if t is None or d is None or t < last[0] or d < last[1]:
            continue  # gaps and the odd backwards step from GPS corrections
        samples.append((t, d))
        last = (t, d)
    return best_efforts(samples)


_shared_client = None  # logged in once and kept, for activities opened on the page


def fetch_details(db_path, activity_id):
    """Fetch an activity's GPS track and laps from Garmin if they aren't stored yet, and a run's best efforts.
    Returns None when all is well, or a message saying why they couldn't be fetched."""
    global _shared_client
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT json_extract(raw, '$.hasPolyline') AS gps, type FROM activities WHERE id = ?",
                           (activity_id,)).fetchone()
        if not row:
            return None
        need_track = bool(row["gps"]) and db.get_track(conn, activity_id) is None
        laps = db.get_laps(conn, activity_id)
        # Laps stored before max speed and descent were added are fetched again to get them
        need_laps = laps is None or (laps and "max_speed" not in laps[0])
        # A run synced since the last batch of best efforts gets them now, so its 400 m shows straight away
        need_efforts = "running" in (row["type"] or "") and db.get_efforts(conn, activity_id) is None
        if not (need_track or need_laps or need_efforts):
            return None
        _shared_client = _shared_client or _client()
        if need_track:
            points = fetch_track(_shared_client, activity_id)
            if points is not None:  # a failed request is retried the next time the activity is opened
                db.set_track(conn, activity_id, points)
        if need_laps:
            laps = fetch_laps(_shared_client, activity_id)
            if laps is not None:
                db.set_laps(conn, activity_id, laps)
        if need_efforts:
            bests = fetch_efforts(_shared_client, activity_id)
            if bests is not None:
                db.set_efforts(conn, activity_id, bests)
        conn.commit()
        return None
    except Exception as e:
        _shared_client = None  # log in again next time
        msg = str(e) or e.__class__.__name__
        if "Username and password are required" in msg or "garth" in msg:
            msg = "not logged in to Garmin"
        log.warning("Fetching details for %s failed: %s", activity_id, msg)
        return f"Couldn't fetch the route and laps from Garmin ({msg})."
    finally:
        conn.close()


def fetch_vo2max_history(client, start, end):
    """One row per day Garmin updated the VO2 max estimate."""
    rows = _try(client.get_max_metrics_range, start.isoformat(), end.isoformat()) or []
    out = []
    for r in rows:
        g = _get(r, "generic") or {}
        if g.get("calendarDate") and g.get("vo2MaxPreciseValue"):
            out.append({"date": g["calendarDate"], "vo2max": g["vo2MaxPreciseValue"]})
    return out


PREDICTIONS = {"time5K": "pred_5k", "time10K": "pred_10k", "timeHalfMarathon": "pred_half", "timeMarathon": "pred_marathon"}


def fetch_predictions(client, start, end):
    """Garmin's race predictions (5 km to marathon) for each day from start to end (at most a year)."""
    rows = _try(client.get_race_predictions, start.isoformat(), end.isoformat(), "daily") or []
    return [{"date": r["calendarDate"], **{col: r.get(key) for key, col in PREDICTIONS.items()}}
            for r in rows if isinstance(r, dict) and r.get("calendarDate")]


def _calories(stats):
    return {"cal_total": stats.get("totalKilocalories"), "cal_active": stats.get("activeKilocalories"),
            "cal_resting": stats.get("bmrKilocalories")}


def fetch_day(client, day):
    d = day.isoformat()
    stats = _try(client.get_stats, d) or {}
    hrv = _try(client.get_hrv_data, d) or {}
    sleep = _try(client.get_sleep_data, d) or {}
    ready = _try(client.get_training_readiness, d)

    # Garmin can return several scores per day (e.g. last evening's update and the morning one); take the newest.
    if isinstance(ready, dict):
        ready = [ready]
    scored = [r for r in ready or [] if isinstance(r, dict) and r.get("score") is not None]
    readiness = max(scored, key=lambda r: r.get("timestamp") or "")["score"] if scored else None
    # Recovery time (minutes) counts down through the day and jumps after a workout: the day is planned from the
    # wake-up value, or the newest one before it
    timed = sorted((r for r in ready or [] if isinstance(r, dict) and r.get("recoveryTime") is not None),
                   key=lambda r: r.get("timestamp") or "")
    woke = [r for r in timed if r.get("inputContext") == "AFTER_WAKEUP_RESET"]
    recovery = (woke or timed)[-1]["recoveryTime"] if timed else None

    hs = hrv.get("hrvSummary") or {}
    sleep_dto = sleep.get("dailySleepDTO") or {}
    return {
        "date": d,
        "resting_hr": stats.get("restingHeartRate"),
        "bb_high": stats.get("bodyBatteryHighestValue"),
        "bb_low": stats.get("bodyBatteryLowestValue"),
        "stress_avg": stats.get("averageStressLevel"),
        "steps": stats.get("totalSteps"),
        **_calories(stats),
        "hrv_last_night": hs.get("lastNightAvg"),
        "hrv_weekly_avg": hs.get("weeklyAvg"),
        "hrv_low": _get(hs, "baseline", "balancedLow"),
        "hrv_high": _get(hs, "baseline", "balancedUpper"),
        "hrv_status": hs.get("status"),
        "sleep_s": sleep_dto.get("sleepTimeSeconds"),
        "sleep_score": _get(sleep_dto, "sleepScores", "overall", "value"),
        "readiness": readiness,
        "recovery_h": round(recovery / 60, 1) if recovery is not None else None,
        **fetch_training_status(client, day),
    }


def run_sync(db_path=None):
    """Sync new data. Returns a short status string.

    The steps run in order of importance, cheapest first: new activities, today's numbers, then the one-off backfills.
    If Garmin rate-limits a step, the ones after it wait for the next sync; every backfill saves its own progress."""
    if not _lock.acquire(blocking=False):
        return "already running"
    conn = db.connect(db_path or config.DB_PATH)
    try:
        today = date.today()
        last = db.get_meta(conn, "last_sync_date")
        last = date.fromisoformat(last) if last else None
        act_start = last - timedelta(days=3) if last else today - timedelta(days=config.ACTIVITY_BACKFILL_DAYS)
        day_start = last - timedelta(days=2) if last else today - timedelta(days=config.DAILY_BACKFILL_DAYS)

        client = _client()

        # Once a day, look further back, so activities edited or deleted on Garmin since are changed here too
        # (the first sync looks further back anyway, and counts as today's)
        recheck = db.get_meta(conn, "activities_rechecked") != today.isoformat()
        if recheck:
            act_start = min(act_start, today - timedelta(days=RECHECK_DAYS))
        activities = client.get_activities_by_date(act_start.isoformat(), today.isoformat())
        for a in activities:
            db.upsert_activity(conn, parse_activity(a))
        log.info("Synced %d activities since %s", len(activities), act_start)
        if recheck and activities:  # an empty answer may be a failed request, so nothing is deleted on it
            seen = {a.get("activityId") for a in activities}
            gone = [r["id"] for r in conn.execute("SELECT id FROM activities WHERE date >= ?", (act_start.isoformat(),))
                    if r["id"] not in seen]
            db.delete_activities(conn, gone)
            if gone:
                log.info("Removed %d activities deleted on Garmin", len(gone))
            db.set_meta(conn, "activities_rechecked", today.isoformat())
        conn.commit()

        # Race predictions: the last year once (one request), then the days since the last sync
        pred_start = day_start if db.get_meta(conn, "predictions_backfilled") else today - timedelta(days=364)
        rows = fetch_predictions(client, pred_start, today)
        for row in rows:
            db.upsert_daily(conn, row)
        if rows:
            db.set_meta(conn, "predictions_backfilled", "1")
        conn.commit()

        # Lactate threshold and heart rate zones for the workout page, once a day
        if db.get_meta(conn, "profile_date") != today.isoformat():
            profile = fetch_profile(client)
            if profile:  # a failed request keeps the last one
                db.set_meta(conn, "training_profile", json.dumps(profile))
                db.set_meta(conn, "profile_date", today.isoformat())
                conn.commit()

        # VO2 max history for its chart: one request covers years, so fetch it once.
        if not db.get_meta(conn, "vo2max_backfilled"):
            start = today - timedelta(days=config.VO2MAX_BACKFILL_DAYS)
            rows = fetch_vo2max_history(client, start, today)
            for row in rows:
                db.upsert_daily(conn, row)
            if rows:  # an empty answer may be a failed request; try again next sync
                db.set_meta(conn, "vo2max_backfilled", "1")
            conn.commit()

        # The daily numbers since the last sync. Once they're in, the next sync starts from here, even if a backfill
        # below is cut short.
        day = day_start
        while day <= today:
            db.upsert_daily(conn, fetch_day(client, day))
            conn.commit()
            day += timedelta(days=1)
            time.sleep(0.4)  # be gentle with Garmin's rate limits
        db.set_meta(conn, "last_sync_date", today.isoformat())
        conn.commit()

        # GPS tracks for the newest activities, for the map in the last activity panel
        missing = conn.execute(
            "SELECT id FROM (SELECT id, raw FROM activities ORDER BY start_local DESC LIMIT ?) "
            "WHERE json_extract(raw, '$.hasPolyline') = 1 AND id NOT IN (SELECT activity_id FROM tracks)",
            (TRACK_ACTIVITIES,)).fetchall()
        for row in missing:
            points = fetch_track(client, row["id"])
            if points is not None:  # a failed request is retried next sync
                db.set_track(conn, row["id"], points)
                conn.commit()

        # Laps of the training plan's runs, so the plan can see how the intervals went
        plan = json.loads(db.get_meta(conn, "training_plan") or "null")
        if plan:
            todo = conn.execute(
                "SELECT id FROM activities WHERE type LIKE '%running%' AND date >= ? "
                "AND id NOT IN (SELECT activity_id FROM laps) ORDER BY start_local DESC LIMIT ?",
                (plan["start"], PLAN_LAPS_PER_SYNC)).fetchall()
            for row in todo:
                laps = fetch_laps(client, row["id"])
                if laps is not None:  # a failed request is retried next sync
                    db.set_laps(conn, row["id"], laps)
                    conn.commit()

        # Best efforts (400 m to marathon) for runs that don't have them yet, newest first, a batch per sync
        todo = conn.execute(
            "SELECT id FROM activities WHERE type LIKE '%running%' AND id NOT IN (SELECT activity_id FROM efforts) "
            "ORDER BY start_local DESC LIMIT ?", (EFFORT_RUNS_PER_SYNC,)).fetchall()
        for row in todo:
            bests = fetch_efforts(client, row["id"])
            if bests is not None:  # a failed request is retried next sync
                db.set_efforts(conn, row["id"], bests)
                conn.commit()
            time.sleep(0.5)

        # Full activity history: page back through everything on Garmin once, newest first.
        # The offset is saved per page, so a rate limit just resumes from there on the next sync.
        if not db.get_meta(conn, "activity_history_done"):
            offset = int(db.get_meta(conn, "activity_history_offset") or 0)
            while True:
                page = client.get_activities(offset, ACTIVITY_PAGE)
                page = page if isinstance(page, list) else []
                for a in page:
                    db.upsert_activity(conn, parse_activity(a))
                offset += len(page)
                db.set_meta(conn, "activity_history_offset", str(offset))
                conn.commit()
                if len(page) < ACTIVITY_PAGE:
                    break
                time.sleep(1)
            db.set_meta(conn, "activity_history_done", "1")
            conn.commit()
            log.info("Activity history complete: %d activities", offset)

        # Health history: a year of daily data (HRV, sleep, Body Battery...), fetched once, backwards from the oldest
        # day already synced. A chunk per sync, saving progress per day, so a rate limit just resumes on the next sync.
        oldest = db.get_meta(conn, "health_backfill_from") or conn.execute(
            "SELECT min(date) FROM daily WHERE resting_hr IS NOT NULL OR sleep_s IS NOT NULL").fetchone()[0]
        day = date.fromisoformat(oldest) if oldest else day_start
        target = today - timedelta(days=config.HEALTH_HISTORY_DAYS)
        for _ in range(HEALTH_DAYS_PER_SYNC):
            if day <= target:
                break
            day -= timedelta(days=1)
            db.upsert_daily(conn, fetch_day(client, day))
            db.set_meta(conn, "health_backfill_from", day.isoformat())
            conn.commit()
            time.sleep(0.4)

        # Calories for the days synced before they were stored: once, backwards from the newest day without them,
        # a chunk per sync (one request a day), saving its place after every day
        start = db.get_meta(conn, "calories_backfill_from")
        day = date.fromisoformat(start) if start else day_start
        done = 0
        while day > target and done < HEALTH_DAYS_PER_SYNC:
            day -= timedelta(days=1)
            row = conn.execute("SELECT cal_total FROM daily WHERE date = ?", (day.isoformat(),)).fetchone()
            if row and row["cal_total"] is None:
                stats = _try(client.get_stats, day.isoformat()) or {}
                db.upsert_daily(conn, {"date": day.isoformat(), **_calories(stats)})
                done += 1
                time.sleep(0.4)
            db.set_meta(conn, "calories_backfill_from", day.isoformat())
            conn.commit()

        db.set_meta(conn, "last_sync_at", datetime.now().isoformat(timespec="seconds"))
        db.set_meta(conn, "last_error", "")
        conn.commit()
        return "ok"
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        if "Username and password are required" in msg or "garth" in msg:
            msg = "Not logged in to Garmin. Run `python backend/login.py` on the server machine."
        log.error("Sync failed: %s", msg)
        db.set_meta(conn, "last_error", msg)
        db.set_meta(conn, "last_error_at", datetime.now().isoformat(timespec="seconds"))
        conn.commit()
        return f"error: {msg}"
    finally:
        conn.close()
        _lock.release()


def is_running():
    return _lock.locked()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(run_sync())
