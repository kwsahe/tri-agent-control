let viewingId = null;
let lastVersion = null;
let sessionsById = {};
let allSessions = [];
let sessionsActiveId = null;

const AGENTS = {
  codex: { label: "Codex", color: "#4f8cff", avatar: "/static/agents/codex.png" },
  antigravity: { label: "Antigravity", color: "#a66cff", avatar: "/static/agents/antigravity.png" },
  claude: { label: "Claude Code", color: "#ff8a3d", avatar: "/static/agents/claude.svg" },
};

const MODE_LABELS = {
  discussion: "토론",
  coding: "코딩",
};

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str || "";
  return d.innerHTML;
}

function formatTokens(value) {
  const n = Number(value || 0);
  return n.toLocaleString();
}

function formatTime(value) {
  return `${Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 1 })}초`;
}

function compactSessionTitle(value) {
  const normalized = String(value || "새 세션").replace(/\s+/g, " ").trim();
  return normalized.length > 56 ? `${normalized.slice(0, 56)}…` : normalized;
}

function estimateCost(tokens) {
  return `$${((Number(tokens || 0) * 0.002) / 1000).toFixed(4)}`;
}

function renderTopic(data) {
  if (!data.topic) return "";
  return `
    <div class="topic">
      <h3>${data.topic} <span style="color:var(--faint);font-weight:500">· ${data.mode_label || "-"}</span></h3>
      <p>활성: ${data.enabled_agents_label || "-"} · 비활성: ${data.disabled_agents_label || "없음"}</p>
      <p>프로젝트 접근: ${data.mode === "coding" ? "읽기·쓰기 (코딩 실행)" : (data.discussion_project_access_label || "프로젝트 읽기")}</p>
      ${data.mode === "coding" ? `<p>현재 진행: ${data.coding_stage === "iteration" ? `목표 사이클 ${data.coding_cycle || 1} / ${data.coding_limits?.max_cycles || 3}` : "계획·승인"}</p>` : ""}
      <p>저장 폴더: ${data.memory_dir || "-"}</p>
    </div>`;
}

function renderSessionList(sessions, activeId) {
  allSessions = sessions;
  sessionsActiveId = activeId;
  sessionsById = Object.fromEntries(sessions.map((session) => [session.id, session]));
  applySessionFilters();
}

function applySessionFilters() {
  const list = $("sessionList");
  const query = ($("sessionSearch")?.value || "").trim().toLowerCase();
  const favoriteOnly = $("favoriteOnly")?.checked || false;
  const showArchived = $("showArchived")?.checked || false;
  const sessions = allSessions.filter((session) => {
    const haystack = `${session.name || ""} ${session.topic || ""} ${(session.tags || []).join(" ")}`.toLowerCase();
    return (!query || haystack.includes(query)) && (!favoriteOnly || session.favorite) && (showArchived ? session.archived : !session.archived);
  });
  if (!sessions.length) {
    list.innerHTML = '<div class="empty-state">아직 세션이 없습니다.</div>';
    return;
  }
  list.innerHTML = sessions.map((s) => {
    const cls = `session-item${s.id === (viewingId || sessionsActiveId) ? " active" : ""}`;
    const status = !s.topic ? "준비" : (s.finished ? "완료" : (s.id === sessionsActiveId ? "진행 중" : "보류"));
    const tags = (s.tags || []).map((tag) => `<span>#${escapeHtml(tag)}</span>`).join("");
    const isCurrent = s.id === (viewingId || sessionsActiveId);
    return `<div class="session-row">
      <button class="${cls}" onclick="viewSession('${s.id}')" aria-current="${isCurrent ? "page" : "false"}" aria-label="${escapeHtml(s.name || s.topic)} · ${status}">
        <span class="s-topic">${s.favorite ? "★ " : ""}${escapeHtml(s.name || s.topic)}</span>
        <span class="s-meta">${status} · ${escapeHtml(s.mode_label)} · ${s.message_count}개</span>
        <span class="s-tags">${tags}</span>
      </button>
      <button class="session-rename icon-button" onclick="openSessionActions('${s.id}')" title="세션 메뉴" aria-label="세션 메뉴">•••</button>
    </div>`;
  }).join("");
}

function updateStatsBoard(tokens, time) {
  $("statTokens").textContent = formatTokens(tokens);
  $("statTime").textContent = formatTime(time);
  $("statCost").textContent = estimateCost(tokens);
}

function buildAgentUsageRows(data) {
  const usage = data.agent_usage || {};
  const contexts = data.context_usage || {};
  const rows = Object.entries(AGENTS).map(([key, info]) => {
    const row = usage[key] || {};
    const context = contexts[key] || {};
    const actualInput = Number(row.input_tokens || 0) + Number(row.cache_creation_input_tokens || 0) + Number(row.cache_read_input_tokens || 0);
    const actual = actualInput + Number(row.output_tokens || 0);
    const estimated = Number(row.estimated_tokens || 0);
    return { key, info, row, context, actual, estimated, tokens: actual > 0 ? actual : estimated };
  });
  const total = rows.reduce((sum, item) => sum + item.tokens, 0);
  rows.forEach((item) => {
    item.percent = total > 0 ? (item.tokens / total) * 100 : 0;
  });
  return { rows, total };
}

function renderAgentUsage(data) {
  const { rows } = buildAgentUsageRows(data);

  $("agentUsage").innerHTML = rows.map(({ info, row, context, actual, tokens, percent }) => {
    const percentText = percent > 0 && percent < 0.1 ? "<0.1%" : `${percent.toFixed(1)}%`;
    const tokenText = actual > 0 ? formatTokens(tokens) : `~${formatTokens(tokens)}`;
    const contextPercent = Number(context.percent || 0);
    const contextTokenText = `${context.estimated ? "~" : ""}${formatTokens(context.used_tokens || 0)} / ${formatTokens(context.limit_tokens || 0)}`;
    return `<div class="agent-usage-row" style="--agent-color:${info.color}">
      <div class="agent-usage-head"><span>${info.label}<small>${row.turns || 0}턴</small></span><strong>점유 ${percentText} · ${tokenText}</strong></div>
      <div class="agent-usage-track" role="progressbar" aria-label="${info.label} 토큰 점유율" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent.toFixed(1)}">
        <span style="width:${Math.min(100, percent).toFixed(2)}%"></span>
      </div>
      <div class="agent-context-meta"><span>현재 컨텍스트</span><strong>${contextPercent.toFixed(1)}% · ${contextTokenText}</strong></div>
      <div class="agent-context-track" role="progressbar" aria-label="${info.label} 현재 컨텍스트 사용률" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${contextPercent.toFixed(1)}">
        <span style="width:${Math.min(100, contextPercent).toFixed(2)}%"></span>
      </div>
    </div>`;
  }).join("");
}

