// The chat on the workout page: messages to Claude on the server, which can change today's session, the plan's days
// and later days. The conversation lives on the server (one a day), so every device sees the same one.

import { $, escapeHtml } from "./util.js";

let log = [], busy = false;

export const paragraphs = (text) => text.split(/\n{2,}/).map((p) => `<p>${escapeHtml(p).replace(/\n/g, "<br>")}</p>`).join("");
const dollars = (c) => `$${c < 0.01 ? c.toFixed(4) : c.toFixed(2)}`;
const tokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);
const MODEL_NAME = { "claude-haiku-5-5": "Haiku", "claude-sonnet-5-5": "Sonnet" };

function item(m) {
  if (m.role === "user") return `<li class="user">${escapeHtml(m.text)}</li>`;
  const changes = m.changes?.length ? `<ul class="chat-changes">${m.changes.map((c) => `<li>✓ ${escapeHtml(c)}</li>`).join("")}</ul>` : "";
  const body = m.error ? `<p class="error">${escapeHtml(m.error)}</p>` : m.pending ? '<p class="pending">Thinking…</p>' : paragraphs(m.text || "");
  return `<li class="assistant">${body}${changes}${usage(m.usage)}</li>`;
}

// A reply's tokens and price: what the API charged, or with Claude Code what they'd cost on the API
export function usage(u) {
  if (!u) return "";
  const price = u.cost != null ? dollars(u.cost) : `on the API ≈ ${dollars(u.haiku)} Haiku, ${dollars(u.sonnet)} Sonnet`;
  return `<p class="chat-usage">${tokens(u.tokens)} tokens · ${price}</p>`;
}

// This month's spend: on the API what it charged, with Claude Code what the same chats would cost there
function cost(out) {
  $("chatCost").textContent = out.backend === "api" ? `${MODEL_NAME[out.model] || out.model} · ${dollars(out.month_cost || 0)} this month`
    : out.backend ? `This month on the API ≈ ${dollars(out.month_haiku || 0)} Haiku, ${dollars(out.month_sonnet || 0)} Sonnet` : "";
}

function render() {
  const el = $("chatLog");
  el.hidden = !log.length;
  el.innerHTML = log.map(item).join("");
  el.scrollTop = el.scrollHeight;
  $("chatNew").hidden = !log.length || busy;
  $("chatUndo").hidden = !log.some((m) => m.changes?.length) || busy;
}

function note(text, error = false) {
  $("chatNote").textContent = text;
  $("chatNote").className = error ? "error" : "";
}

async function request(method, path, body) {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body && JSON.stringify(body) });
  if (res.status === 404 || res.status === 405) throw new Error("The dashboard server is older than this page. Restart it, then try again.");
  const out = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(out.error || "The chat didn't answer.");
  return out;
}

async function start() {
  try {
    const out = await request("GET", "/api/chat");
    log = out.log;
    cost(out);
    if (!out.enabled) {
      $("chatInput").disabled = $("chatSend").disabled = true;
      note("The chat needs Claude Code installed on the machine running the server, or ANTHROPIC_API_KEY in .env.");
    }
  } catch (e) {
    note(e.message, true);
  }
  render();
}

function fit() {
  const t = $("chatInput");
  t.style.height = "auto";
  t.style.height = `${t.scrollHeight + 2}px`;
}

async function send() {
  const text = $("chatInput").value.trim();
  if (!text || busy) return;
  busy = true;
  $("chatSend").disabled = true;
  $("chatInput").value = "";
  fit();
  note("");
  log.push({ role: "user", text }, { role: "assistant", pending: true });
  render();
  try {
    const out = await request("POST", "/api/chat", { text });
    log = out.log;
    cost(out);
    if (out.changed) dispatchEvent(new Event("reload"));  // redraw the workout and the plan with the changes
  } catch (e) {
    log[log.length - 1] = { role: "assistant", error: e.message };
    $("chatInput").value = $("chatInput").value || text;  // nothing reached the chat: keep the message to send again
    fit();
  }
  busy = false;
  $("chatSend").disabled = false;
  render();
  $("chatInput").focus();
}

$("chatForm").addEventListener("submit", (e) => { e.preventDefault(); send(); });

// Enter sends, Shift+Enter starts a new line
$("chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});

$("chatInput").addEventListener("input", fit);

$("chatNew").addEventListener("click", async () => {
  try {
    await request("DELETE", "/api/chat");
    log = [];
    note("");
    render();
  } catch (e) {
    note(e.message, true);
  }
});

$("chatUndo").addEventListener("click", async () => {
  if (!confirm("Undo every change the chat has made from today on? The suggested and planned sessions come back.")) return;
  try {
    await request("DELETE", "/api/changes");
    note("Changes undone.");
    dispatchEvent(new Event("reload"));
  } catch (e) {
    note(e.message, true);
  }
});

start();
