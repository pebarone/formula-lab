"use strict";

const $ = (id) => document.getElementById(id);
const form = $("training-form");
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const states = {queued: "Na fila", running: "Em execução", completed: "Concluído", failed: "Falhou", cancelled: "Interrompido", interrupted: "Processo encerrado"};
const kinds = {prepare: "Verificação", train: "Treinamento", evaluate: "Avaliação", decensor: "Recusa", tool_evaluate: "Teste de ferramentas"};
let sessionToken = "", jobs = [], selectedId = null, online = false, pending = false;
let report = null, reportId = "baseline", page = 0, lastDetail = null, initialized = false;
let adapterSignature = "", historySignature = "", requestedReport = 0;
let comparisonOverride = null, comparisonKey = null, comparisonMessage = "";
let connectionError = false;
const number = (value, digits = 0) => Number(value).toLocaleString("pt-BR", {maximumFractionDigits: digits});
const percentage = (value) => `${number(value * 100, 2)}%`;
const elapsed = (seconds) => {
  if (!Number.isFinite(seconds)) return "—";
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}min ${value % 60}s`;
  return `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}min`;
};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : {"Content-Type": "application/json", "X-Session-Token": sessionToken},
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(path.startsWith("/api/workbooks") ? 75000 : 20000),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || `Falha HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return result;
}

function config() {
  const result = {};
  for (const input of form.querySelectorAll("input[name]")) {
    result[input.name] = input.type === "checkbox" ? input.checked : Number(input.value);
  }
  return result;
}

function fillConfig(values) {
  for (const [name, value] of Object.entries(values)) {
    const input = form.elements.namedItem(name);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value;
  }
  updateEffective();
}

function updateEffective() {
  const value = config();
  $("effective-batch").textContent = number(value.batch_size * value.gradient_accumulation);
}

function buttons() {
  const active = jobs.some((job) => !terminal.has(job.status));
  document.querySelectorAll("[data-start]").forEach((button) => { button.disabled = !online || pending || active; });
  const running = lastDetail && !terminal.has(lastDetail.status);
  $("cancel").hidden = !running;
  $("cancel").disabled = pending || !online;
  $("force-stop").hidden = !running;
  $("force-stop").disabled = pending || !online;
}

async function start(kind) {
  if (kind !== "evaluate" && !form.reportValidity()) return;
  pending = true;
  buttons();
  notice("");
  try {
    const source = kind === "evaluate" ? $("adapter-select").value : $("train-adapter-select").value;
    const result = await api("/api/start", {kind, config: config(), adapter_run: source || null});
    selectedId = result.id;
    if (kind === "evaluate") comparisonOverride = null;
    lastDetail = null;
    try { localStorage.setItem("formula-lab-config", JSON.stringify(config())); } catch { /* Optional convenience only. */ }
    await refresh();
    $("monitor-title").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) { notice(error.message); }
  finally { pending = false; buttons(); }
}

async function startDecensor() {
  pending = true;
  buttons();
  notice("");
  try {
    const result = await api("/api/start", {kind: "decensor", config: {}});
    selectedId = result.id;
    lastDetail = null;
    await refresh();
    $("monitor-title").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) { notice(error.message); }
  finally { pending = false; buttons(); }
}

async function stop(force) {
  if (!selectedId) return;
  if (force && !window.confirm("Encerrar o processo agora? O passo atual e o adaptador ainda não salvo podem ser perdidos.")) return;
  pending = true;
  buttons();
  try {
    await api("/api/stop", {id: selectedId, force});
    notice(force ? "Processo encerrado. Os arquivos já gravados foram preservados." : "Interrupção solicitada. Aguarde o fim da operação atual; durante o treino, o adaptador parcial será salvo quando possível.");
    await refresh();
  } catch (error) { notice(error.message); }
  finally { pending = false; buttons(); }
}

