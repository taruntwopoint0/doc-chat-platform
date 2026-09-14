/* Documents: list, status polling, delete, upload, workspace vocabulary. */
"use strict";

const WORKING = ["pending", "parsing", "profiling", "chunking", "embedding"];
const STAGE_WIDTH = { pending: 4, parsing: 12, profiling: 28, chunking: 45, embedding: 72 };

function renderDocs(docs) {
  const ul = $("docs");
  ul.innerHTML = "";
  $("doc-count").textContent = docs.length
    ? `${docs.filter((d) => d.status === "ready").length}/${docs.length} ready`
    : "";

  docs.forEach((d) => {
    const li = document.createElement("li");
    const row = document.createElement("div");
    row.className = "doc-row";

    const name = document.createElement("span");
    name.className = "doc-name";
    name.textContent = d.filename;
    name.title = d.filename;

    const badge = document.createElement("span");
    badge.className = "badge " + (d.status === "ready" ? "ready"
      : d.status === "failed" ? "failed" : "working");
    badge.textContent = d.status === "ready" ? `ready · ${d.chunk_count}` : d.status;

    row.append(name, badge);

    if (me && me.is_admin) {
      const del = document.createElement("button");
      del.className = "doc-del";
      del.textContent = "×";
      del.title = "Delete";
      del.onclick = () => removeDoc(d);
      row.append(del);
    }
    li.append(row);

    if (WORKING.includes(d.status)) {
      const bar = document.createElement("div");
      bar.className = "bar";
      const fill = document.createElement("i");
      fill.style.width = (STAGE_WIDTH[d.status] || 5) + "%";
      bar.append(fill);
      li.append(bar);
    }
    if (d.status === "failed" && d.error_message) {
      const err = document.createElement("div");
      err.className = "doc-err";
      err.textContent = d.error_message;
      li.append(err);
    }
    ul.append(li);
  });
}

async function removeDoc(doc) {
  const ok = await confirmDialog(
    `Delete “${doc.filename}” and everything indexed from it?`);
  if (!ok) return;
  try {
    await api(`/api/documents/${doc.id}`, { method: "DELETE" });
    toast(`Deleted ${doc.filename}`);
    refresh();
  } catch (err) { toast(err.message, true); }
}

function renderVocab(profile) {
  const box = $("vocab");
  const byCount = (obj) => Object.entries(obj || {}).sort((a, b) => b[1] - a[1]).slice(0, 40);
  const topics = byCount(profile.topics);
  const entities = byCount(profile.entities);
  if (!topics.length && !entities.length) {
    box.textContent = "Nothing yet — it fills in as documents are indexed.";
    return;
  }
  box.innerHTML = "";
  const section = (label, pairs) => {
    if (!pairs.length) return;
    const h = document.createElement("div");
    h.className = "muted small";
    h.textContent = label;
    h.style.marginTop = "6px";
    box.append(h);
    pairs.forEach(([k, n]) => {
      const t = document.createElement("span");
      t.className = "tag";
      t.textContent = n > 1 ? `${k} ×${n}` : k;
      box.append(t);
    });
  };
  section("Topics", topics);
  section("Entities", entities);
}

async function refresh() {
  clearTimeout(pollTimer);
  if (!workspaceId) { renderDocs([]); return; }
  const polling = workspaceId;
  try {
    const [docs, ws] = await Promise.all([
      api(`/api/workspaces/${workspaceId}/documents`),
      api(`/api/workspaces/${workspaceId}`),
    ]);
    // The workspace may have been switched while this request was in flight;
    // painting its results over the new workspace would show the wrong list.
    if (polling !== workspaceId) return;
    renderDocs(docs);
    renderVocab(ws.profile || {});
    // Poll only while something is actually in flight.
    if (docs.some((d) => WORKING.includes(d.status))) {
      pollTimer = setTimeout(refresh, 2000);
    }
  } catch (err) { toast(err.message, true); }
}

async function upload(files) {
  if (!workspaceId) { toast("Create a workspace first.", true); return; }
  for (const file of files) {
    if (maxUploadMb && file.size > maxUploadMb * 1024 * 1024) {
      const mb = (file.size / 1024 / 1024).toFixed(1);
      toast(`${file.name} is ${mb} MB; the limit is ${maxUploadMb} MB per file.`, true);
      continue;
    }
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await api(`/api/workspaces/${workspaceId}/documents`,
                            { method: "POST", body: form });
      toast(res.deduplicated ? `${file.name}: already indexed`
                             : `${file.name}: ${res.message}`);
    } catch (err) { toast(`${file.name}: ${err.message}`, true); }
    // Show each file in the list as soon as it is accepted, not after the batch.
    refresh();
  }
}

const drop = $("drop");
$("browse").onclick = () => $("file").click();
$("file").onchange = (e) => { upload(e.target.files); e.target.value = ""; };
["dragenter", "dragover"].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => upload(e.dataTransfer.files));
