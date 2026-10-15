// Exercise the workbook bench against mocked Excel and Tabulator APIs.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.events = {};
    this.attributes = {};
    this.value = "";
    this.textContent = "";
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.style = {};
    this.dataset = {};
    this.classList = {
      add: name => { if (!this.className.split(/\s+/).includes(name)) this.className = (this.className + " " + name).trim(); },
      remove: name => { this.className = this.className.split(/\s+/).filter(item => item !== name).join(" "); },
      toggle: (name, force) => {
        const has = this.className.split(/\s+/).includes(name);
        const on = force === undefined ? !has : Boolean(force);
        if (on) this.classList.add(name); else this.classList.remove(name);
        return on;
      },
      contains: name => this.className.split(/\s+/).includes(name),
    };
  }
  append(...nodes) { nodes.forEach(node => { node.parent = this; this.children.push(node); }); }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, callback) { this.events[name] = callback; }
  focus() {}
  show() { this.open = true; }
  close() { this.open = false; }
  click() { this.clicked = true; }
  querySelector(selector) {
    const matches = node => selector.startsWith(".") ? node.className.split(/\s+/).includes(selector.slice(1)) : node.tagName === selector;
    for (const child of this.children) {
      if (matches(child)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
  querySelectorAll(selector) {
    const matches = node => selector.startsWith(".") ? node.className.split(/\s+/).includes(selector.slice(1)) : node.tagName === selector;
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  get lastElementChild() { return this.children.at(-1) || null; }
}

class MockTabulator {
  constructor(host, options) { this.host = host; this.options = options; this.data = options.data; this.events = {}; MockTabulator.instance = this; }
  on(name, callback) { this.events[name] = callback; }
  setData(data) { this.data = data; }
  redraw() {}
}

const saved = new Map();
let requests = [];
let failChange = false;
let testResult = {ok: true, formula: "=SUM(A1:A2)", formula_local: "=SOMA(A1:A2)", value: 42, error: null};
let detail = {status: "running", events: [], chat_response: null};
const pairedReport = {total: 1, without_tools: {correct: 0, accuracy: 0}, with_tools: {correct: 1, accuracy: 1}, comparison: {delta_pp: 100, gained: 1, lost: 0}, items: [{id: "one", without_tools: {match: false, predicted: "=1"}, with_tools: {match: true, predicted: "=2"}}]};
let snapshot = {
  id: "book-1", name: "Rascunho", revision: 0, engine: "Microsoft Excel",
  sheets: [{name: "Dados", rows: 3, columns: 3, cells: [{address: "A1", value: 20}, {address: "A2", value: 22}]}],
  warnings: [],
};

function cloneSnapshot() { return JSON.parse(JSON.stringify(snapshot)); }
function boot() {
  requests = [];
  const nodes = new Map();
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const context = vm.createContext({
    $, console, setTimeout() {}, clearTimeout() {},
    document: {querySelectorAll: () => [], createElement: tag => new Node(tag), body: new Node("body")},
    window: {location: {hash: "#chat"}, addEventListener() {}, matchMedia: () => ({matches: false}), scrollTo() {}},
    history: {pushState() {}},
    localStorage: {getItem: key => saved.get(key) || null, setItem: (key, value) => saved.set(key, value)},
    sessionStorage: {getItem: () => null, setItem() {}},
    element(tag, text, className) { const node = new Node(tag); if (text !== undefined) node.textContent = text; node.className = className || ""; return node; },
    Tabulator: MockTabulator,
    FileReader: class { readAsDataURL() { this.result = "data:application/octet-stream;base64,dGVzdA=="; this.onload(); } },
    online: true, jobs: [], kinds: {}, terminal: new Set(["completed", "failed", "cancelled", "interrupted"]),
    number: value => String(value), elapsed: value => String(value) + "s",
    api: async (path, body) => {
      requests.push({path, body: body ? JSON.parse(JSON.stringify(body)) : undefined});
      if (path === "/api/excel") return {available: true, engine: "Microsoft Excel"};
      if (path === "/api/attachments") return {id: "xlsx-upload", name: body.name, kind: "xlsx", context: "A1=20", warnings: []};
      if (path === "/api/workbooks/create") return cloneSnapshot();
      if (path.startsWith("/api/workbooks?id=")) return cloneSnapshot();
      if (path === "/api/workbooks/change") {
        if (failChange) throw Object.assign(new Error("Falha temporária"), {status: 503});
        assert.equal(body.expected_revision, snapshot.revision);
        for (const change of body.changes) {
          const sheet = snapshot.sheets.find(item => item.name === change.sheet);
          let cell = sheet.cells.find(item => item.address === change.cell);
          if (!cell) { cell = {address: change.cell}; sheet.cells.push(cell); }
          if (Object.hasOwn(change, "formula")) { cell.formula = change.formula; cell.value = 42; }
          else { delete cell.formula; cell.value = change.value; }
        }
        snapshot.revision++;
        return cloneSnapshot();
      }
      if (path === "/api/workbooks/test") return {...testResult};
      if (path === "/api/start") return {id: "20261009-120000-abcdef12", adapter_run: body.adapter_run};
      if (path.startsWith("/api/run?id=")) return detail;
      if (path.startsWith("/api/tool-report?id=")) return pairedReport;
      throw new Error("Unexpected API path " + path);
    },
  });
  for (const file of ["chat_render.js", "workbook.js", "chat.js"]) vm.runInContext(fs.readFileSync("dashboard/static/" + file, "utf8"), context);
  return {$, context, run: code => vm.runInContext(code, context)};
}
async function settle() { await new Promise(resolve => setImmediate(resolve)); }

(async () => {
  const first = boot();
  await settle();
  assert.equal(first.$("workbook-engine-status").textContent, "Microsoft Excel disponível");
  await first.run("workbookCreate(null)");
  assert.equal(first.run("currentChat().workbookId"), "book-1", "New workbook ID should belong to the active chat");
  assert.equal(first.run("workbookSelected.sheet"), "Dados");
  const bench = first.$("workbook-bench");
  first.run("workbookOpenWindow()");
  assert.equal(first.$("workbook-window").open, true);
  assert.equal(bench.parent, first.$("workbook-window"));
  first.run("workbookCloseWindow()");
  assert.equal(bench.parent, first.$("workbook-dock"));
  assert.equal(first.run("currentChat().workbookId"), "book-1", "Moving the workbook preserves its session");
  const gridCell = {getRow: () => ({getData: () => ({__row: 3})}), getField: () => "C", getValue: () => "7"};
  MockTabulator.instance.events.cellClick({}, gridCell);
  assert.equal(first.run("workbookSelected.cell"), "C3", "Tabulator 6 event selects the actual cell");
  MockTabulator.instance.events.cellEdited(gridCell);
  assert.equal(first.run("workbookPendingChanges.size"), 1, "Tabulator 6 edit event queues a save");
  const creations = requests.filter(request => request.path === "/api/workbooks/create").length;
  await first.run('workbookCreate("new-attachment")');
  assert.equal(requests.filter(request => request.path === "/api/workbooks/create").length, creations, "Import must preserve unsaved edits");
  assert.equal(first.run("workbookPendingChanges.size"), 1);
  assert.equal(first.run('workbookPendingChanges.get(workbookCellKey("Dados", "C3")).value'), 7, "Numeric grid input stays numeric in Excel");
  assert.equal(first.run('workbookLiteralValue("007")'), "007");
  assert.equal(first.run('workbookLiteralValue("1234567890123456")'), "1234567890123456");
  assert.equal(first.run('workbookLiteralValue("\'7")'), "7", "Apostrophe explicitly keeps text");
  first.run('currentChat().attachments = [{id:"csv-1",name:"dados.csv"},{id:"tsv-1",name:"dados.tsv"}]; workbookRenderAttachmentOptions()');
  assert.deepEqual(first.$("workbook-attachment-select").children.map(option => option.value), ["", "csv-1", "tsv-1"]);
  first.run('currentChat().attachments = []');

  first.run('workbookSetSelectedCell("Dados", "C3")');
  first.$("workbook-cell-entry").value = "=";
  first.run("workbookSetSelectedValue()");
  const queuedLiteral = first.run('workbookPendingChanges.get(workbookCellKey("Dados", "C3"))');
  assert.equal(queuedLiteral.value, "=", "A literal equals sign stays a value");
  assert.equal(Object.hasOwn(queuedLiteral, "formula"), false, "Grid/value entry must not infer a formula");
  await first.run("workbookApplyChanges()");
  const firstWrite = requests.filter(request => request.path === "/api/workbooks/change");
  assert.equal(firstWrite.length, 1, "One Apply action should submit once");
  assert.deepEqual(firstWrite[0].body.changes, [{sheet: "Dados", cell: "C3", value: "="}]);
  await first.run("workbookApplyChanges()");
  assert.equal(requests.filter(request => request.path === "/api/workbooks/change").length, 1, "A saved edit is not submitted again");
  assert.equal(snapshot.sheets[0].cells.find(cell => cell.address === "C3").value, "=");

  first.run('workbookSetSelectedCell("Dados", "B4")');
  first.$("workbook-cell-entry").value = "keep this draft";
  first.run("workbookSetSelectedValue()");
  failChange = true;
  await first.run("workbookApplyChanges()");
  assert.equal(first.run("workbookPendingChanges.size"), 1, "Failed saves preserve edits for retry");
  assert.equal(first.run('workbookGridRows()[3].B'), "keep this draft", "Failed save leaves the typed value visible in the grid");
  assert.match(first.$("workbook-notice").textContent, /continua na grade/);
  failChange = false;
  await first.run("workbookApplyChanges()");
  assert.equal(first.run("workbookPendingChanges.size"), 0);

  first.run('workbookSetSelectedCell("Dados", "D4")');
  first.$("workbook-cell-entry").value = "=SUM(A1:A2)";
  const writesBeforeTest = requests.filter(request => request.path === "/api/workbooks/change").length;
  await first.run("workbookTestSelectedFormula()");
  const preview = requests.filter(request => request.path === "/api/workbooks/test").at(-1);
  assert.deepEqual(preview.body, {id: "book-1", sheet: "Dados", cell: "D4", formula: "=SUM(A1:A2)"});
  assert.equal(requests.filter(request => request.path === "/api/workbooks/change").length, writesBeforeTest, "Formula preview does not modify the workbook");
  assert.match(first.$("workbook-notice").textContent, /→ 42/);
  testResult = {ok: false, formula: "=BAD()", error: "#NAME?", value: "#NAME?"};
  first.$("workbook-cell-entry").value = "=BAD()";
  await first.run("workbookTestSelectedFormula()");
  assert.match(first.$("workbook-notice").textContent, /#NAME\?/);
  assert.equal(first.$("workbook-notice").classList.contains("error"), true, "Excel formula errors are presented as errors");

  first.run('currentChat().messages.push({role:"user",content:"Mensagem anterior"},{role:"assistant",content:"Resposta anterior"}); saveActiveSession(); renderActiveChatMessages()');
  const sessionBeforeToggle = first.run("currentChat().id");
  first.$("chat-use-excel-tools").checked = true;
  first.$("chat-use-excel-tools").events.change();
  assert.equal(first.run("currentChat().id"), sessionBeforeToggle, "Changing tool mode keeps the current chat");
  assert.equal(first.run("currentChat().messages.length"), 2, "Changing tool mode preserves existing messages");
  assert.equal(first.run("currentChat().useExcelTools"), true);
  first.$("chat-input").value = "Confira a fórmula";
  first.run("updateChatState()");
  await first.run("submitChat()");
  const chatStart = requests.findLast(request => request.path === "/api/start");
  assert.equal(chatStart.body.workbook_id, "book-1");
  assert.equal(chatStart.body.use_excel_tools, true);
  assert.equal(chatStart.body.messages.length, 3, "Workbook tool settings are request fields, not appended user instructions");

  detail = {status: "completed", events: [], chat_response: {
    text: "Fórmula testada.", tools_used: false, tool_calls: 0, tool_trace: [], workbook_id: "book-1", workbook_revision: 2,
    generated_tokens: 4, generation_seconds: 0.2, generation_mode: "accelerated",
  }};
  await first.run("pollChat()");
  assert.equal(first.run("currentChat().messages.at(-1).response.tools_used"), false);
  assert.equal(first.run("currentChat().messages.at(-1).response.workbook_revision"), 2);
  assert.equal(first.$("chat-messages").querySelector(".workbook-tool-trace").children[0].textContent, "Ferramentas do Excel · 0 chamadas");

  const restored = boot();
  await settle();
  assert.equal(restored.run("currentChat().workbookId"), "book-1", "Workbook association restores with the chat");
  assert.equal(restored.run("currentChat().useExcelTools"), true, "Tool preference restores with the chat");
  assert.equal(restored.run("currentChat().messages.at(-1).response.workbook_id"), "book-1");
  assert.equal(restored.run("currentChat().messages.at(-1).response.workbook_revision"), 2);
  assert.equal(restored.run("currentChat().messages.at(-1).response.tools_used"), false);
  assert.equal(restored.$("workbook-grid").children.length, 0, "Tabulator owns rendering without hand-built formula evaluation");

  const traceRow = new Node();
  const traceWrap = new Node();
  traceWrap.className = "chat-msg-content-wrap";
  traceRow.append(traceWrap);
  restored.context.traceRow = traceRow;
  restored.run('workbookRenderToolTrace(traceRow, {tools_used:true, tool_calls:1, tool_trace:[{name:"test_formula",arguments:{formula:"=1"},result:"<script>text</script>"}]})');
  const trace = traceWrap.querySelector(".workbook-tool-trace");
  assert.ok(trace);
  assert.equal(trace.querySelector("strong").textContent, "Testar fórmula no Excel");
  assert.equal(trace.querySelectorAll("pre").some(pre => pre.textContent.includes("<script>text</script>")), true, "Tool output is rendered as text");

  restored.run('currentChat().toolEvalRunId = "20261009-120000-abcdef12"');
  detail = {status: "completed", events: [], tool_report: {total: 1}};
  requests = [];
  await restored.run('workbookPollEvaluation("20261009-120000-abcdef12")');
  assert.equal(requests.filter(request => request.path.startsWith("/api/tool-report")).length, 1, "Full report is fetched only after the run completes");
  assert.equal(restored.$("workbook-evaluation-results").hidden, false);
  const upload = boot();
  await settle();
  await upload.run('attachChatFiles([{name:"dados.xlsx",size:10}])');
  assert.equal(requests.some(request => request.path === "/api/workbooks/create" && request.body.attachment_id === "xlsx-upload"), true, "XLSX upload must populate the workbook without a manual import");
  assert.equal(upload.$("workbook-window").open, true, "Upload opens the large spreadsheet window");
  assert.equal(upload.run('workbookFindCell("Dados", "A1").value'), 20);
  const reads = requests.filter(request => request.path.startsWith("/api/workbooks?id=")).length;
  upload.run('workbookRefreshAfterChat({tools_used:true,complete:false,workbook_id:"book-1",tool_trace:[{name:"excel_write_cells",result:{ok:true,result:{revision:8}}}]})');
  await settle();
  assert.equal(requests.filter(request => request.path.startsWith("/api/workbooks?id=")).length, reads + 1, "Completed AI writes refresh the grid before the final answer");
  upload.run('workbookRefreshAfterChat({tools_used:true,complete:false,workbook_id:"book-1",tool_trace:[{name:"excel_write_cells",result:{ok:true,result:{revision:8}}}]})');
  assert.equal(requests.filter(request => request.path.startsWith("/api/workbooks?id=")).length, reads + 1, "Polling the same revision does not refetch the grid");
  console.log("Workbook frontend: Tabulator events, CSV/TSV import, report fetch, restore, literal values, exact-once save, save recovery, formula preview/errors, chat metadata and safe traces passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
