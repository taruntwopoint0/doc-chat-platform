/* Boot: health, identity, first paint. Loaded last, so every function the
 * other scripts define exists before anything here runs. */
"use strict";

async function health() {
  try {
    const h = await (await fetch("/health")).json();
    authEnabled = !!h.auth_enabled;
    if (h.max_upload_mb) {
      maxUploadMb = h.max_upload_mb;
      $("limit").textContent = `max ${maxUploadMb} MB`;
    }
    // Nothing to sign out of when nobody signs in.
    $("logout").hidden = !authEnabled;
    const el = $("health");
    el.textContent = h.status === "ok" ? "ok" : "down";
    el.className = "pill " + (h.status === "ok" ? "ok" : "bad");
    el.title = `db ${h.database} · ${h.pending_jobs} job(s) queued · parser ${h.parser}`;
    return h.status === "ok";
  } catch {
    const el = $("health");
    el.textContent = "down";
    el.className = "pill bad";
    el.title = "The server is unreachable";
    return false;
  }
}

async function start() {
  $("whoami").textContent = authEnabled && me ? (me.display_name || me.username) : "";
  // Creating and deleting workspaces are admin actions.
  const admin = !me || me.is_admin;
  $("new-workspace").hidden = !admin;
  $("delete-workspace").hidden = !admin;
  await loadWorkspaces();
  await Promise.all([refresh(), loadHistory()]);
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
