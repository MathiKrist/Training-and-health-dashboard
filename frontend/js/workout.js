// Workout: today's suggested session per sport (worked out on the server), and the button that sends it to the watch

import { data } from "./state.js";
import { $, fmtDay, fmtDuration, fmtPace, isoDay, escapeHtml } from "./util.js";
import { lastCard } from "./overview.js";
import { routeMap } from "./map.js";
import { paragraphs, usage } from "./chat.js";

// Volt for easy running, Puls for hard (darker for threshold); a long run's easy steps take the long run's full Volt
const COLOR = { easy: "var(--k-easy)", high: "var(--k-threshold)", anaerobic: "var(--k-vo2)", rest: "var(--k-jog)" };
const color = (intensity, kind) => (intensity === "easy" && kind === "long" ? "var(--k-long)"
  : intensity === "high" && kind === "vo2" ? "var(--k-vo2)" : intensity === "anaerobic" && kind === "anaerobic" ? "var(--k-anaerobic)"
  : COLOR[intensity]);
const HEIGHT = { rest: 30, easy: 50, high: 80, anaerobic: 100 };

let sport = null;

try { sport = localStorage.getItem("workoutSport"); } catch {}

export const total = (s) => { const m = Math.round(s / 60); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`; };

export const pace = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

const len = (s) => (s < 60 ? `${s} s` : s % 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} min` : `${s / 60} min`);

function target(step) {
  const t = step.target, parts = [];
  if (t?.type === "pace") parts.push(`${pace(t.fast)}–${pace(t.slow)} /km`);
  if (t?.type === "power") parts.push(`${t.lo}–${t.hi} W`);
  if (t?.type === "hr") parts.push(`Zone ${t.zone} · ${t.lo}–${t.hi} bpm`);
  if (step.note) parts.push(step.note);
  return parts.join(" · ") || (step.intensity === "rest" || step.kind === "warmup" || step.kind === "cooldown" ? "Easy, by feel" : "");
}

// A swim's steps go by distance
const stepItem = (s, kind) => `<li style="--c:${color(s.intensity, kind)}"><span class="len">${s.m ? `${s.m} m` : len(s.s)}</span>
  <span class="what">${escapeHtml(s.label)}</span><span class="target">${target(s)}</span></li>`;

// The steps in order, with each repeat once per round, for the workout's shape
const flat = (steps) => steps.flatMap((s) => (s.kind === "repeat" ? Array(s.n).fill(s.steps).flat() : [s]));

// The workout's shape: each block as long as the step and as tall as it's hard
export const barHtml = (steps, kind) => flat(steps).map((s) =>
  `<i style="flex:${s.s};height:${HEIGHT[s.intensity]}%;background:${color(s.intensity, kind)}"></i>`).join("");

export const stepsHtml = (steps, kind) => steps.map((s) => (s.kind === "repeat"
  ? `<li class="reps" style="--c:${color(s.steps[0].intensity, kind)}"><span class="len">${s.n} ×</span><ol>${s.steps.map((x) => stepItem(x, kind)).join("")}</ol></li>`
  : stepItem(s, kind))).join("");

// The pace (or power) the session is about: its first hard step's, else its first step's
export function mainPace(steps) {
  const all = flat(steps), s = all.find((x) => x.intensity === "high") || all[0], t = s?.target;
  return t?.type === "pace" ? `${pace(t.fast)}–${pace(t.slow)} /km` : t?.type === "power" ? `${t.lo}–${t.hi} W` : "";
}

export const currentSport = () => sport;

export const todaysWorkout = () => (data.workouts || []).find((x) => x.sport === sport);

// Switch the sport shown (a plan session of another sport picked for today)
export function setSport(s) {
  if (s === sport || !(data.workouts || []).some((w) => w.sport === s)) return;
  sport = s;
  try { localStorage.setItem("workoutSport", sport); } catch {}
  renderWorkout();
  dispatchEvent(new Event("sportchange"));
}

export function renderWorkout() {
  const list = data.workouts || [];
  if (!list.some((w) => w.sport === sport)) sport = list[0]?.sport;
  const w = todaysWorkout();
  $("workoutSport").innerHTML = list.map((x) => `<button type="button" data-sport="${x.sport}" aria-pressed="${x.sport === sport}">${x.label}</button>`).join("");
  $("workout").hidden = !w;
  if (w) showWorkout(w);
  renderLast();
}

// ---- The sport's newest activity, at the top, and the review of a run done from a workout the dashboard sent