function renderHistory() {
  const experiments = jobs.filter(job => job.kind !== "chat");
  const signature = JSON.stringify(experiments.map((job) => [job.id, job.status, job.result?.accuracy, job.tool_result])) + selectedId;
  if (signature === historySignature) return;
  historySignature = signature;
  $("history-count").textContent = `${experiments.length} ${experiments.length === 1 ? "execução" : "execuções"}`;
  const body = $("history-body");
  body.replaceChildren();
  if (!experiments.length) {
    const row = element("tr"), cell = element("td", "Não há execuções salvas.", "empty-cell");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
  }
  for (const job of experiments) {
    const row = element("tr", undefined, selectedId === job.id ? "selected" : "");
    const date = element("td", new Date(job.created_at * 1000).toLocaleString("pt-BR", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}));
    date.append(element("small", job.label || job.id.slice(-8)));
    const operation = element("td", kinds[job.kind] || job.kind);
    const settings = element("td", job.kind === "evaluate" ? (job.adapter_run ? `Adaptador ${job.adapter_run.slice(-8)}` : "Modelo original") : job.kind === "decensor" ? "Direção de recusa · LoRA rank 1" : job.kind === "tool_evaluate" ? `${number(job.tool_eval.test_count)} testes · com/sem ferramentas` : `${number(job.config.train_samples)} exemplos · ${job.config.epochs} época(s)`);
    if (job.kind === "train") settings.append(element("small", job.adapter_run ? `Continuação de ${job.adapter_run.slice(-8)}` : "A partir do base"));
    const status = element("td");
    status.append(element("span", states[job.status] || job.status, `status ${job.status}`));
    const measured = job.kind === "train" ? experiments.filter(item => item.status === "completed" && item.adapter_run === job.id && (item.result || item.tool_result)).sort((a, b) => (b.ended_at || b.created_at) - (a.ended_at || a.created_at))[0] : job;
    const paired = measured?.tool_result;
    const result = element("td", paired ? `${percentage(paired.without_tools.accuracy)} sem · ${percentage(paired.with_tools.accuracy)} com` : measured?.result ? percentage(measured.result.accuracy) : "—");
    const actions = element("td"), open = element("button", undefined, "text-button");
    open.innerHTML = 'Abrir <svg class="icon-inline" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 7h10v10" /><path d="M7 17 17 7" /></svg>';
    open.addEventListener("click", async () => {
      selectedId = job.id;
      if (job.result || job.tool_result || job.kind === "train") {
        $("adapter-select").value = job.kind === "train" ? job.id : job.adapter_run || "";
        comparisonOverride = job.kind === "train" ? null : job.id;
      }
      await refresh();
      $("monitor-title").scrollIntoView({behavior: "smooth", block: "start"});
    });
    actions.append(open);
    row.append(date, operation, settings, status, result, actions);
    body.append(row);
  }
  const adapters = experiments.filter((job) => job.kind === "train" && job.status === "completed" && job.has_adapter);
  const decensors = experiments.filter((job) => job.kind === "decensor" && job.status === "completed" && job.has_adapter);
  const nextSignature = JSON.stringify([...adapters, ...decensors].map((job) => [job.id, job.label]));
  if (nextSignature !== adapterSignature) {
    adapterSignature = nextSignature;
    const select = $("adapter-select"), previous = select.value;
    select.replaceChildren(new Option("Qwen original · sem adaptador", ""));
    for (const job of adapters) select.add(new Option(job.label || `Treino ${job.id.slice(-8)} · ${number(job.config.train_samples)} exemplos`, job.id));
    for (const job of decensors) select.add(new Option(`Recusa ${job.id.slice(-8)} · direção de recusa`, job.id));
    select.value = [...adapters, ...decensors].some((job) => job.id === previous) ? previous : "";
    const source = $("train-adapter-select"), previousSource = source.value;
    source.replaceChildren(new Option("Modelo base · novo adaptador", ""));
    for (const job of adapters) {
      const date = new Date(job.created_at * 1000).toLocaleString("pt-BR");
      source.add(new Option(`${job.label || `${job.id.slice(-8)} · ${number(job.config.train_samples)} exemplos`} · ${date}`, job.id));
    }
    source.value = adapters.some(job => job.id === previousSource) ? previousSource : "";
    updateTrainingSource();
  }
}

