const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function node(text) {
  return {textContent: text, value: "", hidden: false, children: [],
    append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; }};
}
const nodes = new Map();
const $ = id => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
const evaluation = (id, adapter, time, status = "completed") => ({id, adapter_run: adapter, ended_at: time, kind: "evaluate", status, result: {}});
const reports = {
  base: {adapter_run: null, accuracy: .006, correct: 3, total: 500, items: []},
  trained: {adapter_run: "adapter-a", accuracy: .384, correct: 192, total: 500, items: []},
  newer: {adapter_run: "adapter-a", accuracy: .4, correct: 200, total: 500, items: []},
};
reports.baseline = reports.base;
const context = vm.createContext({
  $, jobs: [evaluation("base", null, 20), evaluation("trained", "adapter-a", 10)],
  comparisonOverride: null, comparisonKey: null, comparisonMessage: "", requestedReport: 0,
  report: null, reportId: null, page: 0,
  api: async path => reports[path.split("=")[1]],
  element: (tag, text) => node(text), percentage: value => `${value * 100}%`, number: String,
  renderResponses() {},
  workbookRenderEvaluation(report, root, details) { $(root).textContent = String(report.with_tools.accuracy); $(details).hidden = false; },
});
const source = fs.readFileSync("dashboard/static/app.js", "utf8");
vm.runInContext(source.slice(source.indexOf("function comparisonReportId("), source.indexOf("function renderResponses(")), context);
const run = code => vm.runInContext(code, context);

(async () => {
  await run("syncComparison()");
  assert.equal(context.report.accuracy, .006);
  $("adapter-select").value = "adapter-a";
  await run("syncComparison()");
  assert.equal(context.report.accuracy, .384);
  assert.ok($("responses-title").textContent.includes("adapter-a".slice(-8)));
  // Polling must not replace the selected adapter with a newer base run.
  context.jobs.unshift(evaluation("other-base", null, 100));
  await run("syncComparison()");
  assert.equal(context.reportId, "trained");
  context.jobs.push(evaluation("newer", "adapter-a", 30), evaluation("failed", "adapter-a", 40, "failed"));
  await run("syncComparison()");
  assert.equal(context.reportId, "newer");
  context.comparisonOverride = "trained";
  await run("syncComparison()");
  assert.equal(context.reportId, "trained");
  context.comparisonOverride = null;
  $("adapter-select").value = "never-evaluated";
  await run("syncComparison()");
  assert.equal(context.report, null);
  assert.equal($("comparison-result").hidden, true);
  assert.ok(context.comparisonMessage.includes("ainda não"));
  context.jobs = [];
  $("adapter-select").value = "";
  await run("syncComparison()");
  assert.equal(context.reportId, "baseline");

  // A slower request must not overwrite a later selection, including empty states.
  context.jobs = [evaluation("base", null, 20), evaluation("trained", "adapter-a", 10)];
  const pending = {};
  context.api = path => new Promise(resolve => { pending[path.split("=")[1]] = resolve; });
  $("adapter-select").value = "adapter-a";
  const first = run("syncComparison()");
  assert.equal(context.report, null);
  $("adapter-select").value = "";
  const second = run("syncComparison()");
  pending.base(reports.base); await second;
  pending.trained(reports.trained); await first;
  assert.equal(context.reportId, "base");
  $("adapter-select").value = "adapter-a";
  const third = run("syncComparison()");
  $("adapter-select").value = "never-evaluated";
  await run("syncComparison()");
  pending.trained(reports.trained); await third;
  assert.equal(context.report, null);
  assert.equal($("comparison-result").hidden, true);
  context.api = async () => { throw new Error("offline"); };
  $("adapter-select").value = "adapter-a";
  await run("syncComparison()");
  assert.equal(context.report, null);
  assert.equal(context.comparisonKey, null);
  assert.ok(context.comparisonMessage.includes("offline"));
  context.api = async () => reports.trained;
  await run("syncComparison()");
  assert.equal(context.report.accuracy, .384);
  const paired = {adapter_run: "adapter-a", total: 500, without_tools: {accuracy: .75, correct: 375}, with_tools: {accuracy: .362, correct: 181}, comparison: {delta_pp: -38.8}, items: [{id: "one", expected: "=1", without_tools: {predicted: "=1", match: true}, with_tools: {predicted: "=2", match: false}}]};
  context.jobs.push({...evaluation("paired", "adapter-a", 50), kind: "tool_evaluate", result: null, tool_result: {total: 500}});
  context.api = async path => { assert.ok(path.startsWith("/api/tool-report")); return paired; };
  await run("syncComparison()");
  assert.equal(context.reportId, "paired");
  assert.equal($("paired-comparison").hidden, false);
  assert.equal(context.report.items[0].predicted, "=2");
  assert.equal(context.report.items[0].baseline_predicted, "=1");
  $("adapter-select").value = "never-evaluated";
  await run("syncComparison()");
  assert.equal($("paired-comparison").hidden, true, "Changing models clears old paired results");
  console.log("Comparison: model-specific results, latest completed run, polling, history, baseline fallback and rapid switches passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