function renderTokenDetails(data) {
  const { rows, total } = buildAgentUsageRows(data);
  $("tokenDetailsSummary").textContent = `세션 집계 ${formatTokens(total)}토큰 · 세 모델 합계 100%`;
  $("tokenDetailsGrid").innerHTML = rows.map(({ info, row, context, actual, estimated, tokens, percent }) => {
    const cacheTokens = Number(row.cache_creation_input_tokens || 0)
      + Number(row.cache_read_input_tokens || 0)
      + Number(row.cached_input_tokens || 0);
    const percentText = percent > 0 && percent < 0.1 ? "<0.1%" : `${percent.toFixed(1)}%`;
    const contextPercent = Number(context.percent || 0);
    const contextTokens = `${context.estimated ? "~" : ""}${formatTokens(context.used_tokens || 0)} / ${formatTokens(context.limit_tokens || 0)}`;
    return `<article class="token-model-card" style="--agent-color:${info.color}">
      <header><img src="${info.avatar}" alt=""><div><strong>${info.label}</strong><span>${context.estimated ? "컨텍스트 추정값" : "최신 실제 컨텍스트"}</span></div></header>
      <div class="token-model-total"><strong>${contextPercent.toFixed(1)}%</strong><span>${contextTokens}토큰</span></div>
      <div class="token-model-track" role="progressbar" aria-label="${info.label} 컨텍스트 사용률" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${contextPercent.toFixed(1)}"><span style="width:${Math.min(100, contextPercent).toFixed(2)}%"></span></div>
      <dl>
        <div><dt>세션 점유율</dt><dd>${percentText}</dd></div>
        <div><dt>세션 집계</dt><dd>${actual > 0 ? formatTokens(tokens) : `~${formatTokens(tokens)}`}</dd></div>
        <div><dt>턴</dt><dd>${formatTokens(row.turns || 0)}</dd></div>
        <div><dt>추정 토큰</dt><dd>~${formatTokens(estimated)}</dd></div>
        <div><dt>입력</dt><dd>${actual > 0 ? formatTokens(row.input_tokens || 0) : "-"}</dd></div>
        <div><dt>캐시</dt><dd>${cacheTokens > 0 ? formatTokens(cacheTokens) : "-"}</dd></div>
        <div><dt>출력</dt><dd>${actual > 0 ? formatTokens(row.output_tokens || 0) : "-"}</dd></div>
        <div><dt>비용</dt><dd>${Number(row.cost_usd || 0) > 0 ? `$${Number(row.cost_usd).toFixed(4)}` : "-"}</dd></div>
      </dl>
    </article>`;
  }).join("");
}

function openTokenDetails() {
  renderTokenDetails(lastState.latestPayload || {});
  $("tokenDetailsDialog").showModal();
}

function closeTokenDetails() { $("tokenDetailsDialog").close(); }

function renderAgentStack(data) {
  const enabled = (data.enabled_agents || []).length
    ? data.enabled_agents
    : Object.keys(AGENTS).filter((key) => (data.enabled_agents_label || "").includes(AGENTS[key].label));
  const active = data.active_agent;
  $("agentStack").innerHTML = Object.entries(AGENTS).map(([key, info]) => {
    const isOn = enabled.includes(key) || (data.enabled_agents_label || "").includes(info.label);
    const cls = `agent-chip${isOn ? "" : " off"}${active === key ? " active" : ""}`;
    const state = active === key ? (data.active_phase || "작업 중") : (isOn ? "대기" : "꺼짐");
    const model = (data.agent_setting_labels || {})[key] || "CLI 기본값";
    const role = (data.role_labels || {})[key] || "미지정";
    return `<div class="${cls}" style="--agent-color:${info.color}">
      <div class="agent-chip-identity"><img src="${info.avatar}" alt=""><div><strong>${info.label}</strong><small>${escapeHtml(model)} · ${escapeHtml(role)}</small></div></div>
      <span>${escapeHtml(state)}</span>
    </div>`;
  }).join("");
}

function renderRoleControls(data, containerId = "roleControls", prefix = "role_") {
  const enabled = new Set(data.enabled_agents || []);
  const roles = data.agent_roles || {};
  const catalog = data.role_catalog || [];
  const options = [
    '<option value="">역할 미지정</option>',
    ...catalog.map((role) => `<option value="${escapeHtml(role.id)}" title="${escapeHtml(role.summary)}">${escapeHtml(role.label)}</option>`),
  ].join("");
  $(containerId).innerHTML = Object.entries(AGENTS).map(([agent, info]) => `
    <label class="role-row ${enabled.has(agent) ? "" : "off"}">
      <span><img src="${info.avatar}" alt=""><strong>${info.label}</strong></span>
      <select id="${prefix}${agent}" aria-label="${info.label} 역할" ${data.active_agent || viewingId ? "disabled" : ""}>
        ${options}
      </select>
    </label>`).join("");
  Object.keys(AGENTS).forEach((agent) => {
    const select = $(`${prefix}${agent}`);
    if (select) select.value = roles[agent] || "";
  });
  $("roleSaveButton").disabled = !!data.active_agent || !!viewingId;
}

function syncAgentModelCards() {
  document.querySelectorAll("[data-agent-card]").forEach((card) => {
    const checkbox = card.querySelector('input[name="agent"]');
    if (!checkbox || checkbox.dataset.bound === "true") return;
    const update = () => {
      card.classList.toggle("off", !checkbox.checked);
      card.querySelectorAll("select").forEach((select) => { select.disabled = !checkbox.checked; });
    };
    checkbox.dataset.bound = "true";
    checkbox.addEventListener("change", update);
    update();
  });
}

function syncTargetControls(data) {
  const enabled = new Set(data.enabled_agents || []);
  ["interventionTarget", "approvalTarget"].forEach((name) => {
    document.querySelectorAll(`input[name="${name}"]`).forEach((input) => {
      const isEnabled = enabled.has(input.value);
      input.disabled = !isEnabled;
      if (!isEnabled) input.checked = false;
    });
  });
}

