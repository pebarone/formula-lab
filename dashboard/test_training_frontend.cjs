const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("dashboard/static/app.js", "utf8");
const nodes = {
  "train-adapter-select": {value: "parent-id"}, "adapter-select": {value: "evaluation-id"},
  "train-adapter-hint": {}, "monitor-title": {scrollIntoView() {}},
};
const inputs = {lora_r: {value: 8}, lora_alpha: {value: 16}, exclude_seen: {checked: true}};
const calls = [];
const context = vm.createContext({
  $: id => nodes[id], number: String, jobs: [{id: "parent-id", config: {lora_r: 4, lora_alpha: 8}}],
  form: {elements: {namedItem: key => inputs[key]}, reportValidity: () => true},
  pending: false, selectedId: null, lastDetail: null, comparisonOverride: null,
  buttons() {}, notice() {}, refresh: async () => {},
  localStorage: {setItem() {}}, config: () => ({learning_rate: 0.0002}),
  api: async (path, body) => { calls.push(body); return {id: "new-run"}; },
});
vm.runInContext(source.slice(source.indexOf("function updateTrainingSource()"), source.indexOf("function svgNode(")), context);
vm.runInContext(source.slice(source.indexOf("async function start("), source.indexOf("async function stop(")), context);
const run = code => vm.runInContext(code, context);

(async () => {
  const toolSummary = run('selectionDescription("train", {selected:1200, max_tokens:1536, tool_usage:{selected:1000}, rehearsal:{selected:200}}, true)');
  assert.ok(toolSummary[0].includes("1000 exemplos novos de ferramentas + 200 de revisão"));
  assert.ok(run('selectionDescription("validation", {selected:100,max_tokens:1400},true)')[0].includes("100 exemplos separados"));
  run("updateTrainingSource()");
  assert.equal(inputs.lora_r.value, 4);
  assert.equal(inputs.lora_alpha.value, 8);
  assert.equal(inputs.lora_r.disabled, true);
  assert.ok(nodes["train-adapter-hint"].textContent.includes("parent-id"));
  assert.ok(nodes["train-adapter-hint"].textContent.includes("toda a cadeia"));
  inputs.exclude_seen.checked = false;
  run("updateTrainingSource()");
  assert.ok(nodes["train-adapter-hint"].textContent.includes("Pode repetir"));
  await run('start("train")');
  assert.equal(calls.at(-1).adapter_run, "parent-id");
  assert.equal(calls.at(-1).config.learning_rate, 0.0002);
  await run('start("prepare")');
  assert.equal(calls.at(-1).adapter_run, "parent-id");
  await run('start("evaluate")');
  assert.equal(calls.at(-1).adapter_run, "evaluation-id");
  nodes["train-adapter-select"].value = "";
  run("updateTrainingSource()");
  assert.equal(inputs.lora_r.disabled, false);
  await run('start("train")');
  assert.equal(calls.at(-1).adapter_run, null);
  console.log("Training frontend: source selection, inherited fields, base reset and operation payloads passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
