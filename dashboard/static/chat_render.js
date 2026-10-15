"use strict";

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function highlightExcelFormula(code) {
  return code.split(/("(?:[^"]|"")*"|[\p{L}_.]+(?=\()|\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?|\d+(?:\.\d+)?)/gu).map(token => {
    const kind = token.startsWith('"') ? "str" : /^\$?[A-Za-z]{1,3}\$?\d/.test(token) ? "cell" : /^\d/.test(token) ? "num" : /^[\p{L}_.]+$/u.test(token) ? "fn" : "";
    return kind ? `<span class="tok-${kind}">${escapeHtml(token)}</span>` : escapeHtml(token);
  }).join("");
}

function highlightGenericCode(code) {
  return escapeHtml(code);
}

function renderChatMarkdown(rawText, isStreaming = false) {
  if (!rawText) return "";

  let text = String(rawText);

  // During streaming, handle unclosed triple-backtick code block fences
  if (isStreaming) {
    const fences = text.match(/```/g);
    if (fences && fences.length % 2 === 1) {
      text += "\n```";
    }
  }

  const codeBlocks = [];

  // 1. Code blocks with triple backticks
  text = text.replace(/```([a-zA-Z0-9_\-\.\+]*)\n([\s\S]*?)```/g, (match, rawLang, codeContent) => {
    const lang = (rawLang || "").trim().toLowerCase();
    const trimmedCode = codeContent.replace(/\n$/, "");

    let displayLang = lang ? lang.toUpperCase() : "CÓDIGO";
    if (lang === "excel" || lang === "formula" || (!lang && trimmedCode.trim().startsWith("="))) {
      displayLang = "EXCEL";
    }

    let highlighted = "";
    if (displayLang === "EXCEL") {
      highlighted = highlightExcelFormula(trimmedCode);
    } else {
      highlighted = highlightGenericCode(trimmedCode, lang);
    }

    const blockHtml = `
      <div class="chat-code-block">
        <div class="chat-code-header">
          <div class="code-lang-label">
            <span class="code-icon"><svg class="icon-inline" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m16 18 6-6-6-6" /><path d="m8 6-6 6 6 6" /></svg></span>
            <span>${escapeHtml(displayLang)}</span>
          </div>
          <button type="button" class="copy-code-btn" aria-label="Copiar código">
            <svg class="icon-copy" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <rect width="14" height="14" x="8" y="8" rx="2" ry="2" />
              <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />
            </svg>
            <span class="btn-copy-label">Copiar código</span>
          </button>
        </div>
        <pre class="chat-code-pre"><code>${highlighted}</code></pre>
      </div>`;

    const placeholder = `___CHAT_BLOCK_${codeBlocks.length}___`;
    codeBlocks.push(blockHtml);
    return `\n\n${placeholder}\n\n`;
  });

  // 2. Parse Markdown Tables
  const tableRegex = /((?:\|[^\n]+\|\r?\n)(?:\|[-: |]+\|\r?\n)(?:\|[^\n]+\|\r?\n?)+)/g;
  text = text.replace(tableRegex, (tableBlock) => {
    const lines = tableBlock.trim().split(/\r?\n/).filter(l => l.trim().startsWith("|"));
    if (lines.length < 2) return tableBlock;

    const headers = lines[0].split("|").slice(1, -1).map(c => c.trim());
    const rows = lines.slice(2).map(line => line.split("|").slice(1, -1).map(c => c.trim()));

    let tableHtml = `<div class="chat-table-wrap"><table class="chat-table"><thead><tr>`;
    headers.forEach(h => {
      tableHtml += `<th>${escapeHtml(h)}</th>`;
    });
    tableHtml += `</tr></thead><tbody>`;
    rows.forEach(r => {
      tableHtml += `<tr>`;
      r.forEach(cell => {
        tableHtml += `<td>${escapeHtml(cell)}</td>`;
      });
      tableHtml += `</tr>`;
    });
    tableHtml += `</tbody></table></div>`;

    const placeholder = `___CHAT_BLOCK_${codeBlocks.length}___`;
    codeBlocks.push(tableHtml);
    return `\n\n${placeholder}\n\n`;
  });

  // 3. Process normal markdown lines
  const lines = text.split(/\r?\n/);
  const output = [];
  let inUl = false, inOl = false;

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];

    if (/^___CHAT_BLOCK_\d+___$/.test(line.trim())) {
      if (inUl) { output.push("</ul>"); inUl = false; }
      if (inOl) { output.push("</ol>"); inOl = false; }
      output.push(line.trim());
      continue;
    }

    // Standalone Excel formula line starting with "="
    if (/^\s*=[A-Za-z_]+\(.*\)\s*$/.test(line)) {
      if (inUl) { output.push("</ul>"); inUl = false; }
      if (inOl) { output.push("</ol>"); inOl = false; }
      const rawFormula = line.trim();
      const highlighted = highlightExcelFormula(rawFormula);
      output.push(`
        <div class="formula-callout">
          <div class="formula-callout-body">
            <code class="formula-callout-code">${highlighted}</code>
          </div>
          <button type="button" class="copy-code-btn" aria-label="Copiar fórmula">
            <svg class="icon-copy" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <rect width="14" height="14" x="8" y="8" rx="2" ry="2" />
              <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />
            </svg>
            <span class="btn-copy-label">Copiar</span>
          </button>
        </div>`);
      continue;
    }

    // Headers
    if (/^####\s+(.*)/.test(line)) {
      if (inUl) { output.push("</ul>"); inUl = false; }
      if (inOl) { output.push("</ol>"); inOl = false; }
      output.push(`<h4>${parseInline(line.replace(/^####\s+/, ""), isStreaming)}</h4>`);
      continue;
    }
    if (/^###\s+(.*)/.test(line)) {
      if (inUl) { output.push("</ul>"); inUl = false; }
      if (inOl) { output.push("</ol>"); inOl = false; }
      output.push(`<h3>${parseInline(line.replace(/^###\s+/, ""), isStreaming)}</h3>`);
      continue;
    }
    if (/^##\s+(.*)/.test(line)) {
      if (inUl) { output.push("</ul>"); inUl = false; }
      if (inOl) { output.push("</ol>"); inOl = false; }
      output.push(`<h3>${parseInline(line.replace(/^##\s+/, ""), isStreaming)}</h3>`);
      continue;
    }
    if (/^#\s+(.*)/.test(line)) {
      if (inUl) { output.push("</ul>"); inUl = false; }
      if (inOl) { output.push("</ol>"); inOl = false; }
      output.push(`<h3>${parseInline(line.replace(/^#\s+/, ""), isStreaming)}</h3>`);
      continue;
    }

    // Blockquote
    if (/^>\s*(.*)/.test(line)) {
      if (inUl) { output.push("</ul>"); inUl = false; }
      if (inOl) { output.push("</ol>"); inOl = false; }
      output.push(`<blockquote class="chat-quote">${parseInline(line.replace(/^>\s*/, ""), isStreaming)}</blockquote>`);
      continue;
    }

    // Unordered list
    if (/^[-*]\s+(.*)/.test(line)) {
      if (inOl) { output.push("</ol>"); inOl = false; }
      if (!inUl) { output.push("<ul>"); inUl = true; }
      output.push(`<li>${parseInline(line.replace(/^[-*]\s+/, ""), isStreaming)}</li>`);
      continue;
    }

    // Ordered list
    if (/^\d+\.\s+(.*)/.test(line)) {
      if (inUl) { output.push("</ul>"); inUl = false; }
      if (!inOl) { output.push("<ol>"); inOl = true; }
      output.push(`<li>${parseInline(line.replace(/^\d+\.\s+/, ""), isStreaming)}</li>`);
      continue;
    }

    // Empty line
    if (!line.trim()) {
      if (inUl) { output.push("</ul>"); inUl = false; }
      if (inOl) { output.push("</ol>"); inOl = false; }
      continue;
    }

    // Regular paragraph
    if (inUl) { output.push("</ul>"); inUl = false; }
    if (inOl) { output.push("</ol>"); inOl = false; }
    output.push(`<p>${parseInline(line, isStreaming)}</p>`);
  }

  if (inUl) output.push("</ul>");
  if (inOl) output.push("</ol>");

  let result = output.join("\n");

  // Re-inject code blocks
  codeBlocks.forEach((block, idx) => {
    result = result.replace(`___CHAT_BLOCK_${idx}___`, block);
  });

  return result;
}