function renderRuntimeLog(events) {
  const list = events || [];
  if (!list.length) {
    $("runtimeLog").innerHTML = '<div class="runtime-empty">아직 런타임 이벤트가 없습니다.</div>';
    return;
  }
  $("runtimeLog").innerHTML = list.slice(-10).reverse().map((event) => `
    <div class="runtime-item ${escapeHtml(event.level || "info")}">
      <span>${escapeHtml(event.time)}</span>
      <p>${escapeHtml(event.text)}</p>
    </div>
  `).join("");
}

function renderValidationLog(results) {
  if (!results || !results.length) {
    $("validationLog").innerHTML = '<div class="runtime-empty">아직 검증 결과가 없습니다.</div>';
    return;
  }
  $("validationLog").innerHTML = results.map((result) => `
    <details class="runtime-item ${result.ok ? "info" : "error"}">
      <summary>${escapeHtml(result.label)} · ${result.ok ? "통과" : "실패"} · ${formatTime(result.elapsed)}</summary>
      <p>${escapeHtml(result.output || "출력 없음")}</p>
    </details>`).join("");
}

function renderAgentCalls(calls) {
  if (!calls || !calls.length) {
    $("agentCallLog").innerHTML = '<div class="runtime-empty">아직 에이전트 호출이 없습니다.</div>';
    return;
  }
  $("agentCallLog").innerHTML = calls.slice(-10).reverse().map((call) => `
    <div class="runtime-item">
      <span>${escapeHtml(call.time || "")} · ${escapeHtml(MODE_LABELS[call.mode] || call.mode || "토론")}</span>
      <p><strong>${escapeHtml(AGENTS[call.source]?.label || call.source)}</strong> → <strong>${escapeHtml(AGENTS[call.target]?.label || call.target)}</strong><br>${escapeHtml(call.task || "")}</p>
    </div>`).join("");
}

let lastState = {};

function setInterventionLoading(isLoading) {
  const btn = document.querySelector('.composer button[onclick="sendMessage()"]');
  const ta = $("msgText");
  const sel = $("interventionIntent");
  const cbs = document.querySelectorAll('input[name="interventionTarget"]');
  
  if (isLoading) {
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = `<span class="spinner" style="display:inline-block;width:12px;height:12px;border-width:1.5px;--spinner-color:var(--text);margin-right:6px;vertical-align:-1px"></span>전송 중...`;
    }
    if (ta) ta.disabled = true;
    if (sel) sel.disabled = true;
    cbs.forEach(cb => cb.disabled = true);
  } else {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>보내기`;
    }
    if (ta) ta.disabled = false;
    if (sel) sel.disabled = false;
    cbs.forEach(cb => cb.disabled = false);
  }
}

function setApprovalLoading(isLoading) {
  const btn = document.querySelector('.approve-banner button[onclick="sendApprovalMessage()"]');
  const ta = $("approvalMsgText");
  const sel = $("approvalIntent");
  const cbs = document.querySelectorAll('input[name="approvalTarget"]');
  
  if (isLoading) {
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = `<span class="spinner" style="display:inline-block;width:12px;height:12px;border-width:1.5px;--spinner-color:var(--text);margin-right:6px;vertical-align:-1px"></span>전송 중...`;
    }
    if (ta) ta.disabled = true;
    if (sel) sel.disabled = true;
    cbs.forEach(cb => cb.disabled = true);
  } else {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>질문/수정 요청 보내기`;
    }
    if (ta) ta.disabled = false;
    if (sel) sel.disabled = false;
    cbs.forEach(cb => cb.disabled = false);
  }
}

function updateLiveCard(data) {
  const liveCardKey = `${data.active_agent}_${data.active_phase}_${data.active_elapsed}_${data.active_cli_mode}_${data.active_prompt_chars}_${data.intervention_pending}`;
  if (lastState.liveCardKey === liveCardKey) return;
  lastState.liveCardKey = liveCardKey;

  if (!data.active_agent) {
    $("liveCard").classList.remove("active");
    $("liveLine").textContent = "대기 중";
    $("liveDetail").textContent = data.intervention_pending
      ? "사용자 개입을 처리할 준비 중입니다."
      : "모델 호출이 시작되면 여기에 표시됩니다.";
    $("headerAgentName").textContent = "모델 · 대기";
    $("headerPhase").textContent = data.intervention_pending ? "· 개입 대기" : "· 호출 전";
    $("headerAgent").dataset.agent = "";
    return;
  }
  const agent = AGENTS[data.active_agent] || { label: data.active_agent, color: "#4f8cff" };
  $("liveCard").classList.add("active");
  $("liveCard").style.setProperty("--live-color", agent.color);
  $("liveLine").textContent = `${agent.label} · ${data.active_phase || "생각 중"}`;
  $("liveDetail").textContent = `${formatTime(data.active_elapsed)} · ${data.active_cli_mode || "-"} · 입력 ${formatTokens(data.active_prompt_chars)}자`;
  $("headerAgentName").textContent = `모델 · ${agent.label}`;
  $("headerPhase").textContent = `· ${data.active_phase || "작업 중"}`;
  $("headerAgent").dataset.agent = data.active_agent;
  $("headerAgent").style.setProperty("--agent-color", agent.color);
}

