/* Boot, routing and health. Loaded last, so every function the other scripts
 * define exists before anything here runs.
 *
 * Routes live in the URL hash so Back works and a reload stays put:
 *   #/          the workspace gallery
 *   #/w/<id>    one workspace
 */
"use strict";

function showView(name) {
  $("home").hidden = name !== "home";
  $("workspace").hidden = name !== "workspace";
}

function setCrumb(name) {
  $("crumb-sep").hidden = !name;
  $("crumb-ws").hidden = !name;
  $("crumb-ws").textContent = name || "";
}

async function route() {
  const match = /^#\/w\/([0-9a-f-]{36})$/i.exec(location.hash);
  if (match) await openWorkspace(match[1]);
  else await openHome();
}

window.addEventListener("hashchange", () => { route(); });

async function health() {
  const el = $("health");
  const label = el.querySelector("span");
  try {
    const info = await (await fetch("/health")).json();
    authEnabled = !!info.auth_enabled;
    if (info.max_upload_mb) {
      maxUploadMb = info.max_upload_mb;
      $("limit").textContent = `Up to ${maxUploadMb} MB per file`;
    }
    // Nothing to sign out of when nobody signs in.
    $("logout").hidden = !authEnabled;
    const ok = info.status === "ok";
    el.className = "health " + (ok ? "ok" : "bad");
    label.textContent = ok ? "Online" : "Degraded";
    el.title = `Database ${info.database} · ${info.pending_jobs ?? 0} job(s) queued · parser ${info.parser}`;
    return ok;
  } catch {
    el.className = "health bad";
    label.textContent = "Offline";
    el.title = "The server is unreachable";
    return false;
  }
}

async function start() {
  $("whoami").textContent = authEnabled && me ? (me.display_name || me.username) : "";
  // Creating, uploading and deleting are admin actions.
  const admin = isAdmin();
  $("new-workspace").hidden = !admin;
  $("ws-empty-new").hidden = !admin;
  $("delete-workspace").hidden = !admin;
  $("drop").hidden = !admin;
  await route();
}

async function boot() {
  // Health first: it tells us whether a login screen is even in play.
  await health();
  try {
    me = await api("/api/auth/me", { allow401: true });
    $("login").hidden = true;
    $("app").hidden = false;
    await start();
  } catch {
    if (authEnabled) {
      showLogin();
    } else {
      // Sign-in is off, so a failure here is a real server problem rather than
      // a missing credential. Show the app and let the error surface in place.
      $("login").hidden = true;
      $("app").hidden = false;
      toast("Could not reach the server. It may be waking up — reload in a moment.", true);
    }
  }
}

boot();
setInterval(health, 20000);