function updateTrainingSource() {
  const id = $("train-adapter-select").value;
  const source = jobs.find(job => job.id === id);
  for (const key of ["lora_r", "lora_alpha"]) {
    const input = form.elements.namedItem(key);
    input.disabled = Boolean(source);
    if (source) input.value = source.config[key];
  }
  $("train-adapter-hint").textContent = source
    ? `Continua ${source.id}. Rank e alpha são herdados. Salva uma nova versão com otimizador reiniciado. ${form.elements.namedItem("exclude_seen").checked ? "Exclui exemplos de treino de toda a cadeia de origem." : "Pode repetir exemplos já usados."}`
    : "Começa do Qwen original e salva um adaptador novo.";
}

function selectionDescription(name, values, toolMode = false) {
  const label = name === "train" ? "Treino" : "Validação";
  if (toolMode) {
    const composition = name === "train"
      ? `${number(values.tool_usage.selected)} exemplos novos de ferramentas + ${number(values.rehearsal.selected)} de revisão do treino anterior`
      : `${number(values.selected)} exemplos separados de ferramentas`;
    return [`${label}: ${composition}. Até ${number(values.max_tokens)} tokens por exemplo.`];
  }
  return [
    `${label}: ${number(values.selected)} exemplos · ${number(values.tables)} tabelas · ${number(values.max_tokens)} tokens no maior exemplo. ${number(values.suspect_excluded)} suspeitos excluídos do split; ${number(values.seen_excluded || 0)} já usados excluídos; ${number(values.too_long_skipped)} longos ignorados na seleção.`,
    `Dificuldade: ${number(values.levels.easy || 0)} fáceis · ${number(values.levels.medium || 0)} médios · ${number(values.levels.hard || 0)} difíceis.`,
  ];
}

function svgNode(tag, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text !== undefined) node.textContent = text;
  return node;
}

function drawChart(events) {
  const chart = $("loss-chart");
  const training = events.filter((e) => Number.isFinite(e.loss) && Number.isFinite(e.step));
  const validation = events.filter((e) => Number.isFinite(e.eval_loss) && Number.isFinite(e.step));
  chart.replaceChildren();
  $("chart-empty").hidden = training.length + validation.length > 0;
  if (!training.length && !validation.length) return;
  const values = [...training.map((e) => e.loss), ...validation.map((e) => e.eval_loss)];
  let min = Math.min(...values), max = Math.max(...values);
  const pad = Math.max((max - min) * .15, .05);
  min = Math.max(0, min - pad); max += pad;
  const maxStep = Math.max(1, ...training.map((e) => e.step), ...validation.map((e) => e.step));
  const x = (value) => 50 + value / maxStep * 608;
  const y = (value) => 185 - (value - min) / (max - min) * 165;
  for (let i = 0; i < 5; i++) {
    const value = min + (max - min) * i / 4;
    chart.append(svgNode("line", {x1: 50, x2: 658, y1: y(value), y2: y(value), class: "axis"}));
    chart.append(svgNode("text", {x: 40, y: y(value) + 4, "text-anchor": "end"}, number(value, 2)));
  }
  for (let i = 0; i <= 4; i++) {
    const step = Math.round(maxStep * i / 4);
    chart.append(svgNode("text", {x: x(step), y: 209, "text-anchor": "middle"}, number(step)));
  }
  const addSeries = (rows, key, name) => {
    if (!rows.length) return;
    chart.append(svgNode("polyline", {points: rows.map((e) => `${x(e.step)},${y(e[key])}`).join(" "), class: `${name}-line`}));
    // Points also make single-step smoke tests visible.
    for (const row of rows) {
      const dot = svgNode("circle", {cx: x(row.step), cy: y(row[key]), r: rows.length < 30 ? 3 : 1.5, class: `${name}-dot`});
      dot.append(svgNode("title", {}, `Passo ${row.step}: ${number(row[key], 4)}`));
      chart.append(dot);
    }
  };
  addSeries(training, "loss", "train");
  addSeries(validation, "eval_loss", "eval");
  chart.setAttribute("aria-label", `Loss de treino e validação ao longo de ${maxStep} passos. Última loss de treino: ${training.at(-1)?.loss ?? "ausente"}; validação: ${validation.at(-1)?.eval_loss ?? "ausente"}.`);
}