function parseInline(str, isStreaming = false) {
  let target = str;
  if (isStreaming) {
    const boldCount = (target.match(/\*\*/g) || []).length;
    if (boldCount % 2 === 1) target += "**";
  }
  let esc = escapeHtml(target);
  esc = esc.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");
  esc = esc.replace(/\*(.*?)\*/g, "<em>$1</em>");
  esc = esc.replace(/`([^`]+)`/g, (m, code) => `<code class="chat-inline-code">${code}</code>`);
  return esc;
}

async function copyToClipboard(text, button) {
  let success = false;
  if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      success = true;
    } catch (_) {
      success = false;
    }
  }
  if (!success && typeof document !== "undefined" && document.createElement) {
    try {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      success = true;
    } catch (e) {
      console.error("Falha ao copiar:", e);
    }
  }
  if (success && button) {
    const originalLabel = button.querySelector ? (button.querySelector(".btn-copy-label")?.textContent || "Copiar código") : "Copiar código";
    if (button.classList && button.classList.add) button.classList.add("copied");
    const label = button.querySelector ? button.querySelector(".btn-copy-label") : null;
    if (label) label.textContent = "Copiado!";
    setTimeout(() => {
      if (button.classList && button.classList.remove) button.classList.remove("copied");
      if (label) label.textContent = originalLabel;
    }, 2000);
  }
}

function addChatMessage(role, contentText, attachments = [], response = null) {
  const container = $("chat-messages");
  if (!container) return null;

  const row = document.createElement("div");
  row.className = `chat-msg-row ${role}`;

  if (role === "user") {
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble user";
    const p = document.createElement("p");
    p.textContent = contentText;
    bubble.appendChild(p);
    if (attachments.length) {
      const files = element("div", undefined, "chat-message-files");
      attachments.forEach(file => files.append(element("span", file.name, "chat-file-label")));
      bubble.appendChild(files);
    }
    row.appendChild(bubble);
  } else {
    const wrap = document.createElement("div");
    wrap.className = "chat-msg-content-wrap";

    const meta = document.createElement("div");
    meta.className = "chat-msg-meta";
    meta.textContent = `${chatModelLabel()}${response ? ` · ${chatSpeed(response)}` : ""}`;

    const bubble = document.createElement("div");
    bubble.className = "chat-bubble assistant";
    bubble.innerHTML = renderChatMarkdown(contentText, false);

    const actions = document.createElement("div");
    actions.className = "chat-msg-actions";
    const copyMsgBtn = document.createElement("button");
    copyMsgBtn.type = "button";
    copyMsgBtn.className = "chat-action-btn";
    copyMsgBtn.innerHTML = `<svg class="icon-inline" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2" /><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" /></svg> <span class="btn-copy-label">Copiar resposta</span>`;
    copyMsgBtn.addEventListener("click", () => copyToClipboard(contentText, copyMsgBtn));
    actions.appendChild(copyMsgBtn);

    wrap.appendChild(meta);
    wrap.appendChild(bubble);
    wrap.appendChild(actions);

    row.appendChild(wrap);
  }

  container.appendChild(row);
  if (container.scrollTop !== undefined) {
    container.scrollTop = container.scrollHeight;
  }
  return row;
}

// Streaming Assistant Row Management
let chatStreamingRow = null;

function createStreamingAssistantRow() {
  const container = $("chat-messages");
  if (!container) return null;

  const row = document.createElement("div");
  row.className = "chat-msg-row assistant is-streaming";

  const wrap = document.createElement("div");
  wrap.className = "chat-msg-content-wrap";

  const meta = document.createElement("div");
  meta.className = "chat-msg-meta";
  const modelText = document.createElement("span");
  modelText.textContent = chatModelLabel();
  const liveTag = document.createElement("span");
  liveTag.className = "streaming-tag";
  liveTag.textContent = "Gerando…";
  meta.appendChild(modelText);
  meta.appendChild(liveTag);

  const bubble = document.createElement("div");
  bubble.className = "chat-bubble assistant";
  bubble.innerHTML = `<span class="streaming-cursor" aria-hidden="true"></span>`;

  wrap.appendChild(meta);
  wrap.appendChild(bubble);

  row.appendChild(wrap);

  row._bubble = bubble;
  row._wrap = wrap;
  row._meta = meta;

  container.appendChild(row);
  if (container.scrollTop !== undefined) {
    container.scrollTop = container.scrollHeight;
  }
  return row;
}

function updateStreamingAssistant(text) {
  if (chatStreamingRow?._text === text) return;
  if (!chatStreamingRow) {
    chatStreamingRow = createStreamingAssistantRow();
  }
  if (!chatStreamingRow) return;
  const bubble = chatStreamingRow._bubble || (chatStreamingRow.querySelector ? chatStreamingRow.querySelector(".chat-bubble.assistant") : null);
  if (!bubble) return;
  const container = $("chat-messages");
  const nearBottom = container && container.scrollHeight
    ? (container.scrollHeight - container.scrollTop - container.clientHeight < 120)
    : true;

  chatStreamingRow._text = text;
  const formattedHtml = renderChatMarkdown(text, true);
  bubble.innerHTML = formattedHtml + `<span class="streaming-cursor" aria-hidden="true"></span>`;

  if (nearBottom && container && container.scrollHeight) {
    container.scrollTop = container.scrollHeight;
  }
}

function finalizeStreamingAssistant(text, response) {
  if (!chatStreamingRow) {
    addChatMessage("assistant", text);
    return;
  }
  if (chatStreamingRow.classList && chatStreamingRow.classList.remove) {
    chatStreamingRow.classList.remove("is-streaming");
  }
  const wrap = chatStreamingRow._wrap || (chatStreamingRow.querySelector ? chatStreamingRow.querySelector(".chat-msg-content-wrap") : null);
  const meta = chatStreamingRow._meta || (chatStreamingRow.querySelector ? chatStreamingRow.querySelector(".chat-msg-meta") : null);
  const bubble = chatStreamingRow._bubble || (chatStreamingRow.querySelector ? chatStreamingRow.querySelector(".chat-bubble.assistant") : null);

  if (meta && meta.querySelector) {
    const liveTag = meta.querySelector(".streaming-tag");
    if (liveTag) {
      const speed = chatSpeed(response);
      if (speed) {
        liveTag.className = "speed-tag";
        liveTag.textContent = speed;
      } else {
        liveTag.remove();
      }
    }
  }

  if (bubble) {
    bubble.innerHTML = renderChatMarkdown(text, false);
  }

  if (wrap) {
    const actions = document.createElement("div");
    actions.className = "chat-msg-actions";
    const copyMsgBtn = document.createElement("button");
    copyMsgBtn.type = "button";
    copyMsgBtn.className = "chat-action-btn";
    copyMsgBtn.innerHTML = `<svg class="icon-inline" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2" /><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" /></svg> <span class="btn-copy-label">Copiar resposta</span>`;
    copyMsgBtn.addEventListener("click", () => copyToClipboard(text, copyMsgBtn));
    actions.appendChild(copyMsgBtn);
    wrap.appendChild(actions);
  }

  chatStreamingRow = null;

  const container = $("chat-messages");
  if (container && container.scrollHeight) {
    container.scrollTop = container.scrollHeight;
  }
}


// Delegation keeps copy actions tied to the current DOM, without retaining old streaming blocks.
$("chat-messages")?.addEventListener("click", event => {
  const button = event.target.closest?.(".copy-code-btn");
  if (!button) return;
  const code = button.closest(".chat-code-block, .formula-callout")?.querySelector("code");
  if (code) copyToClipboard(code.textContent, button);
});
