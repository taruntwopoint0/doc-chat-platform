/* Conversation: ask, answers with citations, suggestions, history, clear. */
"use strict";

function thread() {
  let t = $("chat").querySelector(".thread");
  if (!t) {
    t = h("div", "thread");
    $("chat").append(t);
  }
  return t;
}

function scrollChat() {
  const chat = $("chat");
  chat.scrollTop = chat.scrollHeight;
}

/* ---------- empty state, with questions to start from ---------- */

function suggestions() {
  const asks = topicsOf(currentWs).slice(0, 3).map((t) => `What do the documents say about ${t}?`);
  asks.push("What documents do you have?");   // answered instantly, no AI call
  return asks;
}

function emptyChat() {
  const box = h("div", "chat-empty");
  box.id = "chat-empty";
  box.append(Object.assign(icon("logo"), { className: "empty-art" }));
  const d = currentWs ? currentWs.documents : null;

  if (!d || !d.ready) {
    const indexing = d && d.in_progress;
    box.append(
      h("h2", null, indexing ? "Your documents are being indexed" : "Add a document to get started"),
      h("p", null, indexing
        ? "You can start asking as soon as a document in the library shows Ready."
        : "Drop files into the library. Once a file shows Ready, ask anything about it."),
    );
    return box;
  }

  box.append(
    h("h2", null, `Ask anything about ${currentWs.name}`),
    h("p", null, `Answers come only from the ${plural(d.ready, "document")} here, `
      + "and every claim links to the passage it came from."),
  );
  const chips = h("div", "chips");
  suggestions().forEach((q) => {
    const chip = h("button", "chip", q);
    chip.type = "button";
    chip.onclick = () => { $("question").value = q; $("ask-form").requestSubmit(); };
    chips.append(chip);
  });
  box.append(chips);
  return box;
}

/** Documents finishing indexing change what the empty state should say. */
function refreshEmptyChat() {
  const old = $("chat-empty");
  if (old) old.replaceWith(emptyChat());
}

/* ---------- messages ---------- */

function addUser(text) {
  const empty = $("chat-empty");
  if (empty) empty.remove();
  const msg = h("div", "msg user");
  msg.append(h("div", "body", text));
  thread().append(msg);
  scrollChat();
  return msg;
}

function addBot() {
  const empty = $("chat-empty");
  if (empty) empty.remove();
  const wrap = h("div", "msg bot");
  const avatar = h("div", "avatar");
  avatar.append(icon("logo"));
  const content = h("div", "content");
  const body = h("div", "body");
  content.append(body);
  wrap.append(avatar, content);
  thread().append(wrap);
  scrollChat();
  return { wrap, content, body };
}

/** Answers are rendered by markdown.js, which escapes before it formats. */
function renderAnswer(msg, text, refusalLabel) {
  const { wrap, body } = msg;
  body.innerHTML = "";
  wrap.classList.toggle("refused", !!refusalLabel);
  if (!refusalLabel) {
    body.className = "body md";
    body.innerHTML = renderMarkdown(text);
    return;
  }
  body.className = "body";
  const words = h("div");
  const md = h("div", "md");
  md.innerHTML = renderMarkdown(text);
  words.append(h("span", "refusal-label", refusalLabel), md);
  body.append(icon("alert"), words);
}

/** Sources grouped by file: one chip per cited passage, and a panel that
 *  shows the passage for whichever chip is selected. */
function renderCitations(msg, citations) {
  if (!citations || !citations.length) return;
  const sorted = [...citations].sort((a, b) => a.number - b.number);
  const byFile = new Map();
  sorted.forEach((c) => {
    if (!byFile.has(c.filename)) byFile.set(c.filename, []);
    byFile.get(c.filename).push(c);
  });

  const sources = h("div", "sources");
  sources.append(h("div", "sources-label",
    `Sources · ${plural(sorted.length, "passage")} from ${plural(byFile.size, "document")}`));
  const panel = h("div", "snip-panel");
  panel.hidden = true;
  let active = null;

  const select = (c, chip) => {
    if (active) active.classList.remove("on");
    if (active === chip) { active = null; panel.hidden = true; return; }
    active = chip;
    chip.classList.add("on");
    panel.innerHTML = "";
    const head = h("div", "snip-head");
    head.append(h("span", "ref", `[${c.number}]`),
                h("span", "snip-where", [c.filename, ...(c.section_path || [])].join(" › ")));
    // PowerPoint stores a line break inside a text box as a vertical tab.
    panel.append(head, h("div", "snip-text", c.snippet.replace(/\v/g, "\n")));
    panel.hidden = false;
  };

  byFile.forEach((list, filename) => {
    const group = h("div", "src-file");
    const head = h("div", "src-head");
    head.append(fileBadge(filename), h("span", "src-name", filename));
    const chips = h("div", "src-chips");
    list.forEach((c) => {
      const chip = h("button", "src-chip");
      chip.type = "button";
      chip.dataset.n = c.number;
      chip.title = [filename, ...(c.section_path || [])].join(" › ");
      const path = c.section_path || [];
      chip.append(h("span", "ref", `[${c.number}]`),
                  h("span", "src-label", path.length ? path[path.length - 1] : "Passage"));
      chip.onclick = () => select(c, chip);
      chips.append(chip);
    });
    group.append(head, chips);
    sources.append(group);
  });
  sources.append(panel);
  msg.content.append(sources);
}