function renderDetail(detail) {
  lastDetail = detail;
  const events = detail.events || [];
  const metric = (key) => events.findLast((e) => e[key] !== undefined)?.[key];
  $("run-status").textContent = states[detail.status] || detail.status;
  $("run-status").className = `status ${detail.status}`;
  const phase = events.findLast((event) => event.phase) || {};
  const finished = terminal.has(detail.status);
  const phaseName = phase.phase || "Preparando ambiente";
  $("run-phase").textContent = detail.error || (finished && detail.status !== "completed" ? states[detail.status] : phaseName);
  $("run-id").textContent = `${detail.id} · ${detail.label || kinds[detail.kind]}`;
  if (detail.kind === "train" && detail.adapter_run) $("run-id").textContent += ` · Continuação de ${detail.adapter_run}`;
  const step = phase.phase_step ?? phase.step ?? 0, total = phase.phase_total ?? phase.total_steps ?? 0;
  const unit = phase.phase_unit || (phaseName === "Treinando" ? "passos do otimizador" : "itens");
  if (total || finished) $("progress").value = total ? Math.min(100, step / total * 100) : (detail.status === "completed" ? 100 : 0);
  else $("progress").removeAttribute("value");
  $("step-label").textContent = total ? `${number(step)} / ${number(total)} ${unit} · ${number(step / total * 100, 1)}% desta etapa` : "Etapa sem contagem de itens";
  const eta = phase.phase_eta_seconds ?? phase.eta_seconds;
  $("eta").textContent = finished ? states[detail.status] : (total && step >= total ? "Finalizando etapa" : Number.isFinite(eta) ? `Restam ~${elapsed(eta)} nesta etapa` : "Estimando duração da etapa");
  const now = detail.ended_at || Date.now() / 1000;
  const phaseTime = Math.max(0, now - (phase.phase_started_at || phase.time || detail.started_at || detail.created_at));
  const sinceUpdate = Math.max(0, now - (events.at(-1)?.time || detail.created_at));
  const speed = phase.phase_rate ? ` · ${number(phase.phase_rate, 1)} ${unit}/s` : "";
  $("phase-detail").textContent = `${elapsed(phaseTime)} nesta etapa${speed} · última atualização há ${elapsed(sinceUpdate)}. ` +
    (phaseName === "Validando" ? "Calculando loss nos exemplos separados, sem atualizar o modelo." : phaseName === "Treinando" ? `Passos do otimizador: ${metric("step") || 0}/${metric("total_steps") || "—"}. A validação e o salvamento vêm depois.` : "O percentual se refere apenas à etapa atual.");
  if (!finished && phaseName === "Treinando" && step >= total && total && !phase.phase_unit) {
    $("run-phase").textContent = "Treino finalizado · aguardando validação ou salvamento";
    $("phase-detail").textContent = "Esta execução antiga não registra o progresso da validação. A contagem detalhada estará disponível nos próximos treinos.";
    $("progress").removeAttribute("value");
    $("eta").textContent = "Duração restante indisponível";
  }
  let stageLabels, stage;
  if (detail.kind === "decensor") {
    stageLabels = ["Carregar", "Medir", "Salvar"];
    stage = /Medindo/.test(phaseName) ? 1 : /Salvando|Recusa concluída/.test(phaseName) ? 2 : 0;
  } else if (detail.kind === "tool_evaluate") {
    stageLabels = ["Preparar", "Carregar", "Comparar", "Salvar"];
    stage = /Comparando/.test(phaseName) ? 2 : /concluída|Salvando/.test(phaseName) ? 3 : /Carregando/.test(phaseName) ? 1 : 0;
  } else {
    stageLabels = detail.kind === "train" ? ["Preparar", "Carregar", "Treinar", "Validar", "Salvar"] : detail.kind === "evaluate" ? ["Preparar", "Carregar", "Avaliar", "Salvar"] : ["Preparar"];
    stage = 0;
    if (/Carregando Qwen|Carregando adaptador|Preparando adaptador|Preparando otimizador/.test(phaseName)) stage = 1;
    else if (/Treinando|Avaliando teste/.test(phaseName)) stage = 2;
    else if (/Validando|Validação concluída/.test(phaseName)) stage = 3;
    else if (/Salvando|melhor checkpoint|Treinamento concluído|Avaliação concluída/.test(phaseName)) stage = stageLabels.length - 1;
  }
  const track = $("phase-track");
  track.hidden = false;
  track.replaceChildren();
  stageLabels.forEach((label, index) => {
    const item = element("li", `${index + 1}. ${label}`, index === stage ? "current" : "");
    if (index === stage) item.setAttribute("aria-current", "step");
    track.append(item);
  });
  $("train-loss").textContent = Number.isFinite(metric("loss")) ? number(metric("loss"), 4) : "—";
  $("eval-loss").textContent = Number.isFinite(metric("eval_loss")) ? number(metric("eval_loss"), 4) : "—";
  $("elapsed").textContent = elapsed((detail.ended_at || Date.now() / 1000) - (detail.started_at || detail.created_at));
  $("vram").textContent = Number.isFinite(metric("vram_gb")) ? `${number(metric("vram_gb"), 1)} GB` : "—";
  if (Number.isFinite(metric("vram_peak_gb"))) $("vram").textContent += ` · pico ${number(metric("vram_peak_gb"), 1)} GB`;
  $("vram").title = (terminal.has(detail.status) ? "Última medição da execução; a GPU já pode ter sido liberada. " : "Memória alocada pelo PyTorch nesta execução. ") +
    (Number.isFinite(metric("vram_reserved_gb")) ? `Reservada: ${number(metric("vram_reserved_gb"), 2)} GB. ` : "") +
    (Number.isFinite(metric("vram_peak_reserved_gb")) ? `Pico reservado: ${number(metric("vram_peak_reserved_gb"), 2)} GB. ` : "") +
    (Number.isFinite(metric("gpu_budget_gb")) ? `Limite do alocador: ${number(metric("gpu_budget_gb"), 2)} GB.` : "");
  $("epoch").textContent = Number.isFinite(metric("epoch")) ? number(metric("epoch"), 2) : "—";
  $("learning-rate").textContent = Number.isFinite(metric("learning_rate")) ? metric("learning_rate").toExponential(2) : "—";
  $("grad-norm").textContent = Number.isFinite(metric("grad_norm")) ? number(metric("grad_norm"), 3) : "—";
  drawChart(events);
  const selection = detail.selection?.splits;
  $("selection").hidden = !selection;
  if (selection) {
    $("selection").replaceChildren();
    for (const [name, values] of Object.entries(selection)) {
      for (const text of selectionDescription(name, values, detail.training_mode === "tool_usage")) $("selection").append(element("div", text));
    }
  }
  const log = $("log"), follow = $("follow-log").checked;
  if (log.textContent !== detail.log) {
    log.textContent = detail.log || "Processo iniciado. Aguardando mensagens do Python…";
    if (follow) log.scrollTop = log.scrollHeight;
  }
  $("artifacts").replaceChildren();
  const labels = {"request.json": "Parâmetros", "selection.json": "Seleção", "report.json": "Resultado JSON", "tool_report.json": "Comparação com/sem ferramentas", "tool_predictions.jsonl": "Respostas e chamadas ao Excel", "tool_validation_report.json": "Verificação do uso de ferramentas", "tool_validation_predictions.jsonl": "Respostas da verificação de ferramentas", "training_summary.json": "Resumo do treino", "decensor_report.json": "Relatório da recusa", "log.txt": "Log completo", "events.jsonl": "Métricas", "environment.json": "Ambiente"};
  for (const name of detail.artifacts || []) {
    const link = element("a", labels[name]);
    link.href = `/api/download?id=${detail.id}&file=${encodeURIComponent(name)}`;
    link.download = name;
    $("artifacts").append(link);
  }
  buttons();
}

