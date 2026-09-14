/* Safe markdown for model answers.
 *
 * Answers contain text quoted from uploaded documents, so they are untrusted:
 * a document could carry <script> or an <img onerror=...>. Everything is
 * therefore HTML-escaped FIRST, and formatting is applied to the escaped text
 * with patterns that only ever emit a fixed set of tags. No captured text is
 * ever inserted unescaped, and there is no link syntax at all, so neither a tag
 * nor a javascript: URL can come through.
 *
 * Deliberately small: bold, italic, inline code, headings, bullet and numbered
 * lists, paragraphs, and [n] citation markers. That covers what the model
 * actually writes without taking on a markdown library.
 */
"use strict";

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/** Inline formatting on ALREADY-ESCAPED text. Code spans are left alone. */
function inlineMarkdown(escaped) {
  return escaped
    .split(/(`[^`]+`)/g)
    .map((part) => {
      if (/^`[^`]+`$/.test(part)) return `<code>${part.slice(1, -1)}</code>`;
      return part
        .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?!\*)/g, "$1<em>$2</em>")
        .replace(/\[(\d{1,2})\]/g, '<span class="ref">[$1]</span>');
    })
    .join("");
}

function renderMarkdown(text) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let html = "";
  let list = null;          // "ul" | "ol" while inside a list
  let paragraph = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html += `<p>${paragraph.map(inlineMarkdown).join("<br>")}</p>`;
    paragraph = [];
  };
  const closeList = () => {
    if (list) html += `</${list}>`;
    list = null;
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) {
      flushParagraph();
      closeList();
      continue;
    }

    const heading = /^\s{0,3}(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph();
      closeList();
      // Demote: a "# Title" from the model should not dwarf the chat bubble.
      const level = Math.min(heading[1].length + 2, 6);
      html += `<h${level}>${inlineMarkdown(escapeHtml(heading[2]))}</h${level}>`;
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.+)$/.exec(line);
    const numbered = /^\s*(\d{1,4})[.)]\s+(.+)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) {
        closeList();
        // Keep the source's numbering: a procedure that resumes at step 4
        // must not be relabelled as step 1. The start value is digits only.
        const start = numbered && numbered[1] !== "1" ? ` start="${numbered[1]}"` : "";
        html += `<${kind}${start}>`;
        list = kind;
      }
      const content = bullet ? bullet[1] : numbered[2];
      html += `<li>${inlineMarkdown(escapeHtml(content))}</li>`;
      continue;
    }

    closeList();
    paragraph.push(escapeHtml(line));
  }

  flushParagraph();
  closeList();
  return html;
}
