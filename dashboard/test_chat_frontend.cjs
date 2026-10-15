// Exercise chat state and rendering without a browser or GPU.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor() {
    this.children = []; this.events = {}; this.value = ""; this.textContent = "";
    this.options = [{}, {}]; this.style = {}; this.dataset = {};
    this.classList = {toggle() {}, add() {}, remove() {}};
  }
  append(...nodes) { nodes.forEach(node => { node.parent = this; this.children.push(node); }); }
  appendChild(node) { this.append(node); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); }
  get lastElementChild() { return this.children.at(-1); }
  addEventListener(name, callback) { this.events[name] = callback; }
  querySelectorAll() { return []; }
  focus() {}
}

const nodes = new Map();
const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
const requests = [];
let detail;
const context = vm.createContext({
  $, console, setTimeout() {},
  document: {querySelectorAll: () => [], createElement: () => new Node()},
  window: {location: {hash: "#chat"}, addEventListener() {}, scrollTo() {}},
  sessionStorage: {getItem: () => null, setItem() {}},
  element(tag, text, className) { const node = new Node(); node.textContent = text; node.className = className; return node; },
  online: true, jobs: [], kinds: {train: "Treinamento"},
  terminal: new Set(["completed", "failed", "cancelled", "interrupted"]),
  number: String, elapsed: value => `${value}s`,
  api: async (path, body) => { requests.push({path, body}); return body ? {id: "chat-id", adapter_run: body.adapter_run ? "trained-id" : null} : detail; },
});
vm.runInContext(fs.readFileSync("dashboard/static/chat_render.js", "utf8"), context);
vm.runInContext(fs.readFileSync("dashboard/static/chat.js", "utf8"), context);
const run = code => vm.runInContext(code, context);

(async () => {
  assert.equal($("chat-send-btn").disabled, true);
  $("chat-input").value = "Sum A1:A3";
  run("updateChatState()");
  assert.equal($("chat-send-btn").disabled, false);
  await run("submitChat()");
  assert.equal(requests.at(-1).body.adapter_run, null);
  assert.equal($("chat-model-select").disabled, true);
  detail = {status: "completed", created_at: Date.now() / 1000, events: [], chat_response: {text: "=SUM(A1:A3)", generated_tokens: 8}};
  await run("pollChat()");
  assert.equal(run("currentChat().messages.length"), 2);
  assert.equal(run("currentChat().run"), null);

  context.jobs = [{id: "trained-id", kind: "train", status: "completed", has_adapter: true, ended_at: 20, config: {train_samples: 5000}}];
  $("chat-model-select").value = "latest";
  $("chat-model-select").events.change();
  assert.equal(run("currentChat().messages.length"), 0);
  $("chat-input").value = "First";
  run("updateChatState()");
  await run("submitChat()");
  assert.equal(requests.at(-1).body.adapter_run, "latest");
  await run("pollChat()");
  $("chat-input").value = "Follow-up";
  run("updateChatState()");
  await run("submitChat()");
  assert.equal(requests.at(-1).body.adapter_run, "trained-id");
  assert.equal(requests.at(-1).body.messages.length, 3);
  detail = {...detail, status: "cancelled", chat_response: null};
  await run("pollChat()");
  assert.equal(run("currentChat().messages.length"), 2);
  assert.equal($("chat-input").value, "Follow-up");

  context.jobs.push({id: "busy", kind: "train", status: "running"});
  run("updateChatState()");
  assert.equal($("chat-send-btn").disabled, true);
  assert.equal($("chat-running-banner").hidden, false);

  for (const malicious of ['<img src=x onerror=alert(1)>', '___CHAT_BLOCK_0___<script>alert(1)</script>', '```excel\n=SUM(A1,"<img src=x>")\n```']) {
    context.untrusted = malicious;
    const html = run("renderChatMarkdown(untrusted)");
    assert.ok(!html.includes("<img") && !html.includes("<script"), html);
  }
  const highlighted = run('highlightExcelFormula("=SUM(A1:A3)")');
  assert.ok(highlighted.includes('<span class="tok-fn">SUM</span>'));
  assert.ok(run('highlightExcelFormula("=ÍNDICE(A1:A3;1)")').includes('<span class="tok-fn">ÍNDICE</span>'));
  // Test streaming markdown formatting on unclosed blocks
  context.streamingInput = "Aqui está a fórmula:\n```excel\n=SOMA(B2:B10)";
  const streamingOpenCode = run("renderChatMarkdown(streamingInput, true)");
  assert.ok(streamingOpenCode.includes('chat-code-block'), "Unclosed code block should be formatted during streaming");
  assert.ok(streamingOpenCode.includes('tok-fn'), "Formula inside open code block should be syntax-highlighted");

  // Test chat history sessions management
  assert.ok(run("chatSessions.length") >= 1, "Should have at least 1 session initialized");
  const initialSessionCount = run("chatSessions.length");
  run('createNewSession("Sessão de Teste")');
  assert.equal(run("chatSessions.length"), initialSessionCount + 1);
  const activeTitle = run('chatSessions.find(s => s.id === activeChatSessionId)?.title');
  assert.equal(activeTitle, "Sessão de Teste");

  const titleGen = run('generateSessionTitle("Na coluna B estão as vendas de SP...")');
  assert.ok(titleGen.startsWith("Na coluna B"), "Title should be derived from user prompt");

  console.log("Chat frontend: base, latest, pinned adapter, reset, cancellation, busy state, streaming markdown and history passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