function comparisonReportId() {
  const adapter = $("adapter-select").value;
  if (comparisonOverride === "baseline" && !adapter) return "baseline";
  const evaluations = jobs.filter(job => ["evaluate", "tool_evaluate"].includes(job.kind) && job.status === "completed" && (job.result || job.tool_result) && (job.adapter_run || "") === adapter);
  if (evaluations.some(job => job.id === comparisonOverride)) return comparisonOverride;
  evaluations.sort((a, b) => (b.ended_at || b.created_at) - (a.ended_at || a.created_at) || b.id.localeCompare(a.id));
  return evaluations[0]?.id || (adapter ? null : "baseline");
}

function clearComparison(message) {
  report = null; reportId = null; page = 0; comparisonMessage = message;
  $("comparison-result").hidden = true;
  $("comparison-result").replaceChildren();
  if ($("paired-comparison")) { $("paired-comparison").hidden = true; $("paired-comparison-content").replaceChildren(); }
  $("responses-title").textContent = message;
  renderResponses();
}

async function syncComparison() {
  const id = comparisonReportId();
  const key = id || `unavailable:${$("adapter-select").value}`;
  if (key === comparisonKey) return;
  comparisonKey = key;
  if (!id) {
    ++requestedReport;
    clearComparison("Este modelo ainda não tem uma avaliação concluída. Clique em Avaliar nos 500 testes.");
    return;
  }
  try { await loadReport(id); }
  catch (error) {
    if (comparisonKey !== key) return;
    comparisonKey = null;
    clearComparison(`Não foi possível carregar a avaliação: ${error.message}`);
  }
}

