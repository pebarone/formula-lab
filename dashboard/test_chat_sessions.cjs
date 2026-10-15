const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const local = new Map(), legacy = new Map(), requests = [];
let detail = {status: "running", events: [], chat_response: {text: "=SUM(A1"}};
let detailError = null;
class Node {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = []; this.events = {}; this.value = ""; this.textContent = "";
    this.options = []; this.style = {}; this.dataset = {};
    this.classList = {toggle() {}, add() {}, remove() {}, contains() { return false; }};
    this.htmlWrites = 0;
  }
  set innerHTML(value) { this.html = value; this.htmlWrites++; }
  get innerHTML() { return this.html; }
  append(...nodes) { nodes.forEach(node => { node.parent = this; this.children.push(node); }); }
  appendChild(node) { this.append(node); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); }
  addEventListener(name, callback) { this.events[name] = callback; }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(selector === child.tagName ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  setAttribute() {}
  focus() {}
}
function boot() {
  const nodes = new Map();
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const context = vm.createContext({
    $, console, setTimeout() {},
    document: {querySelectorAll: () => [], createElement: tag => new Node(tag)},
    window: {location: {hash: "#chat"}, addEventListener() {}},
    localStorage: {getItem: key => local.get(key) || null, setItem: (key, value) => local.set(key, value)},
    sessionStorage: {getItem: key => legacy.get(key) || null, setItem: (key, value) => legacy.set(key, value)},
    element(tag, text, className) { const node = new Node(tag); node.textContent = text; node.className = className; return node; },
    online: true, jobs: [], kinds: {}, terminal: new Set(["completed", "failed", "cancelled", "interrupted"]), number: String,
    api: async (path, body) => {
      requests.push({path, body});
      if (!body) { if (detailError) throw detailError; return detail; }
      const messages = body.messages.map(message => ({...message}));
      if (body.attachment_ids.length) messages.at(-1).content += "\nArquivo: vendas.csv\nA1=Região; B1=Vendas";
      return {id: "20260930-220000-aabbccdd", messages, adapter_run: null};
    },
  });
  for (const file of ["chat_render.js", "chat.js"]) vm.runInContext(fs.readFileSync(`dashboard/static/${file}`, "utf8"), context);
  return {$, context, run: code => vm.runInContext(code, context)};
}
async function settle() { await new Promise(resolve => setImmediate(resolve)); }