function updateInspector(data) {
  lastState.latestPayload = data;
  lastState.active_id = data.active_id || data.id || lastState.active_id;
  if (lastState.sessionName !== data.session_name) {
    const fullSessionTitle = data.session_name || "새 세션";
    $("sessionTitle").textContent = compactSessionTitle(fullSessionTitle);
    $("sessionTitle").title = fullSessionTitle.replace(/\s+/g, " ").trim();
    lastState.sessionName = data.session_name;
  }
  if (lastState.sideStatus !== data.status) {
    $("sideStatus").textContent = data.status || "-";
    lastState.sideStatus = data.status;
  }
  if (lastState.sideMode !== data.mode_label) {
    $("sideMode").textContent = data.mode_label || "-";
    $("headerMode").textContent = `모드 · ${data.mode_label || "준비"}`;
    $("headerMode").dataset.mode = data.mode || "discussion";
    lastState.sideMode = data.mode_label;
  }
  $("sideCycleRow").style.display = data.mode === "coding" ? "flex" : "none";
  $("sideCycle").textContent = data.coding_stage === "iteration"
    ? `목표 사이클 ${data.coding_cycle || 1} / ${data.coding_limits?.max_cycles || 3}`
    : "계획·승인";
  $("sideCodingLimitRow").style.display = data.mode === "coding" ? "flex" : "none";
  $("sideCodingLimit").textContent = `${data.coding_limits?.max_cycles || 3}회 · ${data.coding_limits?.max_minutes || 45}분 · ${formatTokens(data.coding_limits?.max_tokens || 200000)} 토큰`;
  $("sideCodingStopRow").style.display = data.coding_stop_reason ? "flex" : "none";
  $("sideCodingStop").textContent = data.coding_stop_reason || "-";
  if ($("sessionModeSelect") && data.mode && $("sessionModeSelect").value !== data.mode) {
    $("sessionModeSelect").value = data.mode;
  }
  if ($("composerModeSelect") && data.mode && $("composerModeSelect").value !== data.mode) {
    $("composerModeSelect").value = data.mode;
  }
  ["discussionAccessSelect", "composerDiscussionAccess"].forEach((id) => {
    const select = $(id);
    if (select && document.activeElement !== select) {
      select.value = data.discussion_project_access || "read";
      select.disabled = data.mode === "coding" || !!data.active_agent || !!viewingId;
    }
  });
  ["discussionAccessButton", "composerDiscussionAccessButton"].forEach((id) => {
    const button = $(id);
    if (button) button.disabled = data.mode === "coding" || !!data.active_agent || !!viewingId;
  });
  if (lastState.sideMessages !== data.message_count) {
    $("sideMessages").textContent = data.message_count || 0;
    lastState.sideMessages = data.message_count;
  }
  if (lastState.memory_dir !== data.memory_dir) {
    $("memoryPath").textContent = data.memory_dir || "세션 시작 후 표시됩니다.";
    lastState.memory_dir = data.memory_dir;
  }
  if (lastState.workspacePath !== data.workspace_path) {
    $("workspacePath").textContent = data.workspace_path || "작업 폴더가 선택되지 않았습니다.";
    $("workspacePath").title = data.workspace_path || "";
    lastState.workspacePath = data.workspace_path;
  }
  if (document.activeElement !== $("workspaceAccess") && data.workspace_access) {
    $("workspaceAccess").value = data.workspace_access;
  }
  $("workspaceAccess").disabled = data.mode === "coding" || !!data.active_agent || !!viewingId;
  $("workspaceAccessButton").disabled = data.mode === "coding" || !!data.active_agent || !!viewingId;
  const currentProfilePath = data.profile_path ? `Profile: ${data.profile_path}` : "Profile.md도 여기에 표시됩니다.";
  if (lastState.profilePath !== currentProfilePath) {
    $("profilePath").textContent = currentProfilePath;
    lastState.profilePath = currentProfilePath;
  }
  const currentRolesPath = data.roles_path ? `Roles: ${data.roles_path}` : "Roles.md도 여기에 표시됩니다.";
  if (lastState.rolesPath !== currentRolesPath) {
    $("rolesPath").textContent = currentRolesPath;
    lastState.rolesPath = currentRolesPath;
  }

  if (lastState.total_est_tokens !== data.total_est_tokens || lastState.total_elapsed_time !== data.total_elapsed_time) {
    updateStatsBoard(data.total_est_tokens, data.total_elapsed_time);
    lastState.total_est_tokens = data.total_est_tokens;
    lastState.total_elapsed_time = data.total_elapsed_time;
  }
  if (Number(data.total_actual_tokens || 0) > 0) {
    $("statTokens").textContent = `${formatTokens(data.total_actual_tokens)} 실측`;
    $("statCost").textContent = `$${Number(data.total_actual_cost_usd || 0).toFixed(4)}`;
  }
  $("budgetWarning").textContent = data.budget_exceeded || "";
  $("budgetWarning").style.display = data.budget_exceeded ? "block" : "none";
  $("retryBtn").style.display = data.can_retry ? "inline-flex" : "none";
  $("continueNextBtn").style.display = data.can_continue_next ? "inline-flex" : "none";
  const usageKey = JSON.stringify([data.agent_usage || {}, data.context_usage || {}]);
  if (lastState.agentUsageKey !== usageKey) {
    renderAgentUsage(data);
    if ($("tokenDetailsDialog")?.open) renderTokenDetails(data);
    lastState.agentUsageKey = usageKey;
  }

  // renderAgentStack 캐싱
  const enabled = (data.enabled_agents || []).length
    ? data.enabled_agents
    : Object.keys(AGENTS).filter((key) => (data.enabled_agents_label || "").includes(AGENTS[key].label));
  const active = data.active_agent;
  const agentStackKey = `${enabled.join(",")}_${active}_${data.active_phase}_${JSON.stringify(data.agent_setting_labels || {})}_${JSON.stringify(data.role_labels || {})}`;
  if (lastState.agentStackKey !== agentStackKey) {
    renderAgentStack(data);
    lastState.agentStackKey = agentStackKey;
  }
  const roleControlsKey = JSON.stringify([data.agent_roles || {}, data.role_catalog || [], enabled, active, viewingId]);
  if (lastState.roleControlsKey !== roleControlsKey) {
    renderRoleControls(data);
    lastState.roleControlsKey = roleControlsKey;
  }

  updateLiveCard(data);

  // runtimeLog 캐싱
  const eventsLength = (data.runtime_events || []).length;
  const lastEventText = eventsLength ? data.runtime_events[eventsLength - 1].text : "";
  const runtimeLogKey = `${eventsLength}_${lastEventText}`;
  if (lastState.runtimeLogKey !== runtimeLogKey) {
    renderRuntimeLog(data.runtime_events);
    lastState.runtimeLogKey = runtimeLogKey;
  }
  const validationKey = JSON.stringify(data.validation_results || []);
  if (lastState.validationKey !== validationKey) {
    renderValidationLog(data.validation_results || []);
    lastState.validationKey = validationKey;
  }
  const delegationKey = JSON.stringify(data.delegation_history || []);
  if (lastState.delegationKey !== delegationKey) {
    renderAgentCalls(data.delegation_history || []);
    lastState.delegationKey = delegationKey;
  }
}