async function loadReport(id) {
  const sequence = ++requestedReport;
  clearComparison("Carregando avaliação do modelo selecionado…");
  let result;
  const paired = jobs.some(job => job.id === id && job.kind === "tool_evaluate");
  try { result = await api(`/api/${paired ? "tool-report" : "report"}?id=${encodeURIComponent(id)}`); }
  catch (error) {
    if (sequence !== requestedReport) return;
    throw error;
  }
  if (sequence !== requestedReport) return;
  if (!Array.isArray(result.items)) throw new Error("Resultado indisponível.");
  if (paired) {
    if (!result.without_tools || !result.with_tools) throw new Error("Relatório pareado incompleto.");
    workbookRenderEvaluation(result, "paired-comparison-content", "paired-comparison");
    // Reuse the searchable, paged answer list for the condition with tools.
    result = {...result, ...result.with_tools, comparison: null, paired: true, items: result.items.map(item => ({
      ...item, ...item.with_tools, baseline_predicted: item.without_tools?.predicted, baseline_match: item.without_tools?.match,
    }))};
  }
  report = result; reportId = id; page = 0;
  comparisonMessage = "";
  const modelLabel = result.adapter_run ? `Adaptador ${result.adapter_run.slice(-8)}` : "Modelo base";
  $("responses-title").textContent = paired ? `${modelLabel} · Respostas com ferramentas · ${number(result.total)} testes` : id === "baseline" ? "Modelo base · Respostas do baseline" : `${modelLabel} · Respostas da avaliação ${id.slice(-8)}`;
  const comparison = $("comparison-result");
  comparison.hidden = !result.items;
  comparison.replaceChildren();
  if (result.items) {
    const values = paired ? [] : [["Correspondência normalizada", percentage(result.accuracy)], ["Fórmulas correspondentes", `${result.correct} / ${result.total}`]];
    if (result.comparison) values.push(["Variação vs. baseline", `${result.comparison.delta_pp >= 0 ? "+" : ""}${number(result.comparison.delta_pp, 2)} p.p.`], ["Novos acertos / regressões", `${result.comparison.gained} / ${result.comparison.lost}`]);
    for (const [label, value] of values) { const group = element("div"); group.append(element("span", label), element("strong", value)); comparison.append(group); }
  }
  if (paired) comparison.hidden = true;
  renderResponses();
}