export const lastWorkoutMap = routeMap($("wkLastMap"));
const NOUN = { run: "run", bike: "ride", swim: "swim" };
const RECENT_MS = 12 * 3600e3;
let reviewFor = null;  // the activity whose review is open

// Only an activity from today, or one that ended in the last 12 hours, says anything about today's training. Checked
// against this device's clock, as the server's numbers are worked out once a day.
function recent(a) {
  if (!a) return false;
  if (a.date === isoDay(new Date())) return true;
  const end = new Date(a.start.replace(" ", "T")).getTime() + (a.duration_s || 0) * 1000;
  return Date.now() - end < RECENT_MS;
}

function renderLast() {
  const a = data.last_by_sport?.[sport];
  const shown = recent(a) ? a : null;
  lastCard($("wkLast"), shown, lastWorkoutMap, `Last ${NOUN[sport] || "activity"}`);
  const btn = $("wkReviewBtn");
  btn.hidden = !shown?.review;
  if (!shown?.review || shown.id !== reviewFor) closeReview();
  btn.dataset.noun = NOUN[sport];
  btn.textContent = reviewFor ? "Hide review" : `Review ${NOUN[sport]} ›`;
}

function closeReview() {
  reviewFor = null;
  $("wkReview").hidden = true;
  $("wkReview").innerHTML = "";
  $("wkReviewBtn").setAttribute("aria-expanded", "false");
}

$("wkReviewBtn").addEventListener("click", () => {
  const btn = $("wkReviewBtn");
  if (reviewFor) {
    closeReview();
    btn.textContent = `Review ${btn.dataset.noun} ›`;
    return;
  }
  const a = data.last_by_sport[sport];
  reviewFor = a.id;
  btn.textContent = "Hide review";
  btn.setAttribute("aria-expanded", "true");
  openReview(a.id);
});

const getJson = async (url, method = "GET") => {
  try {
    const res = await fetch(url, { method });
    const out = await res.json().catch(() => ({}));
    return res.ok ? out : { error: out.error || "That didn't work." };
  } catch {
    return { error: "Couldn't reach the dashboard server." };
  }
};

// The numbers straight away, then the coach's note (asked for the first time only; kept after)
async function openReview(id) {
  const panel = $("wkReview");
  panel.hidden = false;
  panel.innerHTML = '<p class="pending">Loading the laps…</p>';
  const r = await getJson(`/api/review/${id}`);
  if (reviewFor !== id) return;
  if (r.error) { panel.innerHTML = `<p class="error">${escapeHtml(r.error)}</p>`; return; }
  panel.innerHTML = reviewHtml(r) + `<div class="wk-review-note">${r.note ? noteHtml(r.note) : '<p class="pending">Asking the coach…</p>'}</div>`;
  if (r.note) return;
  const note = await getJson(`/api/review/${id}/note`, "POST");
  if (reviewFor !== id) return;
  panel.querySelector(".wk-review-note").innerHTML = note.error ? `<p class="error">${escapeHtml(note.error)}</p>` : noteHtml(note);
}

const noteHtml = (n) => paragraphs(n.text || "") + usage(n.usage);

const VERDICT = { better: "Better", worse: "Worse", "on plan": "On plan" };
const UNIT = { pace: "s/km", power: "W", hr: "bpm" };

function offCell(r) {
  if (r.off == null) return "<td></td>";
  if (r.off === 0) return '<td class="ok">✓</td>';
  return `<td class="off">${r.off} ${UNIT[r.target.type]} ${r.side}</td>`;
}