function updateThinking(data) {
  const workLog = data.active_work_log || [];
  const lastWork = workLog.length ? `${workLog[workLog.length - 1].time}_${workLog[workLog.length - 1].text}` : "";
  const thinkingKey = `${data.active_agent}_${data.active_phase}_${data.active_elapsed}_${data.active_cli_mode}_${data.active_prompt_chars}_${workLog.length}_${lastWork}`;
  if (lastState.thinkingKey === thinkingKey && $("thinkingIndicator")) return;
  lastState.thinkingKey = thinkingKey;

  if (!data.active_agent) {
    $("thinkingIndicator")?.remove();
    return;
  }
  const agent = AGENTS[data.active_agent] || { label: data.active_agent, color: "#4f8cff" };
  const feed = $("feed");
  const scroller = $("conversationScroll");
  const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  let box = $("thinkingIndicator");
  if (!box) {
    box = document.createElement("div");
    box.id = "thinkingIndicator";
    box.className = "row live-thinking-row is-new";
    feed.appendChild(box);
  }
  box.innerHTML = `
    <div class="bubble" style="--accent:${agent.color}">
      <div class="meta">
        <img class="avatar" src="${agent.avatar || ""}" alt="">
        <span class="name" style="color:${agent.color}">${escapeHtml(agent.label)}</span>
        <span class="phase">${escapeHtml(data.active_phase || "실행 및 추론 중")}</span>
        <span class="time">${formatTime(data.active_elapsed)}</span>
      </div>
      <div class="live-thinking-summary">
        <span class="spinner" style="--spinner-color:${agent.color}"></span>
        <div><strong>실행 및 추론 중</strong><span>${escapeHtml(data.active_cli_mode || "-")} · 입력 ${formatTokens(data.active_prompt_chars)}자</span></div>
      </div>
      <ol class="active-work-log">${workLog.map((event) => {
        const paths = (event.paths || []).map((path) => `<code>${escapeHtml(path)}</code>`).join("");
        return `<li class="work-${escapeHtml(event.kind || "log")}"><span>${escapeHtml(event.time || "")}</span><p>${escapeHtml(event.text || "")}</p>${paths}</li>`;
      }).join("")}</ol>
    </div>
  `;
  if (distanceFromBottom < 160) {
    requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
  }
}

function updateFeedHtml(feedHtml) {
  const feed = $("feed");
  const scroller = $("conversationScroll");
  const previousRows = feed.querySelectorAll(".row").length;
  const previousTop = scroller.scrollTop;
  const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  const followLatest = previousRows === 0 || distanceFromBottom < 120;

  feed.innerHTML = feedHtml;
  const rows = feed.querySelectorAll(".row");
  if (previousRows > 0 && rows.length > previousRows) {
    for (let index = previousRows; index < rows.length; index += 1) {
      rows[index].classList.add("is-new");
    }
  }

  requestAnimationFrame(() => {
    scroller.scrollTop = followLatest ? scroller.scrollHeight : previousTop;
  });
}

function updateConnectionSummary() {
  const items = Array.from(document.querySelectorAll("#conn .conn-item"));
  const connected = items.filter((item) => item.textContent.trim().startsWith("✅")).length;
  const failed = items.filter((item) => item.textContent.trim().startsWith("❌")).length;
  const target = $("headerConnection");
  if (!items.length || (!connected && !failed)) {
    target.textContent = "연결 · 확인 중";
    target.dataset.connection = "pending";
    return;
  }
  target.textContent = failed
    ? `연결 · ${connected}/${items.length} 정상`
    : `연결 · 모두 정상 (${connected})`;
  target.dataset.connection = failed ? "error" : "ok";
}

function renderUserQuestion(question) {
  const banner = $("userQuestionBanner");
  if (!question) {
    banner.style.display = "none";
    return;
  }

  const source = AGENTS[question.source_agent]?.label || question.source_agent || "에이전트";
  $("userQuestionSource").textContent = `${source}${question.source_phase ? ` · ${question.source_phase}` : ""}`;
  $("userQuestionReason").textContent = `중단 이유: ${question.reason}`;
  $("userQuestionText").textContent = question.question;

  const options = $("userQuestionOptions");
  options.replaceChildren();
  (question.options || []).forEach((option) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    const content = document.createElement("span");
    const title = document.createElement("strong");
    const risk = document.createElement("small");
    input.type = "radio";
    input.name = "userQuestionOption";
    input.value = option.id;
    input.checked = option.id === question.recommended_option;
    title.textContent = `${option.id}. ${option.label}`;
    risk.textContent = option.risk || "별도 위험 정보 없음";
    content.append(title, risk);
    label.append(input, content);
    options.append(label);
  });
  banner.style.display = "grid";
}

