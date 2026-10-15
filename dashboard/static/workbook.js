"use strict";

const WORKBOOK_COLUMNS = "ABCDEFGHIJKL".split("");
const WORKBOOK_TRACE_LABELS = {
  excel_read_range: "Ler intervalo da planilha",
  excel_test_formula: "Testar fórmula no Excel",
  excel_write_cells: "Escrever valores ou fórmulas",
  read_values: "Ler valores da planilha",
  read_cell: "Ler uma célula",
  read_range: "Ler um intervalo",
  test_formula: "Testar fórmula no Excel",
  write_formula: "Inserir fórmula na planilha",
  set_value: "Definir valor na planilha",
};
let workbookExcelAvailable = false;
let workbookSnapshot = null;
let workbookGrid = null;
let workbookSessionId = null;
let workbookLoadVersion = 0;
let workbookApplyPending = false;
let workbookOpeningPending = false;
let workbookCellTestPending = false;
let workbookEvalPending = false;
let workbookEvalSessionId = null;
let workbookPendingChanges = new Map();
let workbookChangeSequence = 0;
let workbookSelected = {sheet: "", cell: ""};
let workbookEvalTimer = null;
let workbookSeenRevision = null;

function workbookCurrentSession() {
  return typeof currentChat === "function" ? currentChat() : null;
}

function workbookIsChatBusy() {
  return typeof chatBusy === "function" && chatBusy();
}

function workbookHasOtherRun() {
  if (!Array.isArray(jobs) || typeof terminal === "undefined") return false;
  return jobs.some(job => !terminal.has(job.status) && job.id !== workbookCurrentSession()?.run);
}

function workbookShowNotice(message, isError = false) {
  const notice = $("workbook-notice");
  if (!notice) return;
  notice.textContent = message || "";
  notice.classList.toggle("error", Boolean(isError));
  notice.hidden = !message;
}

function workbookSetEngineStatus(message, state = "") {
  const node = $("workbook-engine-status");
  if (!node) return;
  node.textContent = message;
  node.classList.toggle("ready", state === "ready");
  node.classList.toggle("unavailable", state === "unavailable");
}

function workbookCellKey(sheet, address) { return `${sheet}\u0000${address}`; }

function workbookSheetNames() {
  return Array.isArray(workbookSnapshot?.sheets) ? workbookSnapshot.sheets.map(sheet => sheet.name).filter(name => typeof name === "string") : [];
}

function workbookSelectedSheetData() {
  return workbookSnapshot?.sheets?.find(sheet => sheet.name === workbookSelected.sheet) || null;
}

function workbookUpdatePreviewNote() {
  const note = $("workbook-grid-scope");
  if (!note) return;
  const sheet = workbookSelectedSheetData();
  const partial = Number(sheet?.rows) > 50 || Number(sheet?.columns) > 12;
  note.textContent = partial
    ? "Prévia parcial: mostrando até 50 linhas e 12 colunas. Baixe a cópia para conferir o restante; a planilha original não é sobrescrita."
    : "Grade limitada a 50 linhas e 12 colunas. A planilha original não é sobrescrita.";
}

function workbookFindCell(sheet, address) {
  return workbookSnapshot?.sheets?.find(item => item.name === sheet)?.cells?.find(cell => cell.address === address) || null;
}

function workbookPendingFor(sheet, address) { return workbookPendingChanges.get(workbookCellKey(sheet, address)) || null; }

function workbookAddress(row, column) { return `${column}${row}`; }

function workbookVisibleValue(cell) {
  if (!cell) return "";
  if (cell.value !== undefined && cell.value !== null) return cell.value;
  return cell.formula || cell.formula_local || "";
}

function workbookSetSelectedCell(sheet, address) {
  workbookSelected = {sheet, cell: address};
  const name = $("workbook-cell-name");
  const entry = $("workbook-cell-entry");
  if (name) name.textContent = address || "—";
  const pending = address ? workbookPendingFor(sheet, address) : null;
  const cell = address ? workbookFindCell(sheet, address) : null;
  if (entry) entry.value = pending ? (pending.formula ?? pending.value ?? "") : (cell?.formula || cell?.formula_local || workbookVisibleValue(cell));
  workbookUpdateControls();
}

function workbookGridRows() {
  const sheet = workbookSelectedSheetData();
  const cells = new Map((sheet?.cells || []).filter(cell => typeof cell.address === "string").map(cell => [cell.address.toUpperCase(), cell]));
  return Array.from({length: 50}, (_, index) => {
    const rowNumber = index + 1;
    const row = {__row: rowNumber};
    for (const column of WORKBOOK_COLUMNS) {
      const address = workbookAddress(rowNumber, column);
      const pending = workbookPendingFor(workbookSelected.sheet, address);
      const cell = cells.get(address);
      row[column] = pending ? (pending.formula ?? pending.value ?? "") : workbookVisibleValue(cell);
    }
    return row;
  });
}