function renderResponses() {
  const search = $("response-search").value.trim().toLocaleLowerCase(), filter = $("response-filter").value;
  const rows = (report?.items || []).filter((item) => {
    if (filter === "wrong" && item.match || filter === "correct" && !item.match) return false;
    if (filter === "gained" && !(item.baseline_match === false && item.match)) return false;
    if (filter === "lost" && !(item.baseline_match === true && !item.match)) return false;
    return !search || [item.id, item.instruction, item.expected, item.predicted].some((text) => String(text || "").toLocaleLowerCase().includes(search));
  });
  const pages = Math.max(1, Math.ceil(rows.length / 10));
  page = Math.min(page, pages - 1);
  const container = $("responses");
  container.replaceChildren();
  if (!rows.length) container.append(element("p", comparisonMessage || "Nenhuma resposta para este filtro.", "empty-cell"));
  for (const item of rows.slice(page * 10, page * 10 + 10)) {
    const details = element("details", undefined, "response-item"), summary = element("summary");
    summary.append(element("span", item.id, "response-label"), element("span", item.instruction || item.predicted, "response-preview"), element("span", item.match ? "Acerto" : "Divergência", `status ${item.match ? "completed" : ""}`));
    details.append(summary);
    if (item.instruction) details.append(element("p", item.instruction, "response-question"));
    const grid = element("div", undefined, "formula-grid");
    for (const [name, value] of [["Gabarito", item.expected], ["Resposta do modelo", item.predicted], ...(item.baseline_predicted ? [[report?.paired ? "Resposta sem ferramentas" : "Resposta original (baseline)", item.baseline_predicted]] : [])]) {
      const cell = element("div"); cell.append(element("span", name), element("code", value)); grid.append(cell);
    }
    details.append(grid); container.append(details);
  }
  $("page-label").textContent = `${number(rows.length)} respostas · página ${page + 1} de ${pages}`;
  $("prev-page").disabled = page === 0;
  $("next-page").disabled = page + 1 >= pages;
}

async function refresh() {
  try {
    const state = await api("/api/state");
    online = true;
    if (connectionError) { notice(""); connectionError = false; }
    sessionToken = state.token; jobs = state.jobs;
    $("connection").textContent = "Servidor conectado";
    $("connection").className = "connection online";
    $("baseline-accuracy").textContent = Number.isFinite(state.baseline.accuracy) ? percentage(state.baseline.accuracy) : "Sem baseline";
    $("baseline-count").textContent = state.baseline.total ? `${state.baseline.correct} / ${state.baseline.total}` : "—";
    $("environment").textContent = `torch ${state.versions.torch || "ausente"} · PEFT ${state.versions.peft || "ausente"}`;
    if (!initialized) {
      let values = state.defaults;
      try { values = {...values, ...JSON.parse(localStorage.getItem("formula-lab-config") || "{}")}; } catch { /* Use server defaults if local storage is unavailable or damaged. */ }
      fillConfig(values);
      selectedId = jobs.find((job) => job.kind !== "chat" && !terminal.has(job.status))?.id || jobs.find(job => job.kind !== "chat")?.id || null;
      initialized = true;
    }
    renderHistory();
    await syncComparison();
    if (selectedId) {
      const id = selectedId;
      const detail = await api(`/api/run?id=${id}`);
      if (id === selectedId) renderDetail(detail);

    }
  } catch (error) {
    online = false;
    connectionError = true;
    $("connection").textContent = "Sem conexão · tentando novamente";
    $("connection").className = "connection offline";
    notice(`Não foi possível atualizar o painel: ${error.message}. Verifique se a janela do servidor está aberta. As execuções já iniciadas podem continuar em segundo plano.`);
  } finally { buttons(); if (typeof updateChatState === "function") updateChatState(); }
}

form.addEventListener("submit", (event) => { event.preventDefault(); start("train"); });
form.addEventListener("input", updateEffective);
form.elements.namedItem("exclude_seen").addEventListener("change", updateTrainingSource);
$("train-adapter-select").addEventListener("change", updateTrainingSource);
$("prepare").addEventListener("click", () => start("prepare"));
$("decensor").addEventListener("click", () => startDecensor());
$("evaluate").addEventListener("click", () => start("evaluate"));
$("adapter-select").addEventListener("change", () => {
  comparisonOverride = null;
  syncComparison();
});
$("cancel").addEventListener("click", () => stop(false));
$("force-stop").addEventListener("click", () => stop(true));
$("view-baseline").addEventListener("click", async () => {
  $("adapter-select").value = "";
  comparisonOverride = "baseline";
  await syncComparison();
  $("comparison").scrollIntoView({behavior: "smooth"});
});
$("response-filter").addEventListener("change", () => { page = 0; renderResponses(); });
$("response-search").addEventListener("input", () => { page = 0; renderResponses(); });
$("prev-page").addEventListener("click", () => { page--; renderResponses(); });
$("next-page").addEventListener("click", () => { page++; renderResponses(); });
async function poll() { await refresh(); setTimeout(poll, 2500); }
buttons();
poll();