function reviewHtml(r) {
  const stat = (v, label) => (v ? `<div><dd>${v}</dd><dt>${label}</dt></div>` : "");
  const effect = r.effect.label && data.effects[r.effect.label] ? data.effects[r.effect.label][0] : "";
  const stats = [
    r.on_target && stat(`${r.on_target[0]}<span>of ${r.on_target[1]}</span>`, `${r.reps ? "reps" : "steps"} on target`),
    stat(VERDICT[r.verdict], "plan verdict"),
    r.projection && stat(`${fmtDuration(r.projection.from_s)} → ${fmtDuration(r.projection.to_s)}`, "projected time"),
    stat(effect && `${effect}${r.effect.aerobic ? `<span>${r.effect.aerobic.toFixed(1)}</span>` : ""}`, "Garmin training effect"),
  ].filter(Boolean).join("");
  const done = (x) => (r.sport === "run" ? fmtPace(x.speed && 1000 / x.speed)
    : x.power ? `${Math.round(x.power)} W` : x.speed ? `${(x.speed * 3.6).toFixed(1)} km/h` : "");
  const rows = r.rows.map((x) => `<tr><td>${escapeHtml(x.label)}</td><td class="num">${x.m ? `${x.m} m` : len(x.planned_s)}</td>
    <td>${target(x)}</td><td class="num">${fmtDuration(x.done_s)}</td><td class="num">${done(x)}</td>
    <td class="num">${x.hr ? Math.round(x.hr) : ""}</td>${offCell(x)}</tr>`).join("");
  return `<p class="wk-review-title">${escapeHtml(r.title)}${r.why.length ? ` · ${escapeHtml(r.why.join(" · "))}` : ""}</p>
    ${stats ? `<dl class="last-stats">${stats}</dl>` : ""}
    ${r.paired ? "" : `<p class="wk-review-why">The laps don't line up with the steps, so this is the whole session.</p>`}
    <div class="scroll"><table class="laps review"><thead><tr><th>Step</th><th class="num">Planned</th><th>Target</th>
      <th class="num">Time</th><th class="num">${r.sport === "run" ? "Pace" : "Power"}</th><th class="num">HR</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

// One session in the detail: today's workout, or a later plan day picked in the strip (sent with the week, not on its own)
export function showWorkout(w, day = null) {
  const today = !day;
  $("wkWhen").textContent = `${today ? "Today" : fmtDay(day)} · ${w.label || "Run"}`;
  $("wkTitle").textContent = w.title;
  $("wkTotal").textContent = w.kind === "rest" ? (w.next ? `Next: ${w.next.title}, ${fmtDay(w.next.date)}` : "")
    : (w.total_s ? total(w.total_s) : "") + (w.km ? ` · about ${w.km.toFixed(1)} km` : "");
  $("wkWhy").textContent = w.why.join(" · ");
  // Later days changed in the chat, outside a plan (the plan shows its own)
  const ahead = today ? (data.workouts_ahead || []).filter((x) => x.sport === w.sport) : [];
  $("wkAhead").hidden = !ahead.length;
  $("wkAhead").innerHTML = ahead.map((x) => `<li>${fmtDay(x.date)} · <b>${escapeHtml(x.title)}</b>${x.total_s ? ` · ${total(x.total_s)}` : ""}</li>`).join("");
  $("wkBar").innerHTML = barHtml(w.steps, w.kind);
  $("wkBar").setAttribute("aria-label", `${w.title}, ${total(w.total_s)}`);
  $("wkSteps").innerHTML = stepsHtml(w.steps, w.kind);
  // A rest day or race day has nothing to send
  $("wkSend").parentElement.hidden = !today || !w.steps.length;

  const btn = $("wkSend"), note = $("wkSendNote");
  watchButton(btn, w.sent, "Send to watch");
  note.className = "";
  note.textContent = w.sent ? "In today's Garmin calendar" : "Adds it to today's Garmin calendar";
}

$("workoutSport").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-sport]");
  if (b) setSport(b.dataset.sport);
});

// A send button: "Send …" until sent, then "On the watch ✓", which turns into "Remove from watch" on hover or focus
export function watchButton(btn, onWatch, sendText) {
  btn.disabled = false;
  btn.classList.toggle("on-watch", !!onWatch);
  btn.dataset.send = sendText;
  btn.innerHTML = onWatch ? '<span class="ok">On the watch ✓</span><span class="rm">Remove from watch</span>' : sendText;
}

// Sending to or removing from Garmin through the server; the page reloads when it's done, or the note shows the error
export async function watchRequest(btn, note, method, url, body) {
  const sending = method === "POST";
  btn.disabled = true;
  btn.textContent = sending ? "Sending…" : "Removing…";
  let error = "";
  try {
    const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body && JSON.stringify(body) });
    if (!res.ok) error = (await res.json().catch(() => ({}))).error || (sending ? "Sending failed." : "Removing failed.");
  } catch {
    error = "Couldn't reach the dashboard server.";
  }
  if (error) {
    watchButton(btn, !sending, btn.dataset.send);
    note.className = "error";
    note.textContent = error;
    return;
  }
  dispatchEvent(new Event("reload"));  // the server now marks it as sent, or not
}

// Only this button sends today's workout to Garmin, or takes it off again (deleted on Garmin, gone from the watch at
// its next sync)
$("wkSend").addEventListener("click", () => {
  const w = data.workouts.find((x) => x.sport === sport), btn = $("wkSend"), note = $("wkSendNote");
  if (!w.sent) return watchRequest(btn, note, "POST", `/api/workout/${w.sport}`, { title: w.title });
  if (confirm("Remove today's workout from Garmin and the watch?")) watchRequest(btn, note, "DELETE", `/api/workout/${w.sport}`);
});
