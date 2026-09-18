/* One workspace: its header, the document library, upload, topics. */
"use strict";

const WORKING = ["pending", "parsing", "profiling", "chunking", "embedding"];
const STAGE_WIDTH = { pending: 4, parsing: 12, profiling: 28, chunking: 45, embedding: 72 };
const STAGE_LABEL = {
  pending: "Queued", parsing: "Parsing", profiling: "Profiling",
  chunking: "Chunking", embedding: "Embedding",
};

async function openWorkspace(id) {
  clearTimeout(homeTimer);
  if (id !== workspaceId) {
    // Clear the previous workspace's content before the new one arrives.
    currentWs = null;
    $("ws-title").textContent = "";
    $("ws-meta").textContent = "";
    $("docs").innerHTML = "";
    $("chat").innerHTML = "";
    $("topics-wrap").hidden = true;
  }
  workspaceId = id;
  showView("workspace");
  try {
    currentWs = await api(`/api/workspaces/${id}`);
  } catch (err) {
    toast(err.status === 404 ? "That workspace no longer exists." : err.message, true);
    location.hash = "#/";
    return;
  }
  if (id !== workspaceId) return;
  renderHeader();
  await Promise.all([refresh(), loadHistory()]);
  $("question").focus();
}

function renderHeader() {
  const w = currentWs;
  $("ws-title").textContent = w.name;
  setCrumb(w.name);
  document.title = `${w.name} · Document Chat`;

  const d = w.documents;
  const meta = $("ws-meta");
  meta.innerHTML = "";
  const part = (n, word) => {
    const span = h("span");
    span.append(h("b", null, String(n)), ` ${n === 1 ? word : word + "s"}`);
    return span;
  };
  const created = new Date(w.created_at).toLocaleDateString(
    undefined, { day: "numeric", month: "short", year: "numeric" });
  const bits = [part(d.total, "document"), part(d.chunk_count, "passage")];
  if (d.in_progress) bits.push(h("span", null, `${d.in_progress} indexing`));
  bits.push(h("span", null, `created ${created}`));
  bits.forEach((bit, i) => { if (i) meta.append("  ·  "); meta.append(bit); });
}

function renderDocs(docs) {
  const ul = $("docs");
  ul.innerHTML = "";
  const ready = docs.filter((d) => d.status === "ready").length;
  $("doc-count").textContent = docs.length ? `${ready}/${docs.length} ready` : "";
  if (!docs.length) {
    ul.append(h("li", "docs-empty", isAdmin()
      ? "No documents yet. Add the first one above."
      : "No documents yet."));
    return;
  }

  docs.forEach((d) => {
    const li = h("li", "doc");
    li.append(fileBadge(d.filename));

    const main = h("div", "doc-main");
    const name = h("div", "doc-name", d.filename);
    name.title = d.filename;
    const meta = h("div", "doc-meta");
    if (d.status === "ready") {
      meta.append(h("span", "state ok", "Ready"),
                  h("span", "mono", plural(d.chunk_count, "passage")));
    } else if (d.status === "failed") {
      meta.append(h("span", "state err", "Failed"));
    } else {
      meta.append(h("span", "state warn", STAGE_LABEL[d.status] || d.status));
    }
    meta.append(h("span", "mono", formatBytes(d.size_bytes)));
    if (d.version_label && d.version_label !== "v1") meta.append(h("span", "mono", d.version_label));
    main.append(name, meta);

    if (WORKING.includes(d.status)) {
      const bar = h("div", "bar");
      const fill = h("i");
      fill.style.width = (STAGE_WIDTH[d.status] || 5) + "%";
      bar.append(fill);
      main.append(bar);
    }
    if (d.status === "failed" && d.error_message) main.append(h("div", "doc-err", d.error_message));
    li.append(main);

    if (isAdmin()) {
      const del = h("button", "icon-btn");
      del.type = "button";
      del.title = `Delete ${d.filename}`;
      del.setAttribute("aria-label", `Delete ${d.filename}`);
      del.append(icon("trash"));
      del.onclick = () => removeDoc(d);
      li.append(del);
    }
    ul.append(li);
  });
}

async function removeDoc(doc) {
  const ok = await confirmDialog(`Delete “${doc.filename}”?`,
    "Its indexed passages are removed too, so answers will no longer cite it.",
    "Delete document");
  if (!ok) return;
  try {
    await api(`/api/documents/${doc.id}`, { method: "DELETE" });
    toast(`Deleted ${doc.filename}`);
    refresh();
  } catch (err) { toast(err.message, true); }
}

/** Topic names from a counted {topic: n} profile, most frequent first. */
function topicsOf(ws) {
  const topics = (ws && ws.profile && ws.profile.topics) || {};
  return Object.entries(topics).sort((a, b) => b[1] - a[1]).map(([t]) => t);
}

function renderVocab() {
  const topics = topicsOf(currentWs).slice(0, 24);
  $("topics-wrap").hidden = !topics.length;
  const box = $("vocab");
  box.innerHTML = "";
  topics.forEach((t) => box.append(h("span", "chip", t)));
}

async function refresh() {
  clearTimeout(pollTimer);
  const id = workspaceId;
  if (!id) return;
  try {
    const [docs, detail] = await Promise.all([
      api(`/api/workspaces/${id}/documents`),
      api(`/api/workspaces/${id}`),
    ]);
    // Switched workspace while this was in flight: do not paint over it.
    if (id !== workspaceId) return;
    currentWs = detail;
    renderHeader();
    renderDocs(docs);
    renderVocab();
    refreshEmptyChat();
    // Poll only while something is actually in flight.
    if (docs.some((d) => WORKING.includes(d.status))) pollTimer = setTimeout(refresh, 2000);
  } catch (err) { toast(err.message, true); }
}

async function upload(files) {
  if (!workspaceId) return;
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
      toast(res.deduplicated ? `${file.name} is already indexed` : `${file.name}: ${res.message}`);
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