(async () => {
  const first = boot();
  first.$("chat-input").value = "Some as vendas";
  first.run('currentChat().attachments = [{id:"file-id",name:"vendas.csv",context:"A1=Região; B1=Vendas"}]');
  first.run("updateChatState()"); await first.run("submitChat()");
  assert.equal(first.run("currentChat().run"), "20260930-220000-aabbccdd");
  assert.ok(first.run('currentChat().messages[0].content.includes("A1=Região")'));
  assert.equal(first.run("currentChat().messages[0].display_content"), "Some as vendas");
  const sentExport = first.run("exportChatText(currentChat())");
  assert.ok(sentExport.startsWith("Você\nSome as vendas\nAnexo: vendas.csv"));
  assert.ok(sentExport.includes("Contexto enviado ao modelo:\nSome as vendas\nArquivo: vendas.csv"), "Export must distinguish the visible question from the canonical model context");

  const restored = boot(); await settle();
  assert.equal(restored.run("currentChat().run"), "20260930-220000-aabbccdd", "Pending generation must survive reload");
  assert.equal(restored.$("chat-send-btn").disabled, true);
  detail = {status: "completed", events: [], chat_response: {text: "=SUM(B2:B10)", generated_tokens: 10, generation_seconds: 0.5, generation_mode: "accelerated", prompt_tokens: 80}};
  await restored.run("pollChat()"); await restored.run("pollChat()");
  assert.equal(restored.run("currentChat().messages.length"), 2, "Commit final answer exactly once");
  assert.equal(restored.run("currentChat().run"), null);
  assert.equal(restored.run("currentChat().messages[1].response.generation_mode"), "accelerated");
  assert.equal(restored.run("currentChat().messages[1].response.prompt_tokens"), 80);
  assert.equal(restored.run("chatSpeed(currentChat().messages[1].response)"), "20 tokens/s · 10 tokens · acelerado");
  assert.equal(boot().run("currentChat().messages[1].response.generation_mode"), "accelerated", "Generation mode must survive reload");
  assert.equal(restored.run('chatSpeed({generated_tokens:10,generation_seconds:0.5,generation_mode:"standard"})'), "20 tokens/s · 10 tokens · compatibilidade");
  assert.equal(restored.run('chatSpeed({generated_tokens:10,generation_seconds:0.5})'), "20 tokens/s · 10 tokens", "Legacy replies remain readable");
  restored.$("chat-input").value = "E somente SP?"; restored.run("updateChatState()"); await restored.run("submitChat()");
  const sent = requests.at(-1).body;
  assert.deepEqual(Array.from(sent.messages, message => message.role), ["user", "assistant", "user"]);
  assert.ok(sent.messages[0].content.includes("A1=Região"), "File context must remain in follow-up turns");
  assert.deepEqual(Object.keys(sent.messages[0]).sort(), ["content", "role"]);
  detail = {status: "cancelled", events: [], chat_response: null}; await restored.run("pollChat()");
  assert.equal(restored.$("chat-input").value, "E somente SP?");

  const chosen = restored.run("currentChat().id");
  restored.run('createNewSession("Outra conversa")'); restored.context.chosen = chosen; restored.run("selectChatSession(chosen)");
  const selected = boot(); await settle();
  assert.equal(selected.run("currentChat().id"), chosen, "Restore selected session, not first item");
  assert.equal(selected.$("chat-input").value, "E somente SP?", "Draft must survive reload");

  const html = selected.run('renderChatMarkdown("```excel\\n=SUM(B2:B10)\\n```",true)');
  for (let i = 0; i < 500; i++) assert.equal(selected.run('renderChatMarkdown("```excel\\n=SUM(B2:B10)\\n```",true)'), html);
  assert.equal(selected.run("typeof chatCodeSnippets"), "undefined", "Renderer must not retain copy registry");
  selected.run('updateStreamingAssistant("```excel\\n=SUM(B2:B10)")');
  const writes = selected.run("chatStreamingRow._bubble.htmlWrites");
  selected.run('updateStreamingAssistant("```excel\\n=SUM(B2:B10)")');
  assert.equal(selected.run("chatStreamingRow._bubble.htmlWrites"), writes, "Unchanged stream must not rerender");

  local.clear(); legacy.set("formula-lab-chat", JSON.stringify({history: [{role: "user", content: "Pergunta órfã"}], run: null}));
  const migrated = boot(); await settle();
  assert.equal(migrated.run("currentChat().messages.length"), 0);
  assert.equal(migrated.$("chat-input").value, "Pergunta órfã");
  migrated.run("chatUploading = true; renderChatSessionsList(); updateChatState()");
  assert.ok(migrated.$("chat-sessions-list").querySelectorAll("button").every(button => button.disabled));
  migrated.run("chatUploading = false; updateChatState()");
  assert.ok(migrated.$("chat-sessions-list").querySelectorAll("button").every(button => !button.disabled), "History must become usable after upload finishes");
  local.set("formula-lab-chat-sessions-v3", JSON.stringify({activeId:"malformed", sessions:[{
    id:"malformed", title:42, selection:{}, adapter:4, run:"invalid-run", draft:{}, attachments:{},
    messages:[{role:"user",content:"Pergunta",attachments:{}},{role:"assistant",content:"=SUM(A1:A3)",display_content:42}],
  }]}));
  const malformed = boot(); await settle();
  assert.equal(malformed.run("currentChat().title"), "Pergunta");
  assert.equal(malformed.run("currentChat().selection"), "");
  assert.equal(malformed.run("currentChat().run"), null);
  assert.equal(malformed.run("currentChat().attachments.length"), 0);
  assert.equal(malformed.run("currentChat().messages[0].attachments.length"), 0);
  assert.equal(malformed.$("chat-generation-status").textContent, "Pronto para enviar · execução local");
  malformed.run('currentChat().messages.push({role:"user",content:"Não perca\\nContexto da planilha",display_content:"Não perca",attachments:[{id:"file-id",name:"dados.csv",context:"A1=42"}]}); currentChat().run="20260930-220000-aabbccdd"; saveActiveSession()');
  detailError = Object.assign(new Error("Execução não encontrada"), {status:404});
  await malformed.run("pollChat()");
  assert.equal(malformed.run("currentChat().run"), null);
  assert.equal(malformed.$("chat-input").value, "Não perca");
  assert.equal(malformed.run("currentChat().messages.length"), 2);
  assert.equal(malformed.run("currentChat().attachments[0].name"), "dados.csv");
  assert.equal(malformed.$("chat-send-btn").disabled, false);
  const exported = malformed.run("exportChatText(currentChat())");
  assert.ok(exported.includes("Rascunho (não enviado)") && exported.includes("Não perca") && exported.includes("A1=42"));
  detailError = Object.assign(new Error("Falha temporária"), {status:500});
  malformed.run('currentChat().run="20260930-220000-aabbccdd"'); await malformed.run("pollChat()");
  assert.equal(malformed.run("currentChat().run"), "20260930-220000-aabbccdd", "Temporary server failures must preserve recovery state");
  const named = boot();
  const label = "10.000 exemplos + 1.000 exemplos de tool usage";
  named.context.jobs = [{id:"20261009-120000-abcd1234",kind:"train",status:"completed",has_adapter:true,ended_at:1,label,config:{train_samples:1200}}];
  named.run('currentChat().run=null; currentChat().selection="latest"; currentChat().adapter=null; updateChatState()');
  assert.equal(named.run("chatModelLabel()"), `Qwen · ${label}`);
  assert.equal(named.$("chat-model-select").children.find(option => option.value === "20261009-120000-abcd1234").textContent, label, "Tool-stage name must not become the rehearsal-inclusive sample count");
  named.context.jobs[0].label = "Nome atualizado";
  named.run("updateChatState()");
  assert.equal(named.$("chat-model-select").children.find(option => option.value === "20261009-120000-abcd1234").textContent, "Nome atualizado", "Renaming must refresh options without changing adapter IDs");
  console.log("Chat sessions: reload, exactly-once completion, selected session, drafts, file context, cancellation and bounded streaming passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