// A [n] in an answer opens the passage it refers to.
$("chat").addEventListener("click", (e) => {
  const ref = e.target.closest(".body .ref");
  if (!ref) return;
  const n = ref.textContent.replace(/\D/g, "");
  const msg = ref.closest(".msg");
  const chip = msg.querySelector(`.src-chip[data-n="${n}"]`);
  if (!chip) return;
  if (!chip.classList.contains("on")) chip.click();
  const panel = msg.querySelector(".snip-panel");
  panel.classList.remove("flash");
  void panel.offsetWidth;              // restart the highlight animation
  panel.classList.add("flash");
  panel.scrollIntoView({ block: "nearest" });
});

async function loadHistory() {
  const chat = $("chat");
  const loading = workspaceId;
  if (!loading) return;
  try {
    const messages = await api(`/api/workspaces/${loading}/history`);
    // Switched workspace while this was loading: do not mix conversations.
    if (loading !== workspaceId) return;
    chat.innerHTML = "";
    if (!messages.length) { chat.append(emptyChat()); return; }
    messages.forEach((m) => {
      if (m.role === "user") { addUser(m.content); return; }
      const msg = addBot();
      renderAnswer(msg, m.content, m.refused ? "Not covered by these documents" : "");
      renderCitations(msg, m.citations);
    });
    chat.style.scrollBehavior = "auto";
    scrollChat();
    chat.style.scrollBehavior = "";
  } catch (err) { toast(err.message, true); }
}

$("clear-chat").onclick = async () => {
  if (!workspaceId) return;
  const ok = await confirmDialog("Clear this conversation?",
    "Your documents stay indexed. Only the questions and answers are removed.", "Clear chat");
  if (!ok) return;
  try {
    const res = await api(`/api/workspaces/${workspaceId}/history`, { method: "DELETE" });
    $("chat").innerHTML = "";
    $("chat").append(emptyChat());
    toast(res.deleted ? `Cleared ${plural(res.deleted, "message")}` : "Nothing to clear");
  } catch (err) { toast(err.message, true); }
};

/* ---------- asking ---------- */

function syncSend() {
  $("send").disabled = !$("question").value.trim() || $("send").dataset.busy === "1";
}

$("ask-form").onsubmit = async (e) => {
  e.preventDefault();
  const input = $("question");
  const question = input.value.trim();
  if (!question || $("send").dataset.busy === "1" || !workspaceId) return;

  const asked = addUser(question);
  input.value = "";
  input.style.height = "auto";
  $("send").dataset.busy = "1";
  syncSend();

  const asking = workspaceId;
  const msg = addBot();
  msg.body.innerHTML = '<span class="thinking dots">Searching your documents</span>';
  try {
    const res = await api(`/api/workspaces/${asking}/chat`, { method: "POST", json: { question } });
    renderAnswer(msg, res.answer, res.refused ? "Not covered by these documents" : "");
    renderCitations(msg, res.citations || []);
    // Show the answer from its question down, not the tail of its sources.
    if (asking === workspaceId) asked.scrollIntoView({ block: "start", behavior: "smooth" });
  } catch (err) {
    renderAnswer(msg, err.message, err.status === 429 ? "The AI service is busy" : "Couldn’t answer");
  } finally {
    $("send").dataset.busy = "";
    syncSend();
    if (asking === workspaceId) input.focus();
  }
};

$("question").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 180) + "px";
  syncSend();
});
$("question").addEventListener("keydown", (e) => {
  // Enter sends; Shift+Enter is a newline. isComposing keeps an IME
  // (Hindi, Chinese, Japanese input) from sending mid-word.
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("ask-form").requestSubmit();
  }
});
syncSend();
