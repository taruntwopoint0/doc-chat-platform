/* Shared state, transport, dialogs, sign-in and small DOM helpers.
 *
 * The dashboard is dependency-free on purpose: no build step, no node_modules,
 * nothing to keep in sync with the Python side. It is split across plain
 * scripts that share globals, so index.html must load them in order:
 *
 *   markdown.js  core.js  workspaces.js  documents.js  chat.js  main.js
 *
 * Handlers may call functions from later files -- they run after every script
 * has loaded -- but top-level code may only use what is defined before it.
 *
 * There is no window.prompt or window.confirm anywhere. Embedded browsers
 * block both, so every dialog is inline DOM.
 */
"use strict";

const $ = (id) => document.getElementById(id);

let me = null;                 // signed-in user (or the implicit local one)
let authEnabled = false;       // from /health; false means no login screen
let maxUploadMb = null;        // from /health, so the UI never quotes a stale limit
let workspaceId = "";          // the open workspace; "" on the gallery
let currentWs = null;          // its detail: name, profile, document counts
let pollTimer = null;

const isAdmin = () => !me || me.is_admin;

/* ---------- DOM helpers ---------- */

/** Create an element. `text` becomes textContent and is never parsed as HTML. */
function h(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/* Fixed markup written here, never assembled from data. */
const ICONS = {
  logo: '<svg viewBox="0 0 28 28" aria-hidden="true"><rect width="28" height="28" rx="8" style="fill:var(--primary)"/><path d="M9 6h8.2l3.8 3.8V21a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z" style="fill:var(--surface)"/><rect x="10.3" y="12.2" width="8.4" height="2.9" rx="1" fill="#FFD84D"/><rect x="10.3" y="17.3" width="6" height="1.5" rx=".75" style="fill:var(--primary)"/></svg>',
  plus: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M10 4v12M4 10h12"/></svg>',
  trash: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 5.5h13M8 5.5V4h4v1.5M5.5 5.5l.7 10.2a1.5 1.5 0 0 0 1.5 1.3h4.6a1.5 1.5 0 0 0 1.5-1.3l.7-10.2M8.5 8.5v5.5M11.5 8.5v5.5"/></svg>',
  send: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 16V4M5 9l5-5 5 5"/></svg>',
  upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15V4M7.5 8.5 12 4l4.5 4.5M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/></svg>',
  alert: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><circle cx="10" cy="10" r="7.5"/><path d="M10 6v4.5M10 13.6v.1"/></svg>',
  chevron: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>',
};

function icon(name) {
  const span = h("span");
  span.dataset.icon = name;
  span.innerHTML = ICONS[name];
  return span;
}

/** Fill the static [data-icon] placeholders written in index.html. */
function paintIcons(root) {
  (root || document).querySelectorAll("[data-icon]").forEach((node) => {
    if (!node.firstChild && ICONS[node.dataset.icon]) node.innerHTML = ICONS[node.dataset.icon];
  });
}

const FILE_KINDS = {
  pdf: ["pdf", "PDF"], docx: ["doc", "DOCX"], doc: ["doc", "DOC"],
  pptx: ["ppt", "PPTX"], ppt: ["ppt", "PPT"], xlsx: ["xls", "XLSX"], xls: ["xls", "XLS"],
  csv: ["xls", "CSV"], md: ["txt", "MD"], markdown: ["txt", "MD"], txt: ["txt", "TXT"],
  html: ["web", "HTML"], htm: ["web", "HTML"],
};

/** A small coloured tile naming the file type, e.g. PDF or PPTX. */
function fileBadge(filename) {
  const ext = String(filename).includes(".") ? filename.split(".").pop().toLowerCase() : "";
  const [kind, label] = FILE_KINDS[ext] || ["txt", (ext || "file").slice(0, 4).toUpperCase()];
  return h("span", `ftype ${kind}`, label);
}

function plural(n, word, many) {
  return `${n} ${n === 1 ? word : (many || word + "s")}`;
}

function formatBytes(n) {
  if (n === null || n === undefined) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${i && n < 10 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function timeAgo(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return days === 1 ? "yesterday" : `${days} days ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/* ---------- transport ---------- */

async function api(path, options) {
  const opts = options || {};
  let res;
  try {
    res = await fetch(path, {
      method: opts.method || "GET",
      headers: opts.json ? { "Content-Type": "application/json" } : undefined,
      body: opts.json ? JSON.stringify(opts.json) : opts.body,
      credentials: "same-origin",   // the session cookie
    });
  } catch {
    // A sleeping Render instance or a dropped connection lands here.
    throw new Error("Could not reach the server. It may be waking up — try again in a moment.");
  }
  if (res.status === 401 && !opts.allow401 && authEnabled) {
    showLogin("Your session expired. Sign in again.");
    throw new Error("Not signed in.");
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    // A proxy error page instead of JSON (Render returns HTML while booting).
    throw new Error(res.ok ? "Unexpected response from the server."
                           : `Server error (${res.status}). Try again in a moment.`);
  }
  if (!res.ok) {
    const error = new Error((data && (data.detail || data.message)) || res.statusText);
    error.status = res.status;
    throw error;
  }
  return data;
}

function toast(message, bad) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("bad", !!bad);
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 3200);
}

/** Inline replacement for window.confirm, which embedded browsers block. */
function confirmDialog(title, text, okLabel) {
  return new Promise((resolve) => {
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    $("confirm-ok").textContent = okLabel || "Delete";
    $("confirm").hidden = false;
    $("confirm-cancel").focus();
    const done = (answer) => {
      $("confirm").hidden = true;
      $("confirm-ok").onclick = null;
      $("confirm-cancel").onclick = null;
      document.removeEventListener("keydown", onKey);
      resolve(answer);
    };
    const onKey = (e) => { if (e.key === "Escape") done(false); };
    document.addEventListener("keydown", onKey);
    $("confirm-ok").onclick = () => done(true);
    $("confirm-cancel").onclick = () => done(false);
  });
}

/* ---------- sign in ---------- */

function showLogin(message) {
  me = null;
  $("app").hidden = true;
  $("login").hidden = false;
  const err = $("login-error");
  err.hidden = !message;
  if (message) err.textContent = message;
  $("username").focus();
}

$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const btn = $("login-btn");
  btn.disabled = true;
  try {
    me = await api("/api/auth/login", {
      method: "POST", allow401: true,
      json: { username: $("username").value, password: $("password").value },
    });
    $("password").value = "";
    $("login").hidden = true;
    $("app").hidden = false;
    $("login-error").hidden = true;
    await start();
  } catch (err) {
    showLogin(err.message);
  } finally {
    btn.disabled = false;
  }
};

$("logout").onclick = async () => {
  try { await api("/api/auth/logout", { method: "POST", allow401: true }); }
  catch { /* signing out locally regardless */ }
  $("chat").innerHTML = "";
  showLogin();
};

paintIcons();