async function submitQuestionAnswer() {
  const selected = document.querySelector('input[name="userQuestionOption"]:checked');
  const params = new URLSearchParams({
    option_id: selected?.value || "",
    answer: $("userQuestionAnswer").value.trim(),
  });
  const button = $("userQuestionSubmit");
  button.disabled = true;
  try {
    const response = await fetch("/answer-question", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: params.toString(),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "답변을 처리하지 못했습니다.");
    $("userQuestionAnswer").value = "";
    await poll(true);
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
}

function applyState(data) {
  if (lastState.feed_html !== data.feed_html) {
    updateFeedHtml(data.feed_html);
    lastState.feed_html = data.feed_html;
  }
  if (lastState.status !== data.status) {
    $("status").textContent = `상태 · ${data.status || "준비"}`;
    $("status").dataset.status = data.status || "준비";
    lastState.status = data.status;
  }
  if (lastState.conn_html !== data.conn_html) {
    $("conn").innerHTML = `<div class="conn-list">${data.conn_html}</div><button class="ghost compact" onclick="recheck()"><svg viewBox="0 0 24 24" style="margin-right:4px"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>다시 확인</button>`;
    updateConnectionSummary();
    lastState.conn_html = data.conn_html;
  }
  
  const currentTopicHtml = data.topic_section_html || renderTopic(data);
  if (lastState.topic_html !== currentTopicHtml) {
    $("topicSection").innerHTML = currentTopicHtml;
    syncAgentModelCards();
    lastState.topic_html = currentTopicHtml;
  }

  const hasTopic = !!data.topic;
  const pauseDisabled = !hasTopic || data.finished || data.stopped || data.paused;
  const waitingForAnswer = data.workflow_status === "WAITING_FOR_USER_RESPONSE";
  const resumeDisabled = !hasTopic || data.finished || data.stopped || !data.paused || waitingForAnswer;
  const stopDisabled = !hasTopic || data.finished || data.stopped;

  if (lastState.pauseDisabled !== pauseDisabled) {
    $("pauseBtn").disabled = pauseDisabled;
    lastState.pauseDisabled = pauseDisabled;
  }
  if (lastState.resumeDisabled !== resumeDisabled) {
    $("resumeBtn").disabled = resumeDisabled;
    lastState.resumeDisabled = resumeDisabled;
  }
  if (lastState.stopDisabled !== stopDisabled) {
    $("stopBtn").disabled = stopDisabled;
    lastState.stopDisabled = stopDisabled;
  }

  const questionKey = JSON.stringify(data.pending_user_question || null);
  if (lastState.questionKey !== questionKey) {
    renderUserQuestion(data.pending_user_question);
    lastState.questionKey = questionKey;
  }

  const showApprove = (data.awaiting_approval && !data.stopped && !waitingForAnswer) ? "block" : "none";
  if (lastState.showApprove !== showApprove) {
    $("approveBanner").style.display = showApprove;
    lastState.showApprove = showApprove;
  }

  const approvalRequesters = (data.approval_requested_by || []).join(", ");
  const requesterLabel = approvalRequesters || "에이전트";
  const approveHintText = data.approval_deferred
    ? `${requesterLabel}의 요청을 보류 중입니다. 질문이나 수정 요청을 보낼 수 있습니다.`
    : `${requesterLabel}가 진행 승인을 요청했습니다. 승인 여부를 결정해주세요.`;
  if (lastState.approveHintText !== approveHintText) {
    $("approveHint").textContent = approveHintText;
    lastState.approveHintText = approveHintText;
  }

  updateInspector(data);
  updateThinking(data);
  syncTargetControls(data);
  const notice = lastState.notificationSnapshot;
  if (notice) {
    if (!notice.awaiting && data.awaiting_approval) notifyTransition("승인 대기", `${data.approval_requested_by?.join(", ") || "에이전트"}의 승인이 필요합니다.`);
    if (!notice.awaitingQuestion && waitingForAnswer) notifyTransition("사용자 답변 필요", data.pending_user_question?.question || "작업 진행에 사용자 판단이 필요합니다.");
    if (!notice.finished && data.finished) notifyTransition("세션 완료", data.session_name || "작업이 완료되었습니다.");
    if (!notice.budget && data.budget_exceeded) notifyTransition("예산 한도 도달", data.budget_exceeded);
    const latestEvent = (data.runtime_events || []).at(-1);
    if (latestEvent?.level === "error" && latestEvent.text !== notice.errorText) notifyTransition("작업 실패", latestEvent.text);
  }
  const latestError = [...(data.runtime_events || [])].reverse().find((event) => event.level === "error");
  lastState.notificationSnapshot = {
    awaiting: !!data.awaiting_approval,
    awaitingQuestion: waitingForAnswer,
    finished: !!data.finished,
    budget: !!data.budget_exceeded,
    errorText: latestError?.text || "",
  };
}

async function loadSessions() {
  try {
    const r = await fetch("/sessions.json");
    const data = await r.json();
    renderSessionList(data.sessions, data.active_id);
  } catch (_e) {}
}

async function renameSession(id) {
  const session = sessionsById[id] || {};
  const currentName = session.name || session.topic || "새 세션";
  const name = prompt("세션 이름", currentName);
  if (name === null || !name.trim()) return;
  const response = await fetch("/session/name", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ id, name: name.trim() }).toString(),
  });
  const data = await response.json();
  if (data.error) {
    alert(data.error);
    return;
  }
  lastVersion = null;
  await loadSessions();
  if (id === (viewingId || (lastState.active_id || ""))) await poll(true);
}

async function renameCurrentSession() {
  const id = viewingId || lastState.active_id;
  if (!id) return;
  await renameSession(id);
}

function openSessionActions(id) {
  const session = sessionsById[id];
  if (!session) return;
  $("sessionActionId").value = id;
  $("sessionActionTitle").textContent = session.name || session.topic || "세션 관리";
  $("sessionTagsInput").value = (session.tags || []).join(", ");
  $("sessionFavoriteInput").checked = !!session.favorite;
  $("sessionArchivedInput").checked = !!session.archived;
  $("activateSessionButton").disabled = id === sessionsActiveId;
  $("activateSessionButton").textContent = id === sessionsActiveId ? "현재 활성" : "활성화";
  $("sessionActionsDialog").showModal();
}

function closeSessionActions() { $("sessionActionsDialog").close(); }

async function saveSessionMeta() {
  const params = new URLSearchParams({
    id: $("sessionActionId").value,
    tags: $("sessionTagsInput").value,
    favorite: String($("sessionFavoriteInput").checked),
    archived: String($("sessionArchivedInput").checked),
  });
  const response = await fetch("/session/meta", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: params.toString() });
  const data = await response.json();
  if (data.error) alert(data.error); else { closeSessionActions(); await loadSessions(); }
}

async function renameManagedSession() {
  const id = $("sessionActionId").value;
  closeSessionActions();
  await renameSession(id);
}

async function cloneManagedSession() {
  const id = $("sessionActionId").value;
  if (!confirm("이 세션의 대화와 설정을 새 분기로 복제할까요?")) return;
  const response = await fetch("/session/clone", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ id }).toString() });
  const data = await response.json();
  if (data.error) alert(data.error); else location.reload();
}

async function activateSession(id) {
  if (!id || id === sessionsActiveId) return;
  const session = sessionsById[id] || {};
  const label = session.name || session.topic || "선택한 세션";
  if (!confirm(`"${label}" 세션을 활성화할까요?\nCLI는 자동 실행되지 않습니다.`)) return;
  const response = await fetch("/session/activate", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ id }).toString(),
  });
  const data = await response.json();
  if (data.error) {
    alert(data.error);
    return;
  }
  viewingId = null;
  lastVersion = null;
  closeSessionActions();
  location.reload();
}

async function activateManagedSession() {
  await activateSession($("sessionActionId").value);
}

async function activateViewedSession() {
  await activateSession(viewingId);
}

async function deleteManagedSession() {
  const id = $("sessionActionId").value;
  if (!confirm("이 세션과 저장된 메모리를 삭제할까요?")) return;
  const response = await fetch("/session/delete", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ id }).toString() });
  const data = await response.json();
  if (data.error) alert(data.error); else { closeSessionActions(); viewingId = null; await loadSessions(); await poll(true); }
}

async function openPromptPreview() {
  $("promptPreviewDialog").showModal();
  await loadPromptPreview();
}

function closePromptPreview() { $("promptPreviewDialog").close(); }

async function loadPromptPreview() {
  const response = await fetch(`/prompt-preview.json?agent=${encodeURIComponent($("previewAgent").value)}`);
  const data = await response.json();
  if (data.error) { $("promptPreviewText").textContent = data.error; return; }
  $("previewStats").textContent = `${data.phase} · ${formatTokens(data.characters)}자 · 추정 ${formatTokens(data.estimated_tokens)}토큰 · 공유 ${data.shared_context.join(", ") || "없음"}`;
  $("promptPreviewText").textContent = data.prompt;
}

async function enableNotifications() {
  if (!("Notification" in window)) { alert("이 브라우저는 알림을 지원하지 않습니다."); return; }
  const permission = await Notification.requestPermission();
  if (permission === "granted") new Notification("TriAgent Control", { body: "알림을 활성화했습니다." });
}

