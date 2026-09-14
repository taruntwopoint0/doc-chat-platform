/* Chat: ask, render answers and citations, history, clear. */
"use strict";

function emptyChat() {
  const d = document.createElement("div");
  d.className = "empty";
  d.id = "chat-empty";
  d.innerHTML = `<p>Upload a document, wait for <span class="badge ready">ready</span>,
    then ask a question.</p>
    <p class="muted small">Every claim is cited. If your documents don't cover it,
    you'll be told so.</p>`;
  return d;
}

function bubble(who, cls) {
  const empty = $("chat-empty");
  if (empty) empty.remove();
  const wrap = document.createElement("div");
  wrap.className = "msg " + (cls || "");
  const label = document.createElement("div");
  label.className = "who";
  label.textContent = who;
  const body = document.createElement("div");
  body.className = "body";
  wrap.append(label, body);
  $("chat").append(wrap);
  $("chat").scrollTop = $("chat").scrollHeight;
  return { wrap, body };
}

/** Render a model answer as formatted text with highlighted [n] citations.
 *  renderMarkdown escapes before it formats -- see markdown.js. */
function renderAnswer(body, text) {
  body.classList.add("md");
  body.innerHTML = renderMarkdown(text);
}

function renderCitations(wrap, citations) {
  if (!citations || !citations.length) return;
  const box = document.createElement("div");
  box.className = "cites";
  citations.forEach((c) => {
    const d = document.createElement("details");
    d.className = "cite";
    const s = document.createElement("summary");
    const num = document.createElement("b");
    num.textContent = `[${c.number}]`;
    const where = document.createElement("span");
    const path = (c.section_path && c.section_path.length)
      ? " › " + c.section_path.join(" › ") : "";
    where.textContent = c.filename + path;
    s.append(num, " ", where);
    const snip = document.createElement("div");
    snip.className = "snip";
    snip.textContent = c.snippet;
    d.append(s, snip);
    box.append(d);
  });
  wrap.append(box);
  $("chat").scrollTop = $("chat").scrollHeight;
}

async function loadHistory() {
  const chat = $("chat");
  chat.innerHTML = "";
  if (!workspaceId) { chat.append(emptyChat()); return; }
  const loading = workspaceId;
  try {
    const messages = await api(`/api/workspaces/${workspaceId}/history`);
    // Switched workspace while this was loading: do not mix conversations.
    if (loading !== workspaceId) return;
    chat.innerHTML = "";
    if (!messages.length) { chat.append(emptyChat()); return; }
    messages.forEach((m) => {
      if (m.role === "user") {
        bubble("You", "user").body.textContent = m.content;
      } else {
        const b = bubble("Assistant", m.refused ? "refused" : "");
        renderAnswer(b.body, m.content);
        renderCitations(b.wrap, m.citations);
      }
    });
    chat.scrollTop = chat.scrollHeight;
  } catch (err) { toast(err.message, true); }
}

$("clear-chat").onclick = async () => {
  if (!workspaceId) return;
  const ok = await confirmDialog(
    "Clear this conversation? Your documents stay indexed.", "Clear");
  if (!ok) return;
  try {
    const res = await api(`/api/workspaces/${workspaceId}/history`, { method: "DELETE" });
    $("chat").innerHTML = "";
    $("chat").append(emptyChat());
    toast(res.deleted ? `Cleared ${res.deleted} message(s)` : "Nothing to clear");
  } catch (err) { toast(err.message, true); }
};

$("ask-form").onsubmit = async (e) => {
  e.preventDefault();
  const input = $("question");
  const question = input.value.trim();
  if (!question || $("send").disabled) return;
  if (!workspaceId) { toast("Create a workspace first.", true); return; }

  bubble("You", "user").body.textContent = question;
  input.value = "";
  input.style.height = "auto";
  $("send").disabled = true;

  const asking = workspaceId;
  const pending = bubble("Assistant");
  pending.body.innerHTML = '<span class="muted dots">Searching your documents</span>';

  try {
    const res = await api(`/api/workspaces/${asking}/chat`,
                          { method: "POST", json: { question } });
    pending.wrap.classList.toggle("refused", res.refused);
    renderAnswer(pending.body, res.answer);
    renderCitations(pending.wrap, res.citations || []);
  } catch (err) {
    pending.wrap.classList.add("refused");
    pending.body.classList.remove("md");
    pending.body.textContent = err.message;
  } finally {
    $("send").disabled = false;
    input.focus();
  }
};

$("question").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px";
});
$("question").addEventListener("keydown", (e) => {
  // Enter sends; Shift+Enter is a newline. isComposing keeps an IME
  // (Hindi, Chinese, Japanese input) from sending mid-word.
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("ask-form").requestSubmit();
  }
});
