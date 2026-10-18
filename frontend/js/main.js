// Entry point: loads the data, keeps it fresh and shows the page picked in the menu
// (kept in the address: #overview, #workout, #health, #activities, #sport/run)

import { data, setData } from "./state.js";
import { $ } from "./util.js";
import { sportCounts, sportsByCount, sportLabel, hasPage, sportColor } from "./sports.js";
import { renderHealth } from "./health.js";
import { renderOverview, lastMap } from "./overview.js";
import { renderActivities } from "./activities.js";
import { renderSport } from "./sport.js";
import { renderWorkout, lastWorkoutMap } from "./workout.js";
import { renderPlan } from "./plan.js";
import "./chat.js";
import { redrawModalMap } from "./modal.js";

let pollTimer = null;

let lastJson = "";

// The race goal and plan forms ask for a reload after saving; redraw even if nothing else changed
addEventListener("reload", () => { lastJson = ""; load().catch(() => {}); });

async function load() {
  const res = await fetch("/api/dashboard");
  if (res.status === 401) {  // logged out (the password changed, or the cookie expired)
    location.href = "/login";
    return;
  }
  const text = await res.text();
  if (text !== lastJson) {  // skip redrawing the charts when nothing changed
    lastJson = text;
    setData(JSON.parse(text));
    render();
  }
  clearTimeout(pollTimer);
  if (data.syncing) pollTimer = setTimeout(load, 5000);
}

// "Fri 14:27" for a timestamp from the server
const fmtWhen = (iso) => new Date(iso).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" });

function render() {
  $("demo").hidden = !data.demo;
  $("logout").hidden = !data.login;
  const btn = $("syncBtn");
  btn.disabled = data.syncing || data.demo;
  btn.textContent = data.syncing ? "Syncing…" : "Sync now";
  $("synced").innerHTML = data.meta.last_sync_at
    ? `<span class="sync-word">Synced </span>${fmtWhen(data.meta.last_sync_at)}`
    : "Not synced yet";

  const notice = $("notice");
  if (data.meta.last_error) {
    notice.className = "notice";
    const at = data.meta.last_error_at ? ` (${fmtWhen(data.meta.last_error_at)})` : "";
    notice.textContent = `Last sync failed${at}: ${data.meta.last_error}`;
    notice.hidden = false;
  } else {
    notice.hidden = true;
  }

  if (!data.has_data) {
    $("main").hidden = true;
    $("nav").hidden = true;
    $("empty").hidden = false;
    if (!data.syncing && !data.meta.last_sync_at) $("emptyText").textContent =
      "Nothing synced yet. Run python backend/login.py on the server machine if you haven't, then press Sync now.";
    return;
  }
  $("empty").hidden = true;
  $("main").hidden = false;
  $("nav").hidden = false;
  renderNav();
  showPage();
}

// The workout page also holds the training plan its run comes from
const PAGES = { overview: renderOverview, workout: () => { renderWorkout(); renderPlan(); }, health: renderHealth, activities: renderActivities };

function route() {
  const [page, key] = location.hash.slice(1).split("/");
  if (page === "sport" && hasPage(key)) return { page: "sport", key, id: `sport/${key}` };
  if (page === "plan") return { page: "workout", id: "workout" };  // the plan's own page, before it joined the workout
  return PAGES[page] ? { page, id: page } : { page: "overview", id: "overview" };
}

let shownPage = "";

export function showPage() {
  const r = route();
  // Show the page before drawing it, so its charts are drawn at their real size
  document.querySelectorAll(".page").forEach((el) => { el.hidden = el.id !== `page-${r.page}`; });
  document.querySelectorAll("#nav a").forEach((a) => {
    if (a.dataset.page === r.id) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  if (r.page !== "overview") lastMap.stop();
  if (r.page !== "workout") lastWorkoutMap.stop();
  if (r.page === "sport") renderSport(r.key); else PAGES[r.page]();
  const title = r.page === "sport" ? sportLabel(r.key) : $(`page-${r.page}`).dataset.title;
  document.title = r.page === "overview" ? "Training" : `${title} · Training`;
  $("topbarTitle").textContent = title;
  if (r.id !== shownPage) {  // a new page starts at the top; a background refresh of the same page keeps its place
    if (shownPage) scrollTo(0, 0);
    shownPage = r.id;
  }
}

addEventListener("hashchange", () => { setMenu(false); if (data?.has_data) showPage(); });

// Phones: the menu slides in from the left; picking a page, tapping beside it or Escape closes it
function setMenu(open) {
  if (document.body.classList.contains("menu-open") === open) return;
  document.body.classList.toggle("menu-open", open);
  $("menuBtn").setAttribute("aria-expanded", String(open));
  if (open) ($("nav").querySelector("[aria-current]") || $("syncBtn")).focus({ preventScroll: true });
  else if ($("side").contains(document.activeElement)) $("menuBtn").focus({ preventScroll: true });
}
$("menuBtn").addEventListener("click", () => setMenu(true));
$("scrim").addEventListener("click", () => setMenu(false));
$("nav").addEventListener("click", (e) => { if (e.target.closest("a")) setMenu(false); });  // also the page already shown
addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });
matchMedia("(min-width: 901px)").addEventListener("change", (e) => { if (e.matches) setMenu(false); });

export function renderNav() {
  const counts = sportCounts(), sports = sportsByCount(counts).filter(([key]) => hasPage(key, counts));
  $("navActCount").textContent = data.activities.length;
  $("navSportsLabel").hidden = !sports.length;
  $("navSports").innerHTML = sports.map(([key, label]) => `<li><a href="#sport/${key}" data-page="sport/${key}">
    <span class="label"><i class="swatch" style="background:${sportColor(key)}"></i>${label}</span><span class="count">${counts[key]}</span></a></li>`).join("");
}

$("syncBtn").addEventListener("click", async () => {
  $("syncBtn").disabled = true;
  $("syncBtn").textContent = "Syncing…";
  await fetch("/api/sync", { method: "POST" });
  setTimeout(load, 1500);
});

// Light or dark (js/theme.js): the charts and maps take their colors when drawn, so draw them again
addEventListener("themechange", () => {
  if (data?.has_data) { renderNav(); showPage(); }
  redrawModalMap();
});

const markTheme = () => $("themeSwitch").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.theme === theme.choice())));
markTheme();

$("themeSwitch").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-theme]");
  if (!b) return;
  theme.set(b.dataset.theme);
  markTheme();
});

// Pick up background syncs: check every 5 minutes, and right away when the tab comes back into view.
const refresh = () => { if (!document.hidden) load().catch(() => {}); };

setInterval(refresh, 5 * 60 * 1000);

document.addEventListener("visibilitychange", refresh);

load().catch((e) => {
  console.error(e);
  $("notice").className = "notice";
  $("notice").textContent = "Couldn't reach the dashboard server. Check that python backend/app.py is still running.";
  $("notice").hidden = false;
});

// Lets phones install the dashboard as an app (only over https, which `tailscale serve` gives)
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