function notifyTransition(title, body) {
  if ("Notification" in window && Notification.permission === "granted") new Notification(title, { body });
}

async function viewSession(id) {
  const r = await fetch(`/session.json?id=${encodeURIComponent(id)}`);
  if (!r.ok) return;
  const data = await r.json();
  if (data.is_active) {
    viewingId = null;
    $("viewingBanner").style.display = "none";
    $("controlPanel").style.display = "flex";
    await poll(true);
  } else {
    viewingId = id;
    $("feed").innerHTML = data.feed_html;
    $("topicSection").innerHTML = data.topic_section_html || renderTopic(data);
    $("viewingBanner").style.display = "flex";
    $("controlPanel").style.display = "none";
    $("approveBanner").style.display = "none";
    $("thinkingIndicator")?.remove();
    $("conversationScroll").scrollTop = 0;
    lastState.feed_html = null;
    lastState.topic_html = null;
    updateInspector(data);
  }
  await loadSessions();
}

function backToLive() {
  viewingId = null;
  lastState.feed_html = null;
  lastState.topic_html = null;
  $("viewingBanner").style.display = "none";
  $("controlPanel").style.display = "flex";
  poll(true);
  loadSessions();
}

async function poll(force = false) {
  if (viewingId) return;
  try {
    if (!force) {
      const vr = await fetch("/version.json");
      const vdata = await vr.json();
      if (vdata.version === lastVersion) return;
      lastVersion = vdata.version;
    }
    const r = await fetch("/state.json");
    const data = await r.json();
    applyState(data);
  } catch (_e) {}
}

async function approveWork() {
  const btn = document.querySelector('.approve-banner button[onclick="approveWork()"]');
  let originalText = "";
  if (btn) {
    btn.disabled = true;
    originalText = btn.innerHTML;
    btn.innerHTML = `<span class="spinner" style="display:inline-block;width:12px;height:12px;border-width:1.5px;--spinner-color:var(--text);margin-right:6px;vertical-align:-1px"></span>승인 중...`;
  }
  try {
    const response = await fetch("/approve", { method: "POST" });
    if (!response.ok) throw new Error("서버 에러");
  } catch (err) {
    alert("승인 처리에 실패했습니다: " + err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalText;
    }
    await poll(true);
  }
}

async function deferApproval() {
  await fetch("/defer", { method: "POST" });
  await poll(true);
}

async function rejectApproval() {
  try {
    const response = await fetch("/reject", { method: "POST" });
    if (!response.ok) throw new Error("서버 에러");
  } catch (err) {
    alert("승인 거절 처리에 실패했습니다: " + err.message);
  } finally {
    await poll(true);
  }
}

function syncDirectAgentButtons() {
  const enabled = new Set(
    Array.from(document.querySelectorAll('input[name="agent"]:checked')).map((el) => el.value)
  );
  document.querySelectorAll("[data-direct-agent]").forEach((button) => {
    const available = enabled.has(button.dataset.directAgent);
    button.disabled = !available;
    button.setAttribute("aria-disabled", String(!available));
  });
}

async function submitTopic(startAgent = "") {
  const topic = $("topicInput").value.trim();
  if (!topic) return;
  const modeEl = document.querySelector('input[name="mode"]:checked');
  const mode = modeEl ? modeEl.value : "discussion";
  const discussionAccessEl = document.querySelector('input[name="discussion_project_access"]:checked');
  const discussionProjectAccess = discussionAccessEl ? discussionAccessEl.value : "read";
  const agents = Array.from(document.querySelectorAll('input[name="agent"]:checked')).map((el) => el.value);
  if (!agents.length) {
    alert("최소 한 명의 에이전트를 선택해주세요.");
    return;
  }
  if (!agents.includes(startAgent)) {
    alert("직접 호출할 Agent를 활성화한 뒤 다시 시도해주세요.");
    return;
  }
  const params = new URLSearchParams({ topic, mode, discussion_project_access: discussionProjectAccess });
  params.set("start_agent", startAgent);
  agents.forEach((agent) => params.append("agent", agent));
  Object.keys(AGENTS).forEach((agent) => {
    const model = document.querySelector(`[name="model_${agent}"]`);
    const effort = document.querySelector(`[name="effort_${agent}"]`);
    if (model) params.set(`model_${agent}`, model.value);
    if (effort) params.set(`effort_${agent}`, effort.value);
  });
  await fetch("/topic", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: params.toString(),
  });
  location.reload();
}

async function recheck() {
  $("conn").innerHTML = '<span class="conn-item">확인 중...</span>';
  await fetch("/preflight", { method: "POST" });
  await poll(true);
}

async function ctrl(action) {
  await fetch(`/${action}`, { method: "POST" });
  await poll(true);
}

async function changeSessionMode(source = "side") {
  if (viewingId) {
    alert("지난 세션은 읽기 전용입니다. 현재 세션으로 돌아간 뒤 모드를 전환해주세요.");
    return;
  }
  const mode = source === "composer" ? $("composerModeSelect").value : $("sessionModeSelect").value;
  const response = await fetch("/mode", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `mode=${encodeURIComponent(mode)}`,
  });
  const data = await response.json();
  if (data.error) {
    alert(data.error);
    return;
  }
  applyState(data);
  await loadSessions();
}

async function changeDiscussionAccess(source = "side") {
  if (viewingId) {
    alert("지난 세션은 읽기 전용입니다. 현재 세션으로 돌아가 변경해주세요.");
    return;
  }
  const select = source === "composer" ? $("composerDiscussionAccess") : $("discussionAccessSelect");
  const response = await fetch("/discussion-access", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `access=${encodeURIComponent(select.value)}`,
  });
  const data = await response.json();
  if (data.error) {
    alert(data.error);
    return;
  }
  applyState(data);
}

async function openProfileEditor() {
  const id = viewingId ? `?id=${encodeURIComponent(viewingId)}` : "";
  const response = await fetch(`/profile.json${id}`);
  if (!response.ok) {
    alert("Profile.md를 열 수 없습니다.");
    return;
  }
  const data = await response.json();
  $("profileContent").value = data.content || "";
  $("profileEditor").style.display = "block";
}

function closeProfileEditor() {
  $("profileEditor").style.display = "none";
}

