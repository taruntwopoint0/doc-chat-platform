/* Workspaces: pick, create, delete. */
"use strict";

async function loadWorkspaces() {
  const list = await api("/api/workspaces");
  const select = $("workspace");
  select.innerHTML = "";
  if (!list.length) {
    select.add(new Option("No workspaces yet — click + New", ""));
    workspaceId = "";
    $("delete-workspace").disabled = true;
    return;
  }
  $("delete-workspace").disabled = false;
  list.forEach((w) => select.add(new Option(w.name, w.id)));
  if (!list.some((w) => w.id === workspaceId)) workspaceId = list[0].id;
  select.value = workspaceId;
  writeStored(WS_KEY, workspaceId);
}

$("workspace").onchange = async (e) => {
  workspaceId = e.target.value;
  writeStored(WS_KEY, workspaceId);
  await Promise.all([refresh(), loadHistory()]);
};

$("new-workspace").onclick = () => {
  $("ws-form").hidden = false;
  $("ws-name").value = "";
  $("ws-name").focus();
};

$("ws-cancel").onclick = () => { $("ws-form").hidden = true; };

$("delete-workspace").onclick = async () => {
  if (!workspaceId) return;
  const select = $("workspace");
  const name = select.options[select.selectedIndex].text;

  // Say what is actually about to be destroyed. Deleting a workspace takes its
  // documents and conversation with it, and there is no undo.
  let detail = "";
  try {
    const ws = await api(`/api/workspaces/${workspaceId}`);
    const d = ws.documents || {};
    detail = d.total
      ? ` This removes ${d.total} document(s) and ${d.chunk_count} indexed chunk(s), and the conversation.`
      : " It has no documents.";
  } catch { /* fall back to the bare question */ }

  const ok = await confirmDialog(
    `Delete the workspace “${name}”?${detail} This cannot be undone.`,
    "Delete workspace",
  );
  if (!ok) return;

  try {
    const res = await api(`/api/workspaces/${workspaceId}`, { method: "DELETE" });
    workspaceId = "";
    writeStored(WS_KEY, "");
    await loadWorkspaces();
    await Promise.all([refresh(), loadHistory()]);
    toast(res.message);
  } catch (err) { toast(err.message, true); }
};

$("ws-form").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("ws-name").value.trim();
  if (!name) return;
  // The slug is derived so you only ever type a human name.
  let slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  if (!slug) slug = "workspace";
  try {
    let ws;
    try {
      ws = await api("/api/workspaces", { method: "POST", json: { name, slug } });
    } catch (err) {
      if (!/already exists/i.test(err.message)) throw err;
      // The same name twice is a reasonable thing to do; make the slug unique.
      slug = `${slug}-${Date.now().toString(36).slice(-4)}`;
      ws = await api("/api/workspaces", { method: "POST", json: { name, slug } });
    }
    $("ws-form").hidden = true;
    workspaceId = ws.id;
    writeStored(WS_KEY, workspaceId);
    await loadWorkspaces();
    $("workspace").value = workspaceId;
    await Promise.all([refresh(), loadHistory()]);
    toast(`Created “${ws.name}”`);
  } catch (err) { toast(err.message, true); }
};
