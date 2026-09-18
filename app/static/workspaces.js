/* The workspace gallery: one card per workspace, create, delete. */
"use strict";

let homeList = [];
let homeTimer = null;

async function openHome() {
  clearTimeout(pollTimer);
  workspaceId = "";
  currentWs = null;
  showView("home");
  setCrumb("");
  document.title = "Workspaces · Document Chat";
  await loadHome();
}

async function loadHome() {
  clearTimeout(homeTimer);
  try {
    homeList = await api("/api/workspaces");
  } catch (err) { toast(err.message, true); return; }
  if (workspaceId) return;              // opened a workspace while this loaded
  renderHome();
  // Keep the "Indexing" states honest while any card has work in flight.
  if (homeList.some((w) => w.documents.in_progress)) homeTimer = setTimeout(loadHome, 3000);
}

function renderHome() {
  const grid = $("ws-grid");
  grid.querySelectorAll(".ws-card:not(.ws-new), .no-match").forEach((n) => n.remove());

  const query = $("ws-filter").value.trim().toLowerCase();
  const matches = (w) => !query || w.name.toLowerCase().includes(query)
    || w.top_topics.some((t) => t.toLowerCase().includes(query));
  const shown = homeList
    .filter(matches)
    .sort((a, b) => new Date(b.last_activity_at) - new Date(a.last_activity_at));

  $("ws-empty").hidden = homeList.length > 0 || !$("ws-form").hidden;
  $("ws-filter").hidden = homeList.length < 7;   // only worth it once the grid is busy
  shown.forEach((w) => grid.append(workspaceCard(w)));
  if (query && !shown.length) grid.append(h("p", "no-match", `No workspace matches “${query}”.`));
}

function workspaceCard(w) {
  const d = w.documents;
  const card = h("article", "ws-card");

  const top = h("div", "ws-card-top");
  const title = h("h2", "ws-name");
  const link = h("a", "ws-link", w.name);
  link.href = `#/w/${w.id}`;
  title.append(link);
  top.append(title);
  if (isAdmin()) {
    const del = h("button", "icon-btn");
    del.type = "button";
    del.title = `Delete ${w.name}`;
    del.setAttribute("aria-label", `Delete workspace ${w.name}`);
    del.append(icon("trash"));
    del.onclick = (e) => { e.preventDefault(); deleteWorkspace(w.id, w.name, d); };
    top.append(del);
  }
  card.append(top);

  if (w.document_types.length) {
    const kinds = w.document_types.map((t) => t.charAt(0).toUpperCase() + t.slice(1));
    card.append(h("p", "ws-kinds", kinds.join(" · ")));
  }

  const stats = h("div", "ws-stats");
  const stat = (n, label) => {
    const s = h("div", "ws-stat");
    s.append(h("b", null, String(n)), h("span", null, label));
    return s;
  };
  stats.append(stat(d.total, d.total === 1 ? "document" : "documents"),
               stat(d.chunk_count, d.chunk_count === 1 ? "passage" : "passages"));
  card.append(stats);

  const chips = h("div", "chips");
  if (w.top_topics.length) {
    w.top_topics.forEach((t) => chips.append(h("span", "chip", t)));
  } else {
    chips.classList.add("none");
    chips.textContent = d.total ? "Topics appear once indexing finishes" : "Empty — open it to add documents";
  }
  card.append(chips);

  const foot = h("div", "ws-card-foot");
  let state;
  if (d.in_progress) state = h("span", "state warn", `Indexing ${d.in_progress}`);
  else if (d.failed) state = h("span", "state err", `${d.failed} failed`);
  else if (d.total) state = h("span", "state ok", "Ready");
  else state = h("span", "state", "No documents");
  foot.append(state, h("span", "ws-when", `Active ${timeAgo(w.last_activity_at)}`));
  card.append(foot);
  return card;
}

/* ---------- create ---------- */

function showCreate() {
  $("ws-form").hidden = false;
  $("ws-empty").hidden = true;
  $("ws-name").value = "";
  $("ws-name").focus();
}

$("new-workspace").onclick = showCreate;
$("ws-empty-new").onclick = showCreate;
$("ws-cancel").onclick = () => { $("ws-form").hidden = true; renderHome(); };
$("ws-filter").oninput = () => renderHome();

$("ws-form").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("ws-name").value.trim();
  if (!name) { $("ws-name").focus(); return; }
  // The slug is derived so you only ever type a human name.
  let slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "workspace";
  try {
    let ws;
    try {
      ws = await api("/api/workspaces", { method: "POST", json: { name, slug } });
    } catch (err) {
      if (err.status !== 409) throw err;
      // The same name twice is a reasonable thing to do; make the slug unique.
      slug = `${slug}-${Date.now().toString(36).slice(-4)}`;
      ws = await api("/api/workspaces", { method: "POST", json: { name, slug } });
    }
    $("ws-form").hidden = true;
    toast(`Created “${ws.name}”`);
    location.hash = `#/w/${ws.id}`;
  } catch (err) { toast(err.message, true); }
};

/* ---------- delete ---------- */

async function deleteWorkspace(id, name, counts) {
  // Say what is actually about to be destroyed: there is no undo.
  const d = counts || (currentWs && currentWs.id === id ? currentWs.documents : null);
  const detail = d && d.total
    ? `This removes ${plural(d.total, "document")}, ${plural(d.chunk_count, "indexed passage")} and the conversation.`
    : "It has no documents. Its conversation, if any, is removed too.";
  const ok = await confirmDialog(`Delete “${name}”?`, `${detail} This cannot be undone.`,
                                 "Delete workspace");
  if (!ok) return;
  try {
    const res = await api(`/api/workspaces/${id}`, { method: "DELETE" });
    toast(res.message);
    if (workspaceId === id || location.hash !== "#/") location.hash = "#/";
    else await loadHome();
  } catch (err) { toast(err.message, true); }
}

$("delete-workspace").onclick = () => {
  if (currentWs) deleteWorkspace(currentWs.id, currentWs.name, currentWs.documents);
};