async function saveProfileEditor() {
  const id = viewingId ? `&id=${encodeURIComponent(viewingId)}` : "";
  const content = $("profileContent").value;
  const response = await fetch("/profile", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `content=${encodeURIComponent(content)}${id}`,
  });
  const data = await response.json();
  if (data.error) {
    alert(data.error);
    return;
  }
  $("profilePath").textContent = `Profile: ${data.path}`;
  alert("Profile.md를 저장했습니다.");
}

async function stopSession() {
  if (!confirm("중단할까요? 현재 실행 중인 CLI도 즉시 종료됩니다.")) return;
  await fetch("/stop", { method: "POST" });
  await poll(true);
}

async function retryFailedTurn() {
  const response = await fetch("/retry", { method: "POST" });
  const data = await response.json();
  if (data.error) alert(data.error);
  else applyState(data);
}

async function continueNextAgent() {
  if (!confirm("현재 변경을 유지하고 실패한 턴을 건너뛸까요?")) return;
  const response = await fetch("/continue-next", { method: "POST" });
  const data = await response.json();
  if (data.error) alert(data.error);
  else applyState(data);
}

async function updateWorkspace(endpoint) {
  const button = endpoint === "/workspace/select" ? $("workspacePickerButton") : $("workspaceAccessButton");
  const original = button.innerHTML;
  button.disabled = true;
  button.textContent = endpoint === "/workspace/select" ? "폴더 선택 창 대기 중..." : "권한 적용 중...";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ access: $("workspaceAccess").value }).toString(),
    });
    const data = await response.json();
    if (data.error) {
      alert(data.error);
      return;
    }
    applyState(data);
  } catch (err) {
    alert(`작업 폴더 설정 실패: ${err.message}`);
  } finally {
    button.disabled = false;
    button.innerHTML = original;
  }
}

async function selectWorkspaceFolder() {
  await updateWorkspace("/workspace/select");
}

async function applyWorkspaceAccess() {
  await updateWorkspace("/workspace/access");
}

function openRoleDialog() {
  const data = lastState.latestPayload || {};
  renderRoleControls(data, "roleDialogControls", "dialog_role_");
  $("roleDialogSaveButton").disabled = !!data.active_agent || !!viewingId;
  $("roleDialog").showModal();
}

function closeRoleDialog() {
  $("roleDialog").close();
}

async function saveAgentRoles(source = "card") {
  const prefix = source === "dialog" ? "dialog_role_" : "role_";
  const params = new URLSearchParams();
  Object.keys(AGENTS).forEach((agent) => params.set(`role_${agent}`, $(`${prefix}${agent}`).value));
  const button = source === "dialog" ? $("roleDialogSaveButton") : $("roleSaveButton");
  button.disabled = true;
  try {
    const response = await fetch("/roles", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: params.toString(),
    });
    const data = await response.json();
    if (data.error) {
      alert(data.error);
      return;
    }
    applyState(data);
    if (source === "dialog") closeRoleDialog();
  } finally {
    button.disabled = false;
  }
}

async function saveBudget() {
  const params = new URLSearchParams({
    token_limit: $("budgetTokenLimit").value || "0",
    cost_limit_usd: $("budgetCostLimit").value || "0",
  });
  const response = await fetch("/budget", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: params.toString(),
  });
  const data = await response.json();
  if (data.error) alert(data.error);
  else applyState(data);
}

async function rollbackSelectedFiles(button) {
  const review = button.closest("[data-checkpoint]");
  const paths = Array.from(review.querySelectorAll('input[type="checkbox"]:checked')).map((input) => input.value);
  if (!paths.length) {
    alert("되돌릴 파일을 선택해주세요.");
    return;
  }
  if (!confirm(`${paths.length}개 파일에서 이 턴의 변경을 되돌릴까요?`)) return;
  const params = new URLSearchParams({ checkpoint: review.dataset.checkpoint });
  paths.forEach((path) => params.append("path", path));
  const response = await fetch("/checkpoint/rollback", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: params.toString(),
  });
  const data = await response.json();
  if (data.error) alert(data.error);
  else {
    alert(`${data.paths.length}개 파일의 변경을 되돌렸습니다.`);
    await poll(true);
  }
}

async function restartSession() {
  if (!confirm("현재 세션 기록을 지우고 새로 시작할까요?")) return;
  await fetch("/restart", { method: "POST" });
  location.reload();
}

async function sendMessage() {
  const ta = $("msgText");
  const text = ta.value.trim();
  const intent = $("interventionIntent").value;
  const targets = Array.from(document.querySelectorAll('input[name="interventionTarget"]:checked')).map((el) => el.value);
  if (!text) return;
  if (intent !== "note" && !targets.length) {
    alert("답변할 대상 모델을 하나 이상 선택해주세요.");
    return;
  }
  
  setInterventionLoading(true);
  try {
    const targetBody = targets.map((target) => `&target=${encodeURIComponent(target)}`).join("");
    const response = await fetch("/message", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `text=${encodeURIComponent(text)}&intent=${encodeURIComponent(intent)}&source=composer${targetBody}`,
    });
    if (!response.ok) throw new Error("서버 연결 실패");
    const resData = await response.json();
    if (!resData || resData.error) {
      throw new Error(resData.error || "알 수 없는 응답 오류");
    }
    ta.value = "";
  } catch (err) {
    alert("메시지 전송 실패: " + err.message);
  } finally {
    setInterventionLoading(false);
    await poll(true);
  }
}

async function sendApprovalMessage() {
  const ta = $("approvalMsgText");
  const text = ta.value.trim();
  const intent = $("approvalIntent").value;
  const targets = Array.from(document.querySelectorAll('input[name="approvalTarget"]:checked')).map((el) => el.value);
  if (!text) return;
  if (!targets.length) {
    alert("답변할 대상 모델을 하나 이상 선택해주세요.");
    return;
  }
  
  setApprovalLoading(true);
  try {
    await fetch("/defer", { method: "POST" });
    const targetBody = targets.map((target) => `&target=${encodeURIComponent(target)}`).join("");
    const response = await fetch("/message", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `text=${encodeURIComponent(text)}&intent=${encodeURIComponent(intent)}&source=approval${targetBody}`,
    });
    if (!response.ok) throw new Error("서버 연결 실패");
    const resData = await response.json();
    if (!resData || resData.error) {
      throw new Error(resData.error || "알 수 없는 응답 오류");
    }
    ta.value = "";
  } catch (err) {
    alert("메시지 전송 실패: " + err.message);
  } finally {
    setApprovalLoading(false);
    await poll(true);
  }
}

setInterval(() => poll(false), 1800);
setInterval(loadSessions, 4000);
poll(true);
loadSessions();
