"use strict";

const CHAT_STORAGE = "formula-lab-chat-sessions-v3";
const CHAT_RUN_ID = /^\d{8}-\d{6}-[0-9a-f]{8}$/;
let chatSessions = [], activeChatSessionId = null;
let chatPending = false, chatPolling = false, chatUploading = false;
let chatPhase = "", chatLatest = null, chatModelSignature = "";

function currentChat() { return chatSessions.find(session => session.id === activeChatSessionId); }
function chatBusy() { return Boolean(currentChat()?.run || chatPending || chatUploading || (typeof workbookNetworkPending === "function" && workbookNetworkPending())); }
function selectedDecensor(session) {
  const id = session?.adapter || session?.selection;
  return Boolean(id && id !== "latest" && jobs.some(job => job.kind === "decensor" && job.id === id));
}
function chatModelLabel() {
  const session = currentChat();
  const stacked = Boolean(session?.withDecensor) && !selectedDecensor(session);
  if (!session?.selection) return stacked ? "Qwen · base + recusa" : "Qwen · modelo base";
  const adapter = session.adapter || (session.selection === "latest" ? chatLatest?.id : session.selection);
  const id = (adapter || session.selection).slice(-8);
  if (selectedDecensor(session)) return `Qwen · recusa · ${id}`;
  const name = jobs.find(job => job.id === adapter)?.label || id;
  return stacked ? `Qwen · ${name} + recusa` : `Qwen · ${name}`;
}
function chatNotice(text) {
  const node = $("chat-notice");
  if (node) { node.textContent = text; node.hidden = !text; }
}
function chatSpeed(response) {
  if (!response?.generated_tokens) return "";
  const speed = response.generation_seconds ? `${number(response.generated_tokens / response.generation_seconds, 1)} tokens/s · ${number(response.generated_tokens)} tokens` : `${number(response.generated_tokens)} tokens`;
  const mode = response.generation_mode === "accelerated" ? "acelerado" : response.generation_mode === "standard" ? "compatibilidade" : "";
  return mode ? `${speed} · ${mode}` : speed;
}
function generateSessionTitle(prompt) {
  const first = (prompt || "Nova conversa").trim().split(/\r?\n/)[0];
  return first.length > 40 ? `${first.slice(0, 38).trim()}…` : first;
}
function normalizeChatAttachments(files) {
  if (!Array.isArray(files)) return [];
  return files.filter(file => file && typeof file.id === "string" && typeof file.name === "string").map(file => ({
    ...file,
    context: typeof file.context === "string" ? file.context : "",
    preview: typeof file.preview === "string" ? file.preview : "",
    warnings: Array.isArray(file.warnings) ? file.warnings.filter(warning => typeof warning === "string") : [],
  }));
}
function normalizeChatResponse(response) {
  if (!response || typeof response !== "object" || Array.isArray(response)) return null;
  const normalized = {};
  for (const key of ["generated_tokens", "generation_seconds", "prompt_tokens", "workbook_revision"]) {
    if (Number.isFinite(response[key])) normalized[key] = response[key];
  }
  for (const key of ["generation_mode", "workbook_id"]) if (typeof response[key] === "string") normalized[key] = response[key];
  for (const key of ["reached_token_limit", "tools_used"]) if (typeof response[key] === "boolean") normalized[key] = response[key];
  if (Number.isFinite(response.tool_calls)) normalized.tool_calls = response.tool_calls;
  if (Array.isArray(response.tool_trace)) normalized.tool_trace = response.tool_trace.slice(0, 20).filter(item => item && typeof item === "object").map(item => {
    const call = {};
    if (typeof item.name === "string") call.name = item.name;
    for (const key of ["arguments", "result"]) if (item[key] !== undefined) call[key] = item[key];
    if (typeof item.error === "string") call.error = item.error;
    if (Number.isFinite(item.elapsed_seconds)) call.elapsed_seconds = item.elapsed_seconds;
    return call;
  });
  return Object.keys(normalized).length ? normalized : null;
}
function normalizeChatSession(session) {
  if (!session || typeof session.id !== "string" || !Array.isArray(session.messages)) return null;
  const messages = session.messages.filter(message => message && ["user", "assistant"].includes(message.role) && typeof message.content === "string").map(message => ({
    ...message,
    display_content: typeof message.display_content === "string" ? message.display_content : message.content,
    attachments: normalizeChatAttachments(message.attachments),
    response: normalizeChatResponse(message.response),
  }));
  const workbookId = typeof session.workbookId === "string" ? session.workbookId : null;
  return {
    id: session.id,
    title: typeof session.title === "string" && session.title.trim() ? session.title : generateSessionTitle(messages[0]?.display_content),
    selection: typeof session.selection === "string" ? session.selection : "",
    withDecensor: session.withDecensor === true,
    adapter: typeof session.adapter === "string" ? session.adapter : null,
    workbookId,
    useExcelTools: Boolean(workbookId && session.useExcelTools === true),
    toolEvalRunId: typeof session.toolEvalRunId === "string" && CHAT_RUN_ID.test(session.toolEvalRunId) ? session.toolEvalRunId : null,
    messages,
    draft: typeof session.draft === "string" ? session.draft : "",
    attachments: normalizeChatAttachments(session.attachments),
    run: typeof session.run === "string" && CHAT_RUN_ID.test(session.run) ? session.run : null,
    updatedAt: Number.isFinite(session.updatedAt) ? session.updatedAt : Date.now(),
  };
}
function loadChatSessions() {
  try {
    if (typeof localStorage !== "undefined") {
      const saved = JSON.parse(localStorage.getItem(CHAT_STORAGE) || "null");
      if (Array.isArray(saved?.sessions)) { activeChatSessionId = saved.activeId; return saved.sessions.map(normalizeChatSession).filter(Boolean); }
    }
    const previous = typeof localStorage === "undefined" ? [] : JSON.parse(localStorage.getItem("formula-lab-chat-sessions-v2") || "[]");
    const legacy = typeof sessionStorage === "undefined" ? null : JSON.parse(sessionStorage.getItem("formula-lab-chat") || "null");
    if (Array.isArray(previous) && previous.length) {
      const sessions = previous.map(normalizeChatSession).filter(Boolean);
      const history = Array.isArray(legacy?.history) ? JSON.stringify(legacy.history.map(({role, content}) => ({role, content}))) : null;
      const match = history && sessions.find(session => JSON.stringify(session.messages.map(({role, content}) => ({role, content}))) === history);
      if (match) { match.run = typeof legacy.run === "string" && CHAT_RUN_ID.test(legacy.run) ? legacy.run : null; activeChatSessionId = match.id; }
      return sessions;
    }
    if (Array.isArray(legacy?.history) && legacy.history.length) return [normalizeChatSession({id: `migrated_${Date.now()}`, title: generateSessionTitle(legacy.history[0]?.content), messages: legacy.history, selection: legacy.selection || "", adapter: legacy.adapter || null, run: legacy.run})];
  } catch (error) { chatNotice("Não foi possível recuperar o histórico salvo neste navegador. A conversa atual permanece disponível enquanto a página estiver aberta."); }
  return [];
}
function saveChatSessions() {
  try { if (typeof localStorage !== "undefined") localStorage.setItem(CHAT_STORAGE, JSON.stringify({activeId: activeChatSessionId, sessions: chatSessions})); }
  catch (error) { chatNotice("O navegador não conseguiu salvar o histórico. Exporte esta conversa antes de fechar a página."); }
}
function saveActiveSession() {
  const session = currentChat(); session.updatedAt = Date.now();
  if (session.messages.length && session.title === "Nova conversa") session.title = generateSessionTitle(session.messages[0].display_content || session.messages[0].content);
  saveChatSessions(); renderChatSessionsList();
}
function createNewSession(title = "Nova conversa", selection = currentChat()?.selection || "", withDecensor = Boolean(currentChat()?.withDecensor)) {
  const session = {id: `session_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`, title, selection, withDecensor: Boolean(withDecensor), adapter: null, workbookId: null, useExcelTools: false, toolEvalRunId: null, messages: [], run: null, draft: "", attachments: [], updatedAt: Date.now()};
  chatSessions.unshift(session); activeChatSessionId = session.id; chatPhase = ""; chatStreamingRow = null;
  if ($("chat-input")) $("chat-input").value = "";
  saveChatSessions(); renderChatSessionsList(); if (typeof workbookSessionChanged === "function") workbookSessionChanged(); return session;
}
function selectChatSession(id) {
  if (chatBusy() || activeChatSessionId === id) return;
  const session = chatSessions.find(item => item.id === id); if (!session) return;
  activeChatSessionId = id; chatPhase = ""; chatStreamingRow = null; $("chat-input").value = session.draft;
  renderActiveChatMessages(); renderAttachments(); saveChatSessions(); renderChatSessionsList(); updateChatState(); if (typeof workbookSessionChanged === "function") workbookSessionChanged();
}
function deleteChatSession(id, event) {
  event?.stopPropagation?.(); if (chatBusy()) return;
  chatSessions = chatSessions.filter(session => session.id !== id);
  if (activeChatSessionId === id) {
    activeChatSessionId = null;
    if (chatSessions.length) selectChatSession(chatSessions[0].id);
    else { createNewSession(); renderActiveChatMessages(); renderAttachments(); }
  }
  saveChatSessions(); renderChatSessionsList(); updateChatState();
}
function renderChatSessionsList() {
  const container = $("chat-sessions-list"); if (!container) return;
  container.replaceChildren(); if ($("chat-history-count")) $("chat-history-count").textContent = String(chatSessions.length);
  const search = $("chat-history-search")?.value.toLocaleLowerCase() || "";
  chatSessions.filter(session => session.title.toLocaleLowerCase().includes(search)).forEach(session => {
    const row = element("div", undefined, `chat-session-item${session.id === activeChatSessionId ? " active" : ""}`);
    row.setAttribute?.("role", "listitem");
    const open = element("button", undefined, "chat-session-open"); open.type = "button"; open.disabled = chatBusy();
    open.append(element("strong", session.title, "chat-session-title"), element("span", `${session.messages.length} mensagens${session.run ? " · gerando" : ""}`, "chat-session-meta"));
    open.addEventListener("click", () => selectChatSession(session.id));
    const remove = element("button", "×", "chat-session-delete-btn"); remove.type = "button"; remove.title = `Excluir ${session.title}`; remove.disabled = chatBusy(); remove.setAttribute?.("aria-label", remove.title);
    remove.addEventListener("click", event => deleteChatSession(session.id, event)); row.append(open, remove); container.append(row);
  });
  if (!container.children.length) container.append(element("p", "Nenhuma conversa encontrada.", "chat-sessions-empty"));
}
function renderActiveChatMessages() {
  const container = $("chat-messages"); if (!container) return; container.replaceChildren();
  const session = currentChat();
  if (!session.messages.length) {
    const empty = element("div", undefined, "chat-empty"); empty.id = "chat-empty";
    empty.append(element("h2", "Qual fórmula você precisa?"), element("p", "Descreva o resultado ou anexe uma planilha. Você pode conferir o contexto antes de enviar.")); container.append(empty);
  } else session.messages.forEach(message => {
    const row = addChatMessage(message.role, message.display_content || message.content, message.attachments || [], message.response);
    if (typeof workbookRenderToolTrace === "function") workbookRenderToolTrace(row, message.response);
  });
  if (session.run) chatStreamingRow = createStreamingAssistantRow();
}
function resetChat() {
  if (chatBusy()) return;
  createNewSession(); renderActiveChatMessages(); renderAttachments(); chatNotice(""); updateChatState(); $("chat-input")?.focus();
}
function updateChatState() {
  const session = currentChat(); if (!session) return;
  const trained = jobs.filter(job => job.kind === "train" && job.status === "completed" && job.has_adapter).sort((a, b) => (b.ended_at || 0) - (a.ended_at || 0));
  const decensors = jobs.filter(job => job.kind === "decensor" && job.status === "completed" && job.has_adapter).sort((a, b) => (b.ended_at || 0) - (a.ended_at || 0));
  chatLatest = trained[0] || null;
  const select = $("chat-model-select"), signature = JSON.stringify([...trained, ...decensors].map(job => [job.id, job.label])) + session.selection;
  if (select && chatModelSignature !== signature) {
    select.replaceChildren(); const base = element("option", "Modelo base · original"); base.value = ""; select.append(base);
    const latest = element("option", "Último treinado"); latest.value = "latest"; latest.disabled = !chatLatest; select.append(latest);
    trained.forEach(job => { const option = element("option", job.label || `${job.id.slice(-8)} · ${number(job.config.train_samples)} exemplos`); option.value = job.id; select.append(option); });
    decensors.forEach(job => { const option = element("option", `Recusa · ${job.id.slice(-8)}`); option.value = job.id; select.append(option); });
    const known = !session.selection || session.selection === "latest" || trained.some(job => job.id === session.selection) || decensors.some(job => job.id === session.selection);
    if (!known) { const old = element("option", `Adaptador ${session.selection.slice(-8)} · indisponível`); old.value = session.selection; select.append(old); }
    chatModelSignature = signature;
  }
  if (select) { select.value = session.selection; select.disabled = chatBusy(); }
  const adapter = session.adapter || (session.selection === "latest" ? chatLatest?.id : session.selection);
  const refusalOnly = selectedDecensor(session);
  const stacked = Boolean(session.withDecensor) && !refusalOnly && decensors.length > 0;
  const box = $("chat-with-decensor");
  if (box) { box.checked = stacked; box.disabled = chatBusy() || refusalOnly || !decensors.length; }
  if ($("chat-model-badge")) $("chat-model-badge").textContent = chatModelLabel();
  if ($("chat-model-detail")) {
    $("chat-model-detail").textContent = refusalOnly ? `Adaptador de recusa ${adapter} · fixo nesta conversa`
      : session.selection && stacked ? `Adaptador ${adapter} + recusa · fixos nesta conversa`
      : session.selection ? `Adaptador ${adapter || "indisponível"} · fixo nesta conversa`
      : stacked ? "Qwen original + adaptador de recusa · geração local na GPU"
      : "Qwen original · geração local na GPU";
  }
  const busy = jobs.find(job => !terminal.has(job.status) && job.id !== session.run);
  if ($("chat-running-banner")) $("chat-running-banner").hidden = !busy;
  if (busy && $("chat-banner-info")) $("chat-banner-info").textContent = `${kinds[busy.kind] || "Chat"} em execução. Você pode preparar sua pergunta enquanto aguarda.`;
  for (const id of ["chat-clear-btn", "chat-new-btn", "chat-clear-all-btn", "chat-attach-btn"]) if ($(id)) $(id).disabled = chatBusy();
  $("chat-sessions-list")?.querySelectorAll("button").forEach(button => { button.disabled = chatBusy(); });
  if ($("chat-send-btn")) $("chat-send-btn").disabled = !online || chatBusy() || Boolean(busy) || !$("chat-input").value.trim() || Boolean(session.selection && !adapter);
  if ($("chat-stop-btn")) { $("chat-stop-btn").hidden = !session.run; $("chat-stop-btn").disabled = !online; }
  if ($("chat-generation-status")) $("chat-generation-status").textContent = !online ? "Reconectando ao servidor…" : chatUploading ? "Lendo a planilha…" : (typeof workbookNetworkStatus === "function" && workbookNetworkStatus()) || chatPhase || (session.run ? "Recuperando geração…" : "Pronto para enviar · execução local");
  if ($("chat-suggestions")) $("chat-suggestions").hidden = Boolean(session.messages.length);
  if ($("chat-session-subtitle")) $("chat-session-subtitle").textContent = session.title;
  if ($("chat-input-count")) $("chat-input-count").textContent = `${number($("chat-input").value.length)} caracteres`;
  if (typeof workbookUpdateControls === "function") workbookUpdateControls();
}
function renderAttachments() {
  const container = $("chat-attachments"); if (!container) return; container.replaceChildren();
  const files = currentChat()?.attachments || [];
  files.forEach(file => {
    const details = element("details", undefined, "chat-attachment"), summary = element("summary");
    summary.append(element("strong", file.name), element("span", "Ver contexto", "chat-attachment-meta"));
    const remove = element("button", "Remover", "text-button"); remove.type = "button"; remove.disabled = chatBusy();
    remove.addEventListener("click", () => { currentChat().attachments = files.filter(item => item.id !== file.id); renderAttachments(); saveActiveSession(); });
    details.append(summary, element("pre", file.context || file.preview || "Prévia indisponível", "chat-attachment-preview"));
    (file.warnings || []).forEach(warning => details.append(element("p", warning, "chat-attachment-warning"))); details.append(remove); container.append(details);
  });
  container.hidden = !files.length;
  if ($("chat-context-count")) $("chat-context-count").textContent = files.length ? `${files.length} ${files.length === 1 ? "arquivo no próximo envio" : "arquivos no próximo envio"}` : "Nenhum arquivo anexado";
  if (typeof workbookRenderAttachmentOptions === "function") workbookRenderAttachmentOptions();
}
function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = () => reject(new Error(`Não foi possível ler ${file.name}.`)); reader.readAsDataURL(file);
  });
}
async function attachChatFiles(files) {
  if (chatBusy()) return; const session = currentChat();
  if (session.attachments.length + files.length > 3) { chatNotice("Anexe até três planilhas por mensagem. Remova um arquivo para acrescentar outro."); return; }
  chatUploading = true; chatNotice(""); updateChatState();
  let uploadedId = null;
  try {
    for (const file of files) {
      if (!/\.(xlsx|csv|tsv)$/i.test(file.name)) throw new Error("Use XLSX, CSV ou TSV. O Qwen atual não lê imagens.");
      if (file.size > 8 * 1024 * 1024) throw new Error(`${file.name} excede 8 MB. Exporte somente as abas ou linhas necessárias.`);
      const attachment = await api("/api/attachments", {name: file.name, content_base64: await fileBase64(file)});
      session.attachments.push(attachment); uploadedId = attachment.id; saveActiveSession();
    }
  } catch (error) { chatNotice(error.message); }
  finally { chatUploading = false; renderAttachments(); updateChatState(); }
  if (session.id === currentChat()?.id && uploadedId && typeof workbookCreate === "function") {
    await workbookCreate(uploadedId);
    if (typeof workbookOpenWindow === "function") workbookOpenWindow();
  }
}
async function submitChat() {
  if ($("chat-send-btn")?.disabled) return;
  const session = currentChat(), prompt = $("chat-input").value.trim();
  if (session.useExcelTools && !session.workbookId) { chatNotice("Crie ou importe uma planilha para usar as ferramentas do Excel nesta conversa."); return; }
  const messages = [...session.messages.map(message => ({role: message.role, content: message.content})), {role: "user", content: prompt}];
  if (messages.length > 199 || messages.reduce((sum, message) => sum + message.content.length, 0) > 200000) { chatNotice("Esta conversa atingiu o limite de histórico. Inicie outra conversa e anexe somente o contexto necessário."); return; }
  chatPending = true; chatNotice(""); chatPhase = "Iniciando geração…"; updateChatState();
  try {
    const request = {kind: "chat", messages, attachment_ids: session.attachments.map(file => file.id), adapter_run: session.selection ? session.adapter || session.selection : null, with_decensor: Boolean(session.withDecensor) && !selectedDecensor(session)};
    if (session.workbookId) { request.workbook_id = session.workbookId; request.use_excel_tools = Boolean(session.useExcelTools); }
    const result = await api("/api/start", request);
    session.messages.push({...result.messages?.at(-1) || messages.at(-1), display_content: prompt, attachments: session.attachments});
    session.run = result.id; session.adapter = result.adapter_run; session.attachments = []; session.draft = ""; $("chat-input").value = "";
    $("chat-empty")?.remove(); addChatMessage("user", prompt, session.messages.at(-1).attachments); chatStreamingRow = createStreamingAssistantRow(); chatPhase = "Preparando resposta…"; saveActiveSession(); renderAttachments();
  } catch (error) { chatNotice(error.message); chatPhase = "Sua pergunta e seus anexos foram preservados."; }
  finally { chatPending = false; updateChatState(); }
}
function restorePendingChat(session) {
  session.run = null;
  chatStreamingRow?.remove(); chatStreamingRow = null;
  if (session.messages.at(-1)?.role === "user") {
    const last = session.messages.pop();
    session.draft = last.display_content || last.content; session.attachments = last.attachments || [];
    $("chat-input").value = session.draft;
  }
  renderActiveChatMessages(); renderAttachments();
}
async function pollChat() {
  const session = currentChat();
  if (session?.run && !chatPolling && online) {
    chatPolling = true;
    try {
      const detail = await api(`/api/run?id=${session.run}`), response = detail.chat_response;
      if (typeof workbookRefreshAfterChat === "function" && response) workbookRefreshAfterChat(response);
      const phase = detail.events?.slice().reverse().find(event => event.phase)?.phase || "Preparando ambiente Python";
      chatPhase = chatSpeed(response) ? `${phase} · ${chatSpeed(response)}` : phase;
      if (response?.text && !terminal.has(detail.status)) updateStreamingAssistant(response.text);
      if (terminal.has(detail.status)) {
        session.run = null;
        if (detail.status === "completed" && response?.text) {
          const responseRow = chatStreamingRow;
          finalizeStreamingAssistant(response.text, response);
          if (typeof workbookRenderToolTrace === "function") workbookRenderToolTrace(responseRow || $("chat-messages")?.lastElementChild, response);
          session.messages.push({role: "assistant", content: response.text, response: normalizeChatResponse(response)});
          chatPhase = `Concluído · ${chatSpeed(response)}`;
          if (response.reached_token_limit) chatNotice("A resposta atingiu o limite de geração e pode estar incompleta.");
        } else {
          restorePendingChat(session); chatNotice(detail.error || "Geração interrompida. Sua pergunta e seus anexos estão prontos para tentar novamente."); chatPhase = "Geração encerrada sem resposta completa.";
          if (typeof workbookShowNotice === "function") workbookShowNotice(detail.error || "A IA foi interrompida. Confira as alterações já concluídas na planilha.", true);
        }
        saveActiveSession();
      }
    } catch (error) {
      if ([400, 404].includes(error.status)) {
        restorePendingChat(session); saveActiveSession();
        chatNotice("A geração salva não está mais disponível. Sua pergunta e seus anexos foram recuperados para tentar novamente.");
        chatPhase = "Pronto para tentar novamente.";
      } else chatPhase = `Tentando recuperar a geração: ${error.message}`;
    }
    finally { chatPolling = false; updateChatState(); }
  }
  setTimeout(pollChat, session?.run ? 400 : 1500);
}
function switchTab(targetTab) {
  document.querySelectorAll(".nav-tab").forEach(tab => tab.classList.toggle("active", tab.dataset.tab === targetTab));
  $("view-workspace").hidden = targetTab === "chat"; $("view-chat").hidden = targetTab !== "chat"; document.body?.classList.toggle("chat-mode", targetTab === "chat");
  if (targetTab === "chat") $("chat-input")?.focus(); else if (["history", "comparison"].includes(targetTab)) $(targetTab)?.scrollIntoView?.({behavior: "smooth"});
}
document.querySelectorAll(".nav-tab").forEach(link => link.addEventListener("click", event => {
  event.preventDefault(); const tab = link.dataset.tab || link.getAttribute("href").slice(1); if (typeof history !== "undefined") history.pushState(null, "", `#${tab}`); switchTab(tab);
}));
$("chat-goto-training")?.addEventListener("click", () => switchTab("workspace"));
window.addEventListener("hashchange", () => switchTab(window.location.hash.slice(1)));
window.addEventListener("popstate", () => switchTab(window.location.hash.slice(1)));
$("chat-model-select")?.addEventListener("change", () => {
  if (chatBusy()) return; createNewSession("Nova conversa", $("chat-model-select").value); renderActiveChatMessages(); renderAttachments(); updateChatState();
});
$("chat-with-decensor")?.addEventListener("change", () => {
  if (chatBusy()) { $("chat-with-decensor").checked = Boolean(currentChat()?.withDecensor); return; }
  const enabled = Boolean($("chat-with-decensor").checked);
  const session = currentChat();
  if (session.messages.length) createNewSession("Nova conversa", session.selection, enabled);
  else { session.withDecensor = enabled; saveActiveSession(); }
  updateChatState();
});
$("chat-clear-btn")?.addEventListener("click", resetChat);
$("chat-new-btn")?.addEventListener("click", resetChat);
$("chat-history-search")?.addEventListener("input", renderChatSessionsList);
$("chat-toggle-history-btn")?.addEventListener("click", () => {
  $("chat-history-sidebar")?.classList.toggle("open"); $("chat-toggle-history-btn")?.setAttribute("aria-expanded", String($("chat-history-sidebar").classList.contains("open")));
});
$("chat-clear-all-btn")?.addEventListener("click", () => {
  if (chatBusy() || !window.confirm("Apagar as conversas salvas neste navegador?")) return; chatSessions = []; createNewSession(); renderActiveChatMessages(); renderAttachments(); updateChatState();
});
$("chat-input")?.addEventListener("input", () => {
  currentChat().draft = $("chat-input").value; saveChatSessions(); updateChatState(); $("chat-input").style.height = "auto"; $("chat-input").style.height = `${Math.min($("chat-input").scrollHeight || 80, 220)}px`;
});
$("chat-input")?.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitChat(); } });
$("chat-form")?.addEventListener("submit", event => { event.preventDefault(); submitChat(); });
$("chat-attach-btn")?.addEventListener("click", () => $("chat-file-input").click());
$("chat-file-input")?.addEventListener("change", async event => { await attachChatFiles(Array.from(event.target.files)); event.target.value = ""; });
$("chat-form")?.addEventListener("dragover", event => { event.preventDefault(); $("chat-form").classList.add("is-dragging"); });
$("chat-form")?.addEventListener("dragleave", () => $("chat-form").classList.remove("is-dragging"));
$("chat-form")?.addEventListener("drop", event => { event.preventDefault(); $("chat-form").classList.remove("is-dragging"); attachChatFiles(Array.from(event.dataTransfer.files)); });
$("chat-suggestions")?.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
  $("chat-input").value = button.dataset.prompt; currentChat().draft = button.dataset.prompt; saveChatSessions(); updateChatState(); $("chat-input").focus();
}));
$("chat-stop-btn")?.addEventListener("click", async () => {
  try { await api("/api/stop", {id: currentChat().run, force: false}); chatNotice("Interrupção solicitada. Aguarde o token em andamento."); }
  catch (error) { chatNotice(error.message); }
});
function exportChatText(session) {
  const parts = session.messages.map(message => {
    const visible = message.display_content || message.content;
    const files = (message.attachments || []).map(file => `Anexo: ${file.name}`).join("\n");
    const context = visible !== message.content ? `\nContexto enviado ao modelo:\n${message.content}` : "";
    return `${message.role === "user" ? "Você" : "Qwen"}\n${visible}${files ? `\n${files}` : ""}${context}`;
  });
  if (session.draft || session.attachments.length) {
    parts.push(`Rascunho (não enviado)\n${session.draft}\n${session.attachments.map(file => `Anexo: ${file.name}\n${file.context || file.preview}`).join("\n\n")}`);
  }
  return parts.join("\n\n");
}
$("chat-export-btn")?.addEventListener("click", () => {
  const session = currentChat(), text = exportChatText(session);
  const url = URL.createObjectURL(new Blob([text], {type: "text/plain;charset=utf-8"})); const link = document.createElement("a"); link.href = url; link.download = `formula-lab-${session.id}.txt`; link.click(); URL.revokeObjectURL(url);
});
chatSessions = loadChatSessions();
if (!chatSessions.length) createNewSession(); else if (!currentChat()) activeChatSessionId = chatSessions[0].id;
// Recover an orphaned legacy question as a draft instead of sending two consecutive user messages.
for (const session of chatSessions) if (!session.run && session.messages.at(-1)?.role === "user") { const last = session.messages.pop(); session.draft = last.display_content || last.content; session.attachments = last.attachments || []; }
$("chat-input").value = currentChat().draft;
renderActiveChatMessages(); renderAttachments(); renderChatSessionsList(); updateChatState(); saveChatSessions();
if (typeof initWorkbookUI === "function") initWorkbookUI();
pollChat();
switchTab(window.location.hash.slice(1) || "workspace");
