/* Shared state, transport, dialogs and sign-in.
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
const WS_KEY = "docchat.workspace";

let me = null;                 // signed-in user (or the implicit local one)
let authEnabled = false;       // from /health; false means no login screen
let maxUploadMb = null;        // from /health, so the UI never quotes a stale limit
let workspaceId = readStored(WS_KEY);
let pollTimer = null;

/** localStorage can throw (private windows, blocked site data); never let
 *  that stop the dashboard from loading. */
function readStored(key) {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}
function writeStored(key, value) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch { /* remembering the workspace is a convenience, not a requirement */ }
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
    throw new Error((data && (data.detail || data.message)) || res.statusText);
  }
  return data;
}

function toast(message, bad) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("bad", !!bad);
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 3000);
}

/** Inline replacement for window.confirm, which embedded browsers block. */
function confirmDialog(text, okLabel) {
  return new Promise((resolve) => {
    $("confirm-text").textContent = text;
    $("confirm-ok").textContent = okLabel || "Delete";
    $("confirm").hidden = false;
    const done = (answer) => {
      $("confirm").hidden = true;
      $("confirm-ok").onclick = null;
      $("confirm-cancel").onclick = null;
      resolve(answer);
    };
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