function workbookBuildColumns() {
  const columns = [{title: "", field: "__row", width: 42, minWidth: 42, maxWidth: 42, frozen: true, headerSort: false, hozAlign: "right", editable: false}];
  for (const field of WORKBOOK_COLUMNS) {
    columns.push({
      title: field,
      field,
      minWidth: 92,
      width: 112,
      headerSort: false,
      editor: "input",
      editable: () => workbookCanWrite(),
      formatter(cell) {
        const node = document.createElement("span");
        const row = cell.getRow().getData().__row;
        const address = workbookAddress(row, field);
        const source = workbookFindCell(workbookSelected.sheet, address);
        node.textContent = String(cell.getValue() ?? "");
        if (source?.error || (typeof source?.value === "string" && /^#(?:REF|VALUE|NAME|DIV\/0|N\/A|NUM|NULL|SPILL|CALC|FIELD|BLOCKED|UNKNOWN)!?$/i.test(source.value))) node.className = "workbook-error-cell";
        return node;
      },
    });
  }
  return columns;
}

function workbookOnGridEdit(cell) {
  const row = cell.getRow().getData().__row;
  const column = cell.getField();
  if (!WORKBOOK_COLUMNS.includes(column)) return;
  const address = workbookAddress(row, column);
  workbookQueueChange({sheet: workbookSelected.sheet, cell: address, value: cell.getValue()});
  workbookSetSelectedCell(workbookSelected.sheet, address);
  workbookShowNotice("Edição pendente. A grade mantém seu conteúdo até as alterações serem aplicadas.");
}

function workbookRenderGrid() {
  const host = $("workbook-grid"), empty = $("workbook-grid-empty");
  const hasSheet = Boolean(workbookSelectedSheetData());
  if (empty) empty.hidden = hasSheet;
  if (!host) return;
  if (!hasSheet) {
    if (workbookGrid) workbookGrid.setData([]);
    return;
  }
  const data = workbookGridRows();
  if (!workbookGrid && typeof Tabulator === "function") {
    workbookGrid = new Tabulator(host, {
      data,
      index: "__row",
      height: "100%",
      layout: "fitColumns",
      responsiveLayout: false,
      movableColumns: false,
      selectableRows: 1,
      columnDefaults: {headerHozAlign: "center", resizable: true},
      columns: workbookBuildColumns(),
    });
    workbookGrid.on("cellEdited", workbookOnGridEdit);
    workbookGrid.on("cellClick", (event, cell) => {
      const field = cell.getField();
      if (WORKBOOK_COLUMNS.includes(field)) workbookSetSelectedCell(workbookSelected.sheet, workbookAddress(cell.getRow().getData().__row, field));
    });
  } else if (workbookGrid) workbookGrid.setData(data);
  else workbookShowNotice("A grade local não carregou. Recarregue o painel para tentar novamente.", true);
  if (!workbookSelected.cell) workbookSetSelectedCell(workbookSelected.sheet, "A1");
}

function workbookRenderSheets() {
  const select = $("workbook-sheet-select");
  if (!select) return;
  select.replaceChildren();
  const names = workbookSheetNames();
  if (!names.includes(workbookSelected.sheet)) workbookSelected.sheet = names[0] || "";
  names.forEach(name => {
    const option = element("option", name);
    option.value = name;
    select.append(option);
  });
  select.value = workbookSelected.sheet;
  workbookUpdatePreviewNote();
  workbookRenderGrid();
}

function workbookRenderAttachmentOptions() {
  const select = $("workbook-attachment-select");
  if (!select) return;
  const selected = select.value;
  const session = workbookCurrentSession();
  const allFiles = [...(session?.attachments || []), ...(session?.messages || []).flatMap(message => message.attachments || [])];
  const seen = new Set();
  const files = allFiles.filter(file => /\.(xlsx|csv|tsv)$/i.test(file.name || "") && typeof file.id === "string" && !seen.has(file.id) && seen.add(file.id));
  select.replaceChildren();
  const placeholder = element("option", files.length ? "Escolha uma planilha anexada ao chat" : "Anexe XLSX, CSV ou TSV pelo chat para importar");
  placeholder.value = "";
  select.append(placeholder);
  files.forEach(file => {
    const option = element("option", file.name);
    option.value = file.id;
    select.append(option);
  });
  if (files.some(file => file.id === selected)) select.value = selected;
  else if (!workbookSnapshot && files.length) select.value = files[0].id;
  workbookUpdateControls();
}

function workbookAcceptSnapshot(snapshot, sessionId = workbookCurrentSession()?.id) {
  const session = workbookCurrentSession();
  if (!session || session.id !== sessionId || !snapshot || typeof snapshot.id !== "string" || !Array.isArray(snapshot.sheets)) return false;
  workbookSnapshot = snapshot;
  session.workbookId = snapshot.id;
  if (!workbookSheetNames().includes(workbookSelected.sheet)) workbookSelected.sheet = workbookSheetNames()[0] || "";
  if (!workbookSelected.cell) workbookSelected.cell = "A1";
  if (typeof saveActiveSession === "function") saveActiveSession();
  workbookRenderSheets();
  workbookSetSelectedCell(workbookSelected.sheet, workbookSelected.cell);
  workbookSetEngineStatus(`Excel conectado · ${snapshot.name || "planilha ativa"}`, "ready");
  workbookShowNotice(snapshot.warnings?.filter?.(warning => typeof warning === "string").join(" ") || "Planilha pronta para editar.");
  workbookUpdateControls();
  return true;
}

async function workbookLoadSnapshot(id, options = {}) {
  if (!id) return null;
  const session = workbookCurrentSession();
  if (!session) return null;
  const sessionId = session.id;
  const version = ++workbookLoadVersion;
  try {
    const snapshot = await api(`/api/workbooks?id=${encodeURIComponent(id)}`);
    if (version !== workbookLoadVersion || sessionId !== workbookCurrentSession()?.id) return null;
    workbookAcceptSnapshot(snapshot, sessionId);
    if (!options.quiet) workbookShowNotice("Planilha atualizada com os dados do Excel.");
    return snapshot;
  } catch (error) {
    if (version !== workbookLoadVersion || sessionId !== workbookCurrentSession()?.id) return null;
    workbookShowNotice(`Não foi possível abrir a planilha: ${error.message}. As alterações pendentes foram preservadas.`, true);
    workbookSetEngineStatus("Planilha não carregada", "unavailable");
    workbookUpdateControls();
    return null;
  }
}

function workbookSessionChanged() {
  const session = workbookCurrentSession();
  if (!session) return;
  workbookRenderAttachmentOptions();
  if (workbookSessionId === session.id && workbookSnapshot?.id === session.workbookId) {
    workbookUpdateControls();
    return;
  }
  workbookSessionId = session.id;
  workbookLoadVersion++;
  workbookPendingChanges = new Map();
  workbookSnapshot = null;
  workbookSelected = {sheet: "", cell: ""};
  if (workbookEvalSessionId !== session.id) workbookEvalPending = false;
  workbookEvalSessionId = session.toolEvalRunId ? session.id : null;
  workbookRenderSheets();
  workbookSetSelectedCell("", "");
  if (session.workbookId) void workbookLoadSnapshot(session.workbookId, {quiet: true});
  else {
    workbookShowNotice("");
    workbookUpdateControls();
  }
  if (session.toolEvalRunId) {
    workbookEvalPending = true;
    workbookEvalSessionId = session.id;
    void workbookPollEvaluation(session.toolEvalRunId);
  }
}

async function workbookRefreshAvailability() {
  try {
    const result = await api("/api/excel");
    workbookExcelAvailable = result?.available === true;
    workbookSetEngineStatus(workbookExcelAvailable ? `${result.engine || "Microsoft Excel"} disponível` : (result.message || "Microsoft Excel indisponível"), workbookExcelAvailable ? "ready" : "unavailable");
    if (!workbookExcelAvailable) workbookShowNotice(result?.message || "Instale ou inicie a versão oficial do Microsoft Excel para editar e calcular planilhas.", true);
  } catch (error) {
    workbookExcelAvailable = false;
    workbookSetEngineStatus("Excel indisponível", "unavailable");
    workbookShowNotice(`Não foi possível verificar o Microsoft Excel: ${error.message}`, true);
  }
  workbookUpdateControls();
}

function workbookCanWrite() {
  return Boolean(workbookExcelAvailable && workbookSnapshot && !workbookIsChatBusy() && !workbookOpeningPending);
}

function workbookNetworkPending() {
  return Boolean(workbookOpeningPending || workbookApplyPending || workbookCellTestPending || workbookEvalPending);
}

function workbookNetworkStatus() {
  if (workbookApplyPending) return "Aplicando alterações no Excel…";
  if (workbookCellTestPending) return "Testando fórmula no Excel…";
  if (workbookOpeningPending) return "Preparando a planilha…";
  if (workbookEvalPending) return "Comparando respostas com e sem ferramentas…";
  return "";
}

function workbookRefreshChatControls() {
  if (typeof updateChatState === "function") updateChatState();
  else workbookUpdateControls();
}

function workbookUpdateControls() {
  const session = workbookCurrentSession();
  const hasBook = Boolean(session?.workbookId && workbookSnapshot?.id === session.workbookId);
  const locked = workbookIsChatBusy();
  const selected = Boolean(workbookSelected.sheet && workbookSelected.cell);
  if ($("workbook-chat-send")) $("workbook-chat-send").disabled = !hasBook || locked || !workbookExcelAvailable;
  if ($("workbook-chat-input")) $("workbook-chat-input").disabled = !hasBook || locked || !workbookExcelAvailable;
  if ($("workbook-chat-status")) $("workbook-chat-status").textContent = locked ? ($("chat-generation-status")?.textContent || "IA trabalhando na planilha…") : workbookPendingChanges.size ? "Aplique suas alterações antes de enviar à IA." : "A IA pode ler, testar e editar a cópia de trabalho.";
  const lastResponse = session?.messages?.at(-1);
  if ($("workbook-chat-response")) {
    $("workbook-chat-response").hidden = lastResponse?.role !== "assistant";
    $("workbook-chat-response").textContent = lastResponse?.role === "assistant" ? lastResponse.content : "";
  }
  const importSelect = $("workbook-attachment-select");
  const importButton = $("workbook-import-btn");
  const createButton = $("workbook-create-btn");
  const sheetSelect = $("workbook-sheet-select");
  if (importSelect) importSelect.disabled = !workbookExcelAvailable || locked || workbookApplyPending || workbookOpeningPending;
  if (importButton) importButton.disabled = !workbookExcelAvailable || locked || workbookApplyPending || workbookOpeningPending || !importSelect?.value;
  if (createButton) createButton.disabled = !workbookExcelAvailable || locked || workbookApplyPending || workbookOpeningPending;
  if (sheetSelect) sheetSelect.disabled = !hasBook || locked || workbookOpeningPending;
  const download = $("workbook-download-btn");
  if (download) download.disabled = !hasBook || locked || workbookOpeningPending;
  const entry = $("workbook-cell-entry");
  if (entry) entry.disabled = !hasBook || !selected || locked || workbookOpeningPending;
  for (const id of ["workbook-set-value-btn", "workbook-set-formula-btn", "workbook-test-formula-btn"]) {
    const button = $(id);
    const unsavedFormulaContext = id === "workbook-test-formula-btn" && workbookPendingChanges.size > 0;
    if (button) button.disabled = !hasBook || !selected || locked || workbookApplyPending || workbookOpeningPending || workbookCellTestPending || unsavedFormulaContext;
  }
  const apply = $("workbook-apply-btn");
  if (apply) apply.disabled = !hasBook || workbookPendingChanges.size === 0 || workbookApplyPending || workbookOpeningPending || locked;
  const toolToggle = $("chat-use-excel-tools");
  if (toolToggle) {
    toolToggle.checked = Boolean(session?.useExcelTools && session?.workbookId);
    toolToggle.disabled = !hasBook || !workbookExcelAvailable || locked || workbookOpeningPending;
  }
  const compare = $("workbook-compare-btn");
  if (compare) compare.disabled = !workbookExcelAvailable || !hasBook || locked || (workbookEvalPending && workbookEvalSessionId === session?.id) || workbookHasOtherRun();
  const countSelect = $("workbook-test-count");
  if (countSelect) countSelect.disabled = (workbookEvalPending && workbookEvalSessionId === session?.id) || locked || workbookHasOtherRun();
}

function workbookSetBenchExpanded(expanded) {
  const bench = $("workbook-bench"), body = $("workbook-bench-body"), button = $("workbook-collapse-btn");
  if (!bench || !body || !button) return;
  bench.classList.toggle("collapsed", !expanded);
  body.hidden = !expanded;
  button.setAttribute("aria-expanded", String(expanded));
  button.textContent = expanded ? "Recolher" : "Abrir bancada";
  if (expanded && workbookGrid) setTimeout(() => workbookGrid.redraw(true), 0);
}

function workbookOpenWindow() {
  const panel = $("workbook-window"), bench = $("workbook-bench");
  if (!panel || !bench || panel.open) return;
  workbookSetBenchExpanded(true);
  panel.append(bench);
  panel.show();
  $("workbook-close-btn").hidden = false;
  $("workbook-expand-btn").hidden = true;
  $("workbook-collapse-btn").hidden = true;
  if (workbookGrid) setTimeout(() => workbookGrid.redraw(true), 0);
}

function workbookCloseWindow() {
  const panel = $("workbook-window"), bench = $("workbook-bench");
  if (!panel?.open) return;
  $("workbook-dock").append(bench);
  panel.close();
  $("workbook-close-btn").hidden = true;
  $("workbook-expand-btn").hidden = false;
  $("workbook-collapse-btn").hidden = false;
  $("workbook-open-btn")?.focus();
  if (workbookGrid) setTimeout(() => workbookGrid.redraw(true), 0);
}

async function workbookAskAI(event) {
  event.preventDefault();
  const input = $("workbook-chat-input"), session = workbookCurrentSession();
  if (!input?.value.trim() || !session?.workbookId || workbookIsChatBusy()) return;
  if (workbookPendingChanges.size) {
    workbookShowNotice("Aplique as alterações pendentes antes de pedir à IA para trabalhar nesta planilha.", true);
    return;
  }
  session.useExcelTools = true;
  saveActiveSession();
  $("chat-input").value = input.value;
  updateChatState();
  await submitChat();
  if (session.run) input.value = "";
}

async function workbookCreate(attachmentId = null) {
  if (!workbookExcelAvailable || workbookIsChatBusy() || workbookApplyPending || workbookOpeningPending) return;
  const session = workbookCurrentSession();
  if (!session) return;
  if (workbookPendingChanges.size) {
    workbookShowNotice("Aplique as alterações pendentes antes de abrir outra planilha. O arquivo anexado permanece no seletor para importar depois.", true);
    return;
  }
  workbookShowNotice(attachmentId ? "Importando a planilha anexada…" : "Criando uma planilha vazia…");
  workbookSetBenchExpanded(true);
  workbookOpeningPending = true;
  workbookRefreshChatControls();
  try {
    const snapshot = await api("/api/workbooks/create", attachmentId ? {attachment_id: attachmentId} : {});
    if (session.id !== workbookCurrentSession()?.id) return;
    workbookSessionId = session.id;
    workbookPendingChanges = new Map();
    workbookSelected = {sheet: snapshot.sheets?.[0]?.name || "", cell: "A1"};
    workbookAcceptSnapshot(snapshot, session.id);
  } catch (error) {
    workbookShowNotice(`Não foi possível criar ou importar a planilha: ${error.message}`, true);
  } finally { workbookOpeningPending = false; workbookRefreshChatControls(); }
}

function workbookLiteralValue(value) {
  if (typeof value !== "string") return value;
  if (value.startsWith("'")) return value.slice(1);
  if (!/^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value) || value.replace(/[.+-]/g, "").length > 15) return value;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : value;
}

function workbookQueueChange(change) {
  if (!workbookExcelAvailable || !workbookSnapshot || !change || !change.sheet || !change.cell) return;
  const normalized = {sheet: change.sheet, cell: change.cell, sequence: ++workbookChangeSequence};
  if (Object.prototype.hasOwnProperty.call(change, "formula")) normalized.formula = change.formula;
  else normalized.value = workbookLiteralValue(change.value);
  workbookPendingChanges.set(workbookCellKey(change.sheet, change.cell), normalized);
  if (workbookGrid) workbookGrid.setData(workbookGridRows());
  workbookUpdateControls();
}

function workbookSetSelectedValue() {
  const entry = $("workbook-cell-entry");
  if (!entry || !workbookSelected.cell) return;
  workbookQueueChange({sheet: workbookSelected.sheet, cell: workbookSelected.cell, value: entry.value});
  workbookShowNotice("Valor pendente. Clique em Aplicar alterações para atualizar a planilha.");
}

function workbookSetSelectedFormula() {
  const entry = $("workbook-cell-entry");
  const formula = entry?.value || "";
  if (!entry || !workbookSelected.cell) return;
  if (!formula.trim().startsWith("=")) {
    workbookShowNotice("Uma fórmula precisa começar com =. Para salvar texto, use Definir valor.", true);
    return;
  }
  workbookQueueChange({sheet: workbookSelected.sheet, cell: workbookSelected.cell, formula});
  workbookShowNotice("Fórmula pendente. Clique em Aplicar alterações para calcular e salvar na cópia.");
}

async function workbookApplyChanges() {
  if (!workbookSnapshot || !workbookPendingChanges.size || workbookApplyPending || workbookIsChatBusy()) return;
  const sessionId = workbookCurrentSession()?.id;
  const submitted = [...workbookPendingChanges.entries()].map(([key, change]) => ({key, change: {...change}}));
  const changes = submitted.map(({change}) => {
    const item = {sheet: change.sheet, cell: change.cell};
    if (Object.prototype.hasOwnProperty.call(change, "formula")) item.formula = change.formula;
    else item.value = change.value;
    return item;
  });
  workbookApplyPending = true;
  workbookShowNotice(`Aplicando ${changes.length} ${changes.length === 1 ? "alteração" : "alterações"}…`);
  workbookRefreshChatControls();
  try {
    const updated = await api("/api/workbooks/change", {id: workbookSnapshot.id, expected_revision: workbookSnapshot.revision, changes});
    if (sessionId !== workbookCurrentSession()?.id) return;
    for (const {key, change} of submitted) {
      if (workbookPendingChanges.get(key)?.sequence === change.sequence) workbookPendingChanges.delete(key);
    }
    workbookAcceptSnapshot(updated, sessionId);
    workbookShowNotice(`${changes.length} ${changes.length === 1 ? "alteração aplicada" : "alterações aplicadas"}. Valores calculados pelo Excel foram atualizados.`);
  } catch (error) {
    if (error.status === 409 && workbookSnapshot?.id) {
      const refreshed = await workbookLoadSnapshot(workbookSnapshot.id, {quiet: true});
      if (refreshed) workbookShowNotice("A planilha mudou durante a edição. A versão atual foi carregada e suas alterações continuam pendentes; aplique novamente.", true);
    } else workbookShowNotice(`Não foi possível aplicar as alterações: ${error.message}. Sua edição continua na grade.`, true);
  } finally {
    workbookApplyPending = false;
    workbookRenderGrid();
    workbookRefreshChatControls();
  }
}

async function workbookTestSelectedFormula() {
  const entry = $("workbook-cell-entry");
  const formula = entry?.value || "";
  if (!workbookSnapshot || !workbookSelected.cell || workbookCellTestPending || workbookIsChatBusy()) return;
  if (workbookPendingChanges.size) {
    workbookShowNotice("A prévia usa a última versão salva. Aplique as alterações pendentes antes de testar esta fórmula.", true);
    return;
  }
  if (!formula.trim().startsWith("=")) {
    workbookShowNotice("Digite uma fórmula começando com = para testar no Excel.", true);
    return;
  }
  workbookCellTestPending = true;
  workbookShowNotice("Testando a fórmula no Excel instalado…");
  workbookRefreshChatControls();
  try {
    const result = await api("/api/workbooks/test", {id: workbookSnapshot.id, sheet: workbookSelected.sheet, cell: workbookSelected.cell, formula});
    if (result?.error || result?.ok === false) workbookShowNotice(`Excel retornou erro em ${workbookSelected.cell}: ${result.error || result.value || "A fórmula retornou uma falha."}`, true);
    else workbookShowNotice(`Prévia em ${workbookSelected.cell}: ${result?.formula_local || result?.formula || formula} → ${result?.value ?? "(vazio)"}. A fórmula não foi salva.`, false);
  } catch (error) { workbookShowNotice(`Falha ao testar a fórmula: ${error.message}. A planilha não foi alterada.`, true); }
  finally { workbookCellTestPending = false; workbookRefreshChatControls(); }
}

function workbookDownloadCopy() {
  const id = workbookCurrentSession()?.workbookId;
  if (!id || workbookIsChatBusy()) return;
  const link = document.createElement("a");
  link.href = `/api/workbooks/download?id=${encodeURIComponent(id)}`;
  const name = String(workbookSnapshot?.name || "planilha").replace(/[\\/:*?"<>|]/g, "_");
  link.download = `${name}.xlsx`;
  link.click();
}

function workbookGetAdapterRun(session) {
  if (!session?.selection) return null;
  if (session.adapter && session.selection === "latest") return session.adapter;
  if (session.selection === "latest") return chatLatest?.id || null;
  return session.adapter || session.selection;
}

function workbookHumanizeToolName(name) {
  if (WORKBOOK_TRACE_LABELS[name]) return WORKBOOK_TRACE_LABELS[name];
  const plain = String(name || "ferramenta").replace(/[_-]+/g, " ").trim();
  return plain ? plain[0].toLocaleUpperCase("pt-BR") + plain.slice(1) : "Ferramenta do Excel";
}

function workbookPrintable(value) {
  if (value === undefined || value === null) return "(sem resultado)";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function workbookRenderToolTrace(row, response) {
  if (!row || !response || (typeof response.tools_used !== "boolean" && !Array.isArray(response.tool_trace))) return;
  const calls = Array.isArray(response.tool_trace) ? response.tool_trace : [];
  const count = Number.isFinite(response.tool_calls) ? response.tool_calls : calls.length;
  const details = document.createElement("details");
  details.className = "workbook-tool-trace";
  const summary = document.createElement("summary");
  summary.textContent = count === 0 ? "Ferramentas do Excel · 0 chamadas" : `Ferramentas do Excel · ${count} ${count === 1 ? "chamada" : "chamadas"}`;
  details.append(summary);
  if (calls.length) {
    calls.forEach(call => {
      const item = document.createElement("div");
      item.className = "workbook-tool-call";
      const title = document.createElement("strong");
      title.textContent = workbookHumanizeToolName(call?.name);
      item.append(title);
      if (call?.arguments !== undefined) {
        const args = document.createElement("pre");
        args.textContent = `Parâmetros\n${workbookPrintable(call.arguments)}`;
        item.append(args);
      }
      const toolError = call?.error || call?.result?.error;
      if (toolError) {
        const error = document.createElement("pre");
        error.className = "workbook-tool-error";
        error.textContent = `Erro\n${workbookPrintable(toolError)}`;
        item.append(error);
      } else {
        const result = document.createElement("pre");
        result.textContent = `Resultado\n${workbookPrintable(call?.result?.result ?? call?.result)}`;
        item.append(result);
      }
      details.append(item);
    });
  } else if (count > 0) {
    const missing = document.createElement("div");
    missing.className = "workbook-tool-call";
    missing.textContent = "A resposta registra chamadas, mas o detalhe dos resultados não está disponível.";
    details.append(missing);
  }
  const wrap = row.querySelector?.(".chat-msg-content-wrap");
  if (wrap) wrap.append(details);
}

function workbookRefreshAfterChat(response) {
  const session = workbookCurrentSession();
  if (!session?.workbookId || response?.tools_used !== true || (response.workbook_id && response.workbook_id !== session.workbookId)) return;
  const writes = (response.tool_trace || []).filter(call => call.name === "excel_write_cells" && call.result?.ok !== false);
  const written = writes.at(-1)?.result;
  const revision = written?.result?.revision ?? written?.revision;
  const key = `${session.id}:${session.run || "completed"}:${revision ?? (response.complete ? "final" : "pending")}`;
  if ((revision === undefined && !response.complete) || workbookSeenRevision === key) return;
  workbookSeenRevision = key;
  void workbookLoadSnapshot(session.workbookId, {quiet: true}).then(snapshot => {
    if (!snapshot && workbookSeenRevision === key) workbookSeenRevision = null;
  });
}

function workbookMetric(label, value, note = "") {
  const item = document.createElement("div");
  item.className = "workbook-eval-metric";
  item.append(element("span", label), element("strong", value));
  if (note) item.append(element("span", note));
  return item;
}

function workbookFormatAccuracy(result) {
  if (!result) return "—";
  const accuracy = Number.isFinite(result.accuracy) ? result.accuracy * 100 : NaN;
  return Number.isFinite(accuracy) ? `${number(accuracy, 2)}%` : "—";
}

function workbookRenderEvaluation(report, rootId = "workbook-evaluation-result-content", detailsId = "workbook-evaluation-results") {
  const root = $(rootId), details = $(detailsId);
  if (!root || !details || !report || typeof report !== "object") return;
  root.replaceChildren();
  const total = Number.isFinite(report.total) ? report.total : 0;
  const without = report.without_tools || {};
  const withTools = report.with_tools || {};
  const comparison = report.comparison || {};
  const metrics = document.createElement("div");
  metrics.className = "workbook-eval-metrics";
  metrics.append(
    workbookMetric("Sem ferramentas · acerto exato", workbookFormatAccuracy(without), `${number(without.correct || 0)} / ${number(total)}`),
    workbookMetric("Com ferramentas · acerto exato", workbookFormatAccuracy(withTools), `${number(withTools.correct || 0)} / ${number(total)}`),
    workbookMetric("Diferença", Number.isFinite(comparison.delta_pp) ? `${comparison.delta_pp > 0 ? "+" : ""}${number(comparison.delta_pp, 2)} p.p.` : "—", `${number(comparison.gained || 0)} novos · ${number(comparison.lost || 0)} regressões`),
    workbookMetric("Fórmulas aceitas pelo Excel", `${number(withTools.execution_ok || 0)} / ${number(total)}`, "Aceitação não mede correspondência ao gabarito"),
    workbookMetric("Chamadas de ferramenta", number(withTools.tool_calls || 0), "Registradas na condição com ferramentas"),
  );
  root.append(metrics);
  root.append(element("p", `Métrica: correspondência exata normalizada em ${number(total)} testes. “Aceitas pelo Excel” conta fórmulas sem erro de execução; não indica que o resultado está correto.`, "workbook-eval-note"));
  root.append(element("p", "A condição com ferramentas dispõe de mais rodadas e tokens. O teste mede esse modo completo; a execução usa apenas a amostra do contexto.", "workbook-eval-note"));
  const items = Array.isArray(report.items) ? report.items : [];
  if (items.length) {
    const responses = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `Ver respostas pareadas (${number(items.length)})`;
    responses.append(summary);
    const table = document.createElement("table");
    table.className = "workbook-eval-table";
    const thead = document.createElement("thead"), headRow = document.createElement("tr");
    ["ID", "Sem ferramentas", "Com ferramentas"].forEach(label => headRow.append(element("th", label)));
    thead.append(headRow);
    const tbody = document.createElement("tbody");
    items.forEach(item => {
      const row = document.createElement("tr");
      row.append(element("th", item.id || "—"));
      for (const key of ["without_tools", "with_tools"]) {
        const cell = document.createElement("td");
        const answer = document.createElement("pre");
        answer.textContent = item[key]?.predicted || "(sem fórmula)";
        const status = document.createElement("span");
        status.textContent = item[key]?.match ? "Acerto" : "Divergência";
        status.className = item[key]?.match ? "workbook-eval-match" : "workbook-eval-miss";
        cell.append(status, answer);
        row.append(cell);
      }
      tbody.append(row);
    });
    table.append(thead, tbody);
    const tableWrap = document.createElement("div");
    tableWrap.className = "workbook-eval-table-wrap";
    tableWrap.append(table);
    responses.append(tableWrap);
    root.append(responses);
  }
  details.hidden = false;
}

function workbookStatusText(detail) {
  const phase = detail.events?.slice?.().reverse?.().find(event => event.phase)?.phase;
  const labels = {queued: "Na fila", running: "Em execução", completed: "Concluído", failed: "Falhou", cancelled: "Interrompido", interrupted: "Processo encerrado"};
  return `${labels[detail.status] || detail.status || "Em andamento"}${phase ? ` · ${phase}` : ""}`;
}

async function workbookPollEvaluation(runId) {
  if (!runId) return;
  const sessionId = workbookCurrentSession()?.id;
  try {
    const detail = await api(`/api/run?id=${encodeURIComponent(runId)}`);
    if (sessionId !== workbookCurrentSession()?.id || workbookCurrentSession()?.toolEvalRunId !== runId) return;
    const status = $("workbook-evaluation-status");
    const goto = $("workbook-evaluation-goto");
    if (status) {
      status.textContent = workbookStatusText(detail);
      status.classList.toggle("error", ["failed", "cancelled", "interrupted"].includes(detail.status));
      status.classList.toggle("ready", detail.status === "completed");
    }
    if (goto) goto.hidden = false;
    if (detail.status === "completed") {
      if (detail.tool_report) {
        const pairedReport = await api(`/api/tool-report?id=${encodeURIComponent(runId)}`);
        if (sessionId !== workbookCurrentSession()?.id || workbookCurrentSession()?.toolEvalRunId !== runId) return;
        workbookRenderEvaluation(pairedReport);
        if (status) status.textContent = `Comparação concluída · ${number(detail.tool_report.total || 0)} testes`;
      } else if (status) status.textContent = "Execução concluída, mas o relatório pareado ainda não está disponível.";
      workbookEvalPending = false;
      workbookEvalSessionId = null;
      workbookRefreshChatControls();
      return;
    }
    if (["failed", "cancelled", "interrupted"].includes(detail.status)) {
      if (status) status.textContent = detail.error || `Comparação ${detail.status}.`;
      workbookEvalPending = false;
      workbookEvalSessionId = null;
      workbookRefreshChatControls();
      return;
    }
  } catch (error) {
    if (sessionId === workbookCurrentSession()?.id && workbookCurrentSession()?.toolEvalRunId === runId) {
      const status = $("workbook-evaluation-status");
      if (status) { status.textContent = `Acompanhando comparação: ${error.message}`; status.classList.add("error"); }
      if ([400, 404].includes(error.status)) {
        workbookEvalPending = false;
        workbookEvalSessionId = null;
        workbookRefreshChatControls();
        return;
      }
    }
  }
  if (sessionId === workbookCurrentSession()?.id && workbookCurrentSession()?.toolEvalRunId === runId) workbookEvalTimer = setTimeout(() => workbookPollEvaluation(runId), 1500);
}

async function workbookStartEvaluation() {
  const session = workbookCurrentSession();
  if (!session?.workbookId || (workbookEvalPending && workbookEvalSessionId === session.id) || workbookIsChatBusy() || workbookHasOtherRun()) return;
  const testCount = Number($("workbook-test-count")?.value || 10);
  if (![10, 50, 500].includes(testCount)) return;
  workbookEvalPending = true;
  workbookEvalSessionId = session.id;
  const status = $("workbook-evaluation-status");
  if (status) { status.textContent = "Iniciando comparação pareada…"; status.classList.remove("error", "ready"); }
  workbookRefreshChatControls();
  try {
    const request = {
      kind: "tool_evaluate",
      adapter_run: workbookGetAdapterRun(session),
      tool_eval: {test_count: testCount, max_tool_calls: 4},
    };
    if (session.withDecensor && !selectedDecensor(session)) request.with_decensor = true;
    const result = await api("/api/start", request);
    if (session.id !== workbookCurrentSession()?.id) return;
    session.toolEvalRunId = result.id;
    saveActiveSession();
    if (typeof selectedId !== "undefined") selectedId = result.id;
    if ($("workbook-evaluation-results")) $("workbook-evaluation-results").hidden = true;
    if (typeof refresh === "function") void refresh();
    workbookEvalPending = true;
    const goto = $("workbook-evaluation-goto");
    if (goto) goto.hidden = false;
    if (workbookEvalTimer) clearTimeout(workbookEvalTimer);
    void workbookPollEvaluation(result.id);
  } catch (error) {
    if (status) { status.textContent = `Não foi possível iniciar a comparação: ${error.message}`; status.classList.add("error"); }
    workbookEvalPending = false;
    workbookEvalSessionId = null;
  } finally { workbookRefreshChatControls(); }
}

function workbookGoToEvaluation(event) {
  event.preventDefault();
  if (typeof switchTab === "function") switchTab("workspace");
  $("monitor-title")?.scrollIntoView?.({behavior: "smooth", block: "start"});
}

function initWorkbookUI() {
  $("workbook-open-btn")?.addEventListener("click", workbookOpenWindow);
  $("workbook-expand-btn")?.addEventListener("click", workbookOpenWindow);
  $("workbook-close-btn")?.addEventListener("click", workbookCloseWindow);
  $("workbook-window")?.addEventListener("cancel", event => { event.preventDefault(); workbookCloseWindow(); });
  $("workbook-window")?.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); workbookCloseWindow(); } });
  $("workbook-chat-form")?.addEventListener("submit", workbookAskAI);
  $("workbook-collapse-btn")?.addEventListener("click", () => workbookSetBenchExpanded($("workbook-collapse-btn").getAttribute("aria-expanded") !== "true"));
  $("workbook-attachment-select")?.addEventListener("change", workbookUpdateControls);
  $("workbook-import-btn")?.addEventListener("click", () => workbookCreate($("workbook-attachment-select")?.value || null));
  $("workbook-create-btn")?.addEventListener("click", () => workbookCreate(null));
  $("workbook-download-btn")?.addEventListener("click", workbookDownloadCopy);
  $("workbook-sheet-select")?.addEventListener("change", () => {
    workbookSelected.sheet = $("workbook-sheet-select").value;
    workbookSelected.cell = "A1";
    workbookRenderGrid();
    workbookSetSelectedCell(workbookSelected.sheet, workbookSelected.cell);
  });
  $("workbook-set-value-btn")?.addEventListener("click", workbookSetSelectedValue);
  $("workbook-set-formula-btn")?.addEventListener("click", workbookSetSelectedFormula);
  $("workbook-test-formula-btn")?.addEventListener("click", workbookTestSelectedFormula);
  $("workbook-apply-btn")?.addEventListener("click", workbookApplyChanges);
  $("chat-use-excel-tools")?.addEventListener("change", () => {
    const session = workbookCurrentSession();
    if (!session) return;
    session.useExcelTools = Boolean($("chat-use-excel-tools").checked && session.workbookId);
    saveActiveSession();
    workbookUpdateControls();
  });
  $("workbook-compare-btn")?.addEventListener("click", workbookStartEvaluation);
  $("workbook-evaluation-goto")?.addEventListener("click", workbookGoToEvaluation);
  if (typeof window.matchMedia === "function" && window.matchMedia("(max-width: 720px), (max-height: 680px)").matches && !workbookCurrentSession()?.workbookId) workbookSetBenchExpanded(false);
  workbookSessionChanged();
  void workbookRefreshAvailability();
  const runId = workbookCurrentSession()?.toolEvalRunId;
  if (runId) {
    workbookEvalPending = true;
    workbookEvalSessionId = workbookCurrentSession().id;
    void workbookPollEvaluation(runId);
  }
  workbookRefreshChatControls();
}
