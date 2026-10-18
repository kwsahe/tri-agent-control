// ──────────────────────────────────────────────────────────────
// CSRF / 네트워크 계층
// ──────────────────────────────────────────────────────────────
// 상태를 바꾸는 요청에는 서버가 발급한 토큰이 있어야 한다. 호출 지점이 30곳이 넘어
// 하나씩 고치면 새로 추가되는 호출을 빠뜨리기 쉬우므로, fetch를 한 번 감싸서
// 같은 오리진의 비-GET 요청에 자동으로 토큰을 붙인다.
// 서버를 다시 켜면 토큰이 바뀐다. 상태 응답마다 오는 csrf_token으로 갱신하고,
// 그래도 403이 나면 새 토큰을 받아 한 번만 다시 보낸다.
let csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

const nativeFetch = window.fetch.bind(window);

async function refreshCsrfToken() {
  try {
    const data = await (await nativeFetch("/state.json")).json();
    if (data.csrf_token && data.csrf_token !== csrfToken) {
      csrfToken = data.csrf_token;
      return true;
    }
  } catch (_e) { /* 서버가 아직 안 떴으면 원래 403을 그대로 돌려준다 */ }
  return false;
}

window.fetch = async function guardedFetch(input, init) {
  const options = { ...(init || {}) };
  const method = (options.method || (typeof input === "object" ? input.method : "GET") || "GET").toUpperCase();
  const url = typeof input === "string" ? input : (input && input.url) || "";
  const sameOrigin = url.startsWith("/") || url.startsWith(window.location.origin);
  if (method === "GET" || method === "HEAD" || !sameOrigin || !csrfToken) return nativeFetch(input, options);
  const headers = new Headers(options.headers || (typeof input === "object" ? input.headers : undefined));
  const callerToken = headers.has("X-CSRF-Token");
  if (!callerToken) headers.set("X-CSRF-Token", csrfToken);
  options.headers = headers;
  options.credentials = options.credentials || "same-origin";
  const response = await nativeFetch(input, options);
  if (response.status !== 403 || callerToken || !(await refreshCsrfToken())) return response;
  headers.set("X-CSRF-Token", csrfToken);
  return nativeFetch(input, options);
};

/** 응답을 JSON으로 읽되, 서버 오류·네트워크 오류를 사용자에게 보여줄 수 있는
 *  형태로 정규화한다. 조용히 실패하지 않는 것이 목적이다. */
async function requestJson(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (err) {
    throw new Error(`서버에 연결하지 못했습니다: ${err.message}`);
  }
  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (_e) {
      payload = null;
    }
  }
  if (!response.ok) {
    const detail = (payload && payload.error) || `HTTP ${response.status}`;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return payload || {};
}

function postForm(url, fields) {
  const body = fields instanceof URLSearchParams ? fields : new URLSearchParams(fields || {});
  return requestJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
}

/** 인라인 오류 표시. alert()과 달리 흐름을 끊지 않고 aria-live로 읽힌다. */
function showInlineError(elementId, message, focusId) {
  const box = document.getElementById(elementId);
  if (box) {
    box.textContent = message;
    box.hidden = false;
  }
  if (focusId) document.getElementById(focusId)?.focus();
}

function clearInlineError(elementId) {
  const box = document.getElementById(elementId);
  if (box) {
    box.textContent = "";
    box.hidden = true;
  }
}

/** 전송 중 버튼 중복 클릭 차단 + 진행 표시. */
async function withBusy(button, label, action) {
  if (!button) return action();
  if (button.dataset.busy === "1") return undefined;
  button.dataset.busy = "1";
  const original = button.innerHTML;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  if (label) button.textContent = label;
  try {
    return await action();
  } finally {
    button.dataset.busy = "0";
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.innerHTML = original;
  }
}

// ──────────────────────────────────────────────────────────────
// 메시지 증분 로딩 상태
// ──────────────────────────────────────────────────────────────
function metaNumber(name, fallback) {
  const value = Number(document.querySelector(`meta[name="${name}"]`)?.content);
  return Number.isFinite(value) ? value : fallback;
}

const feedCursor = {
  first: metaNumber("feed-first-index", 0),
  last: metaNumber("feed-last-index", -1),
  total: metaNumber("message-total", 0),
  loading: false,
};

let followLatestMessages = true;

let viewingId = null;
let lastVersion = null;
let sessionsById = {};
let allSessions = [];
let sessionsActiveId = null;
let guideCardIndex = 0;
let guideRotationTimer = null;
let guideRotationPaused = false;
const GUIDE_ROTATION_MS = 6500;

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

function renderGuideCard(nextIndex) {
  const carousel = $("guideCarousel");
  if (!carousel) return;
  const cards = Array.from(carousel.querySelectorAll("[data-guide-card]"));
  const dots = Array.from(carousel.querySelectorAll(".guide-dot"));
  if (!cards.length) return;
  guideCardIndex = (nextIndex + cards.length) % cards.length;
  cards.forEach((card, index) => {
    const active = index === guideCardIndex;
    card.classList.toggle("is-active", active);
    card.setAttribute("aria-hidden", String(!active));
  });
  dots.forEach((dot, index) => {
    const active = index === guideCardIndex;
    dot.classList.toggle("is-active", active);
    dot.setAttribute("aria-selected", String(active));
    dot.tabIndex = active ? 0 : -1;
  });
  const count = $("guideCarouselCount");
  if (count) count.textContent = `${guideCardIndex + 1} / ${cards.length}`;
}

function stopGuideRotation() {
  if (guideRotationTimer) window.clearInterval(guideRotationTimer);
  guideRotationTimer = null;
}

function startGuideRotation() {
  stopGuideRotation();
  if (guideRotationPaused || document.hidden || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  guideRotationTimer = window.setInterval(() => renderGuideCard(guideCardIndex + 1), GUIDE_ROTATION_MS);
}

function guideStep(direction) {
  renderGuideCard(guideCardIndex + direction);
  startGuideRotation();
}

function selectGuideCard(index) {
  renderGuideCard(index);
  startGuideRotation();
}

function toggleGuideRotation() {
  guideRotationPaused = !guideRotationPaused;
  const button = $("guideToggle");
  if (button) {
    button.textContent = guideRotationPaused ? "재생" : "일시정지";
    button.setAttribute("aria-label", guideRotationPaused ? "카드 자동 회전 재생" : "카드 자동 회전 일시정지");
    button.setAttribute("aria-pressed", String(guideRotationPaused));
  }
  startGuideRotation();
}

function initGuideCarousel() {
  const carousel = $("guideCarousel");
  if (!carousel) return;
  renderGuideCard(0);
  carousel.addEventListener("mouseenter", stopGuideRotation);
  carousel.addEventListener("mouseleave", startGuideRotation);
  carousel.addEventListener("focusin", stopGuideRotation);
  carousel.addEventListener("focusout", (event) => {
    if (!carousel.contains(event.relatedTarget)) startGuideRotation();
  });
  document.addEventListener("visibilitychange", startGuideRotation);
  startGuideRotation();
}

/** 본문과 속성값 모두에 안전하게 넣을 수 있도록 따옴표까지 바꾼다. */
function escapeHtml(str) {
  const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(str ?? "").replace(/[&<>"']/g, (ch) => entities[ch]);
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

/** 컨텍스트 점유율 표시 문자열.
 *  누적 토큰을 컨텍스트 한도로 나누던 옛 계산이 461.1% 같은 값을 만들었다.
 *  이제 서버가 측정/추정/알수없음을 구분해 주므로 그대로 드러낸다. */
function contextDisplay(context) {
  const known = context && context.known;
  if (!known) {
    return { text: "알 수 없음", bar: 0, title: "CLI가 호출 단위 컨텍스트를 제공하지 않습니다.", overflow: false };
  }
  const percent = Number(context.percent || 0);
  const estimated = context.source !== "measured";
  const prefix = estimated ? "추정 " : "실측 ";
  return {
    text: `${prefix}${percent.toFixed(1)}%`,
    bar: Number(context.bar_percent ?? Math.min(100, percent)),
    overflow: Boolean(context.overflow),
    title: estimated
      ? "프롬프트 길이 기반 추정치입니다."
      : "CLI가 보고한 호출 단위 사용량입니다.",
  };
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
      <p>저장 폴더: ${escapeHtml(data.memory_dir || "-")}</p>
    </div>`;
}

function renderSessionList(sessions, activeId) {
  allSessions = sessions;
  sessionsActiveId = activeId;
  sessionsById = Object.fromEntries(sessions.map((session) => [session.id, session]));
  applySessionFilters();
}

let lastSessionListSignature = null;

function applySessionFilters(force = false) {
  const list = $("sessionList");
  const query = ($("sessionSearch")?.value || "").trim().toLowerCase();
  const favoriteOnly = $("favoriteOnly")?.checked || false;
  const showArchived = $("showArchived")?.checked || false;
  const sessions = allSessions.filter((session) => {
    const haystack = `${session.name || ""} ${session.topic || ""} ${(session.tags || []).join(" ")}`.toLowerCase();
    return (!query || haystack.includes(query)) && (!favoriteOnly || session.favorite) && (showArchived ? session.archived : !session.archived);
  });
  // 4초마다 전체 DOM을 갈아엎지 않는다 — 내용이 같으면 건드리지 않는다.
  const signature = JSON.stringify(sessions.map((s) => [
    s.id, s.name, s.status_label, s.mode_label, s.message_count, s.favorite, s.tags,
  ])) + `|${viewingId}|${sessionsActiveId}`;
  if (!force && signature === lastSessionListSignature) return;
  lastSessionListSignature = signature;

  if (!sessions.length) {
    list.innerHTML = '<div class="empty-state">아직 세션이 없습니다.</div>';
    return;
  }
  list.innerHTML = sessions.map((s) => {
    const cls = `session-item${s.id === (viewingId || sessionsActiveId) ? " active" : ""}`;
    // 서버가 상단 배너와 같은 규칙으로 계산해 보낸 값. 색만이 아니라 기호+문구로
    // 표시해 색각 이상에서도 구분할 수 있게 한다.
    const status = s.status_label || (!s.topic ? "준비" : (s.finished ? "완료" : "보류"));
    const glyph = s.status_glyph || "·";
    const tags = (s.tags || []).map((tag) => `<span>#${escapeHtml(tag)}</span>`).join("");
    const isCurrent = s.id === (viewingId || sessionsActiveId);
    return `<div class="session-row">
      <button class="${cls}" data-session-id="${escapeHtml(s.id)}" onclick="viewSession(this.dataset.sessionId)" aria-current="${isCurrent ? "page" : "false"}" aria-label="${escapeHtml(s.name || s.topic)} · ${escapeHtml(status)}">
        <span class="s-topic">${s.favorite ? "★ " : ""}${escapeHtml(s.name || s.topic)}</span>
        <span class="s-meta"><span class="s-glyph" aria-hidden="true">${escapeHtml(glyph)}</span>${escapeHtml(status)} · ${escapeHtml(s.mode_label)} · ${s.message_count}개</span>
        <span class="s-tags">${tags}</span>
      </button>
      <button class="session-rename icon-button" data-session-id="${escapeHtml(s.id)}" onclick="openSessionActions(this.dataset.sessionId)" title="세션 메뉴" aria-label="세션 메뉴">•••</button>
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
    const contextView = contextDisplay(context);
    const contextTokenText = context.known
      ? `${context.source !== "measured" ? "~" : ""}${formatTokens(context.used_tokens || 0)} / ${formatTokens(context.limit_tokens || 0)}`
      : "-";
    return `<div class="agent-usage-row" style="--agent-color:${info.color}">
      <div class="agent-usage-head"><span>${info.label}<small>${row.turns || 0}턴</small></span><strong>점유 ${percentText} · ${tokenText}</strong></div>
      <div class="agent-usage-track" role="progressbar" aria-label="${info.label} 토큰 점유율" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent.toFixed(1)}">
        <span style="width:${Math.min(100, percent).toFixed(2)}%"></span>
      </div>
      <div class="agent-context-meta" title="${escapeHtml(contextView.title)}"><span>현재 컨텍스트</span><strong>${escapeHtml(contextView.text)} · ${contextTokenText}${contextView.overflow ? " ⚠ 한도 초과" : ""}</strong></div>
      <div class="agent-context-track${contextView.overflow ? " is-overflow" : ""}" role="progressbar" aria-label="${info.label} 현재 컨텍스트 사용률" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${contextView.bar.toFixed(1)}" aria-valuetext="${escapeHtml(contextView.text)}">
        <span style="width:${contextView.bar.toFixed(2)}%"></span>
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
    const contextView = contextDisplay(context);
    const contextTokens = context.known
      ? `${context.source !== "measured" ? "~" : ""}${formatTokens(context.used_tokens || 0)} / ${formatTokens(context.limit_tokens || 0)}`
      : "-";
    const cumulative = formatTokens(context.session_cumulative_tokens || 0);
    return `<article class="token-model-card" style="--agent-color:${info.color}">
      <header><img src="${info.avatar}" alt=""><div><strong>${info.label}</strong><span>${context.estimated ? "컨텍스트 추정값" : "최신 실제 컨텍스트"}</span></div></header>
      <div class="token-model-total" title="${escapeHtml(contextView.title)}"><strong>${escapeHtml(contextView.text)}</strong><span>${contextTokens}토큰</span></div>
      <div class="token-model-track${contextView.overflow ? " is-overflow" : ""}" role="progressbar" aria-label="${info.label} 컨텍스트 사용률" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${contextView.bar.toFixed(1)}" aria-valuetext="${escapeHtml(contextView.text)}"><span style="width:${contextView.bar.toFixed(2)}%"></span></div>
      <div class="token-model-cumulative">세션 누적 ${cumulative}토큰 <span class="token-hint">(컨텍스트 한도와 무관)</span></div>
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
  // 서버 모드가 바뀔 때만 맞춘다. 매 폴링마다 맞추면 "전환"을 누르기 전에 고른 값이 되돌아간다.
  if (data.mode && lastState.selectMode !== data.mode) {
    ["sessionModeSelect", "composerModeSelect"].forEach((id) => { if ($(id)) $(id).value = data.mode; });
    lastState.selectMode = data.mode;
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
  }
  renderCostSummary(data);
  // 예산 경고와 저장 실패는 둘 다 사용자가 손을 써야 풀리는 문제다.
  const blockingNotice = data.save_error || data.budget_exceeded || "";
  $("budgetWarning").textContent = blockingNotice;
  $("budgetWarning").style.display = blockingNotice ? "block" : "none";
  renderComposerError();
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

/** 비용 표시. CLI가 보고한 값과 우리가 토큰으로 추정한 값은 근거가 달라서
 *  하나의 확정 숫자로 합치지 않는다. 단위(USD)와 계산 근거를 함께 보여준다. */
function renderCostSummary(data) {
  const box = $("statCost");
  if (!box) return;
  const cost = data.cost_breakdown || {};
  const reported = Number(cost.reported_usd || 0);
  const estimated = Number(cost.estimated_usd || 0);
  const currency = cost.currency || "USD";
  let text;
  if (reported > 0 && estimated > 0) {
    text = `$${reported.toFixed(4)} + 추정 $${estimated.toFixed(4)}`;
  } else if (reported > 0) {
    text = `$${reported.toFixed(4)}`;
  } else if (estimated > 0) {
    text = `추정 $${estimated.toFixed(4)}`;
  } else {
    text = "$0.0000";
  }
  box.textContent = text;
  box.title = `${currency} · ${cost.basis || "집계된 턴 없음"}`;
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

/** 사용자가 하단을 보고 있는지. 여기서만 판단하고 모든 스크롤 결정이 이 값을 쓴다. */
function isNearBottom(scroller, threshold = 120) {
  if (!scroller) return true;
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < threshold;
}

function handleFeedScroll() {
  const scroller = $("conversationScroll");
  followLatestMessages = isNearBottom(scroller);
  const jump = $("jumpLatest");
  if (jump) jump.hidden = followLatestMessages;
}

function jumpToLatest() {
  const scroller = $("conversationScroll");
  if (!scroller) return;
  scroller.scrollTop = scroller.scrollHeight;
  followLatestMessages = true;
  const jump = $("jumpLatest");
  if (jump) jump.hidden = true;
}

/** 새 메시지만 이어붙인다. 전체 innerHTML 교체를 하지 않는다. */
function appendMessagesHtml(html) {
  const feed = $("feed");
  const scroller = $("conversationScroll");
  if (!feed || !html) return;
  const wasNearBottom = isNearBottom(scroller);
  const holder = document.createElement("div");
  holder.innerHTML = html;
  const added = Array.from(holder.children);
  if (added.length) feed.querySelector(":scope > .empty-state")?.remove();
  // 진행 중 표시는 항상 맨 아래에 남도록 그 앞에 끼워 넣는다 (없으면 맨 끝).
  const thinking = $("thinkingIndicator");
  added.forEach((node) => feed.insertBefore(node, thinking));
  if (wasNearBottom && followLatestMessages) {
    requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
  } else if (added.length) {
    const jump = $("jumpLatest");
    if (jump) jump.hidden = false;
  }
}

/** 서버에서 새 메시지만 받아온다 (전체 재전송 없음). */
async function fetchNewMessages() {
  if (feedCursor.loading) return;
  feedCursor.loading = true;
  try {
    const data = await requestJson(`/messages.json?after=${feedCursor.last}&limit=80`);
    if (data.count > 0) {
      appendMessagesHtml(data.messages_html);
      feedCursor.last = data.to_index;
    }
    feedCursor.total = data.total ?? feedCursor.total;
  } catch (_e) {
    // 폴링 실패는 상태 표시에서 이미 알린다.
  } finally {
    feedCursor.loading = false;
  }
}

/** "이전 메시지 불러오기" — 최초에는 최근 것만 받으므로 과거는 여기서 채운다. */
async function loadOlderMessages() {
  if (feedCursor.loading || feedCursor.first <= 0) return;
  feedCursor.loading = true;
  const wrap = $("loadOlderWrap");
  const scroller = $("conversationScroll");
  const previousHeight = scroller ? scroller.scrollHeight : 0;
  try {
    const data = await requestJson(`/messages-older.json?before=${feedCursor.first}&limit=60`);
    if (data.count > 0) {
      const holder = document.createElement("div");
      holder.innerHTML = data.messages_html;
      const feed = $("feed");
      const anchor = wrap && wrap.parentNode === feed ? wrap.nextSibling : feed.firstChild;
      Array.from(holder.children).forEach((node) => feed.insertBefore(node, anchor));
      feedCursor.first = data.from_index;
      // 읽던 위치를 유지한다.
      if (scroller) scroller.scrollTop += scroller.scrollHeight - previousHeight;
    }
    if (wrap) {
      if (feedCursor.first > 0) {
        wrap.querySelector("button").textContent = `이전 메시지 ${feedCursor.first.toLocaleString()}개 불러오기`;
      } else {
        wrap.remove();
      }
    }
  } catch (err) {
    if (wrap) wrap.querySelector("button").textContent = `불러오지 못했습니다 (${err.message}) · 다시 시도`;
  } finally {
    feedCursor.loading = false;
  }
}

/** 지난 세션을 보다가 돌아오면 피드에 남의 대화가 들어 있다. 첫 화면과 같은
 *  구성(모드 안내 + 이전 메시지 버튼 + 최근 메시지)으로 다시 채우고 커서도 맞춘다. */
let liveFeedStale = false;

async function reloadLiveFeed(state) {
  const page = await requestJson("/messages.json?after=-1&limit=60");
  if (viewingId) return;
  feedCursor.first = page.from_index;
  feedCursor.last = page.to_index;
  feedCursor.total = page.total;
  const older = page.from_index > 0
    ? `<div class="load-older" id="loadOlderWrap"><button type="button" class="ghost" onclick="loadOlderMessages()">이전 메시지 ${page.from_index.toLocaleString()}개 불러오기</button></div>`
    : "";
  $("feed").innerHTML = (state.mode_notice_html || "") + older
    + (page.messages_html || '<div class="empty-state">아직 대화가 없습니다.</div>');
  liveFeedStale = false;
  requestAnimationFrame(jumpToLatest);
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

function renderConnection(connHtml) {
  $("conn").innerHTML = `<div class="conn-list">${connHtml || ""}</div><button class="ghost compact" onclick="recheck()"><svg viewBox="0 0 24 24" style="margin-right:4px"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>다시 확인</button>`;
  updateConnectionSummary();
  lastState.conn_html = connHtml;
}

function startFieldKey(el) {
  return el.type === "radio" || el.type === "checkbox" ? `${el.name}=${el.value}` : el.id || el.name;
}

/** 시작 폼은 작업 폴더·권한이 바뀔 때 서버 HTML로 다시 그려진다. 그때 입력하던
 *  주제와 고른 모드·모델·세부 설정 펼침 상태를 잃지 않게 옮겨 담는다. */
function replaceTopicSection(html) {
  const section = $("topicSection");
  const fields = "input, textarea, select";
  const saved = new Map(Array.from(section.querySelectorAll(fields), (el) => [
    startFieldKey(el), el.type === "radio" || el.type === "checkbox" ? el.checked : el.value,
  ]));
  const optionsOpen = $("startOptions") ? !$("startOptions").hidden : false;
  const focused = section.contains(document.activeElement) ? startFieldKey(document.activeElement) : null;
  section.innerHTML = html;
  section.querySelectorAll(fields).forEach((el) => {
    const key = startFieldKey(el);
    if (!saved.has(key)) return;
    const value = saved.get(key);
    if (el.type === "radio" || el.type === "checkbox") el.checked = value;
    else if (el.tagName !== "SELECT" || Array.from(el.options).some((option) => option.value === value)) el.value = value;
    if (key === focused) el.focus();
  });
  if (optionsOpen && $("startOptions")?.hidden) toggleStartOptions();
  syncDirectAgentButtons();
}

function applyState(data) {
  if (data.csrf_token) csrfToken = data.csrf_token;
  if (lastState.status !== data.status) {
    $("status").textContent = `상태 · ${data.status || "준비"}`;
    $("status").dataset.status = data.status || "준비";
    if (!networkErrorMessage && $("mobileStatus")) $("mobileStatus").textContent = data.status || "준비";
    lastState.status = data.status;
  }
  if (lastState.conn_html !== data.conn_html) renderConnection(data.conn_html);

  const currentTopicHtml = data.topic_section_html || renderTopic(data);
  if (lastState.topic_html !== currentTopicHtml) {
    replaceTopicSection(currentTopicHtml);
    syncAgentModelCards();
    lastState.topic_html = currentTopicHtml;
    // 주제 영역을 통째로 갈아끼우면 실행 계획 상자도 비워지므로 다시 채운다.
    if ($("executionPlan")) refreshExecutionPlan();
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
  const request = (extra = {}) => fetch("/session/activate", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ id, ...extra }).toString(),
  }).then((response) => response.json());
  let data = await request();
  // 세션 폴더가 현재 폴더와 다르면 서버가 확인을 요구한다. 사용자가 동의할 때만 다시 보낸다.
  if (data.needs_workspace_confirm) {
    if (!confirm(data.error)) return;
    data = await request({ confirm_workspace: "true" });
  }
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
  if (data.error) { alert(data.error); return; }
  closeSessionActions();
  // 보던 세션을 지웠으면 읽기 전용 화면을 닫고 현재 세션으로 돌아간다.
  if (id === viewingId) await backToLive();
  else await loadSessions();
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
    if (viewingId) await backToLive();
    else await loadSessions();
    return;
  }
  viewingId = id;
  $("feed").innerHTML = data.feed_html;
  $("topicSection").innerHTML = data.topic_section_html || renderTopic(data);
  $("viewingBanner").style.display = "flex";
  $("controlPanel").style.display = "none";
  $("approveBanner").style.display = "none";
  $("thinkingIndicator")?.remove();
  $("conversationScroll").scrollTop = 0;
  updateInspector(data);
  await loadSessions();
}

async function backToLive() {
  viewingId = null;
  // 지난 세션을 보는 동안 덮어쓰거나 숨긴 영역을 다음 상태 반영 때 다시 그리게 한다.
  liveFeedStale = true;
  lastState.topic_html = null;
  lastState.showApprove = null;
  $("viewingBanner").style.display = "none";
  $("controlPanel").style.display = "flex";
  await poll(true);
  await loadSessions();
}

const POLL_BASE_MS = 1800;
const POLL_MAX_BACKOFF_MS = 30000;
let pollFailures = 0;
let pollTimer = null;

let networkErrorMessage = "";

/** 하단 오류 줄: 연결 오류가 먼저, 없으면 저장 실패. 둘 다 풀리면 지운다. */
function renderComposerError() {
  const message = networkErrorMessage || lastState.latestPayload?.save_error || "";
  if (lastState.composerError === message) return;
  lastState.composerError = message;
  if (message) showInlineError("composerError", message);
  else clearInlineError("composerError");
}

function setNetworkStatus(message) {
  networkErrorMessage = message;
  const box = $("mobileStatus");
  if (box) box.textContent = message || lastState.status || "준비";
  renderComposerError();
}

async function poll(force = false) {
  if (viewingId) return;
  try {
    let changed = force || liveFeedStale;
    if (!changed) {
      const vdata = await requestJson("/version.json");
      changed = vdata.version !== lastVersion;
      lastVersion = vdata.version;
    }
    if (changed) {
      // feed_html은 받지 않는다 — 새 메시지는 /messages.json으로 증분 조회한다.
      const data = await requestJson("/state.json");
      if (liveFeedStale) await reloadLiveFeed(data);
      if (viewingId) return;
      applyState(data);
      if (Number(data.message_count || 0) > feedCursor.last + 1) await fetchNewMessages();
    }
    if (pollFailures) setNetworkStatus("");
    pollFailures = 0;
  } catch (err) {
    pollFailures += 1;
    if (pollFailures >= 3) {
      setNetworkStatus(`상태를 갱신하지 못했습니다: ${err.message} (자동 재시도 중)`);
    }
  }
}

/** 고정 간격 폭주 대신 제한된 backoff. 백그라운드 탭에서는 훨씬 느리게. */
function nextPollDelay() {
  if (document.hidden) return 15000;
  if (!pollFailures) return POLL_BASE_MS;
  return Math.min(POLL_MAX_BACKOFF_MS, POLL_BASE_MS * 2 ** Math.min(pollFailures, 5));
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!document.hidden) await poll(false);
    schedulePoll();
  }, nextPollDelay());
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    // 탭으로 돌아오면 즉시 한 번 맞춘다.
    poll(true);
  }
  schedulePoll();
});

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
  await postControl("/defer", "보류 요청이 실패했습니다");
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
  if (!topic) {
    // 예전에는 조용히 return 해서 왜 시작되지 않는지 알 수 없었다.
    showInlineError("topicError", "먼저 주제나 질문을 입력해주세요.", "topicInput");
    return;
  }
  clearInlineError("topicError");
  const modeEl = document.querySelector('input[name="mode"]:checked');
  const mode = modeEl ? modeEl.value : "discussion";
  const discussionAccessEl = document.querySelector('input[name="discussion_project_access"]:checked');
  const discussionProjectAccess = discussionAccessEl ? discussionAccessEl.value : "read";
  const agents = Array.from(document.querySelectorAll('input[name="agent"]:checked')).map((el) => el.value);
  if (!agents.length) {
    showInlineError("topicError", "최소 한 명의 모델을 선택해주세요.", "topicInput");
    return;
  }
  // 기본 "시작하기"는 startAgent 없이 호출된다 — 서버가 첫 활성 모델부터 시작한다.
  if (startAgent && !agents.includes(startAgent)) {
    showInlineError(
      "topicError",
      "직접 호출하려는 모델이 비활성 상태입니다. 먼저 그 모델을 켜주세요.",
      "topicInput",
    );
    return;
  }
  clearInlineError("topicError");
  const params = new URLSearchParams({ topic, mode, discussion_project_access: discussionProjectAccess });
  params.set("start_agent", startAgent);
  agents.forEach((agent) => params.append("agent", agent));
  Object.keys(AGENTS).forEach((agent) => {
    const model = document.querySelector(`[name="model_${agent}"]`);
    const effort = document.querySelector(`[name="effort_${agent}"]`);
    if (model) params.set(`model_${agent}`, model.value);
    if (effort) params.set(`effort_${agent}`, effort.value);
  });
  const button = document.activeElement instanceof HTMLButtonElement ? document.activeElement : null;
  try {
    await withBusy(button, "시작 중...", async () => {
      const response = await fetch("/topic", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
          const payload = await response.json();
          if (payload.error) detail = payload.error;
        } catch (_e) { /* 본문이 JSON이 아니면 상태 코드만 보여준다 */ }
        throw new Error(detail);
      }
      location.reload();
    });
  } catch (err) {
    showInlineError("topicError", `시작하지 못했습니다: ${err.message}`, "topicInput");
  }
}

// ──────────────────────────────────────────────────────────────
// 실행 전 계획 표시
// ──────────────────────────────────────────────────────────────
/** 시작 버튼을 누르기 전에 "무엇이 몇 번 실행되는지"를 보여준다. */
async function refreshExecutionPlan() {
  const box = $("executionPlan");
  if (!box) return;
  const mode = document.querySelector('input[name="mode"]:checked')?.value || "discussion";
  const access = document.querySelector('input[name="discussion_project_access"]:checked')?.value || "read";
  const agents = Array.from(document.querySelectorAll('input[name="agent"]:checked')).map((el) => el.value);
  const params = new URLSearchParams({ mode, access });
  agents.forEach((agent) => params.append("agent", agent));
  try {
    const plan = await requestJson(`/plan.json?${params.toString()}`);
    const calls = plan.min_calls === plan.max_calls
      ? `${plan.min_calls}회`
      : `${plan.min_calls}~${plan.max_calls}회`;
    const rows = [
      ["실행 모드", plan.mode_label],
      ["호출될 모델", `${plan.agent_count}명 (${plan.agent_labels.join(", ")})`],
      ["예상 모델 호출", calls],
      ["프로젝트 권한", plan.project_access_label],
      ["작업 폴더", plan.workspace_ok ? plan.workspace_path : `차단됨 — ${plan.workspace_message}`],
      ["자동 검증", plan.auto_validation ? "실행함" : "실행하지 않음"],
    ];
    const warning = plan.long_running_warning
      ? '<p class="execution-plan-warning">단계가 많아 실행 시간이 길어질 수 있습니다.</p>'
      : "";
    const blocked = plan.workspace_ok
      ? ""
      : '<p class="execution-plan-warning">작업 폴더가 유효하지 않아 실행이 차단됩니다.</p>';
    box.innerHTML =
      `<div class="execution-plan-title">실행 계획</div>` +
      rows.map(([label, value]) =>
        `<div class="execution-plan-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></div>`
      ).join("") +
      (plan.note ? `<p class="execution-plan-note">${escapeHtml(plan.note)}</p>` : "") +
      warning + blocked;
  } catch (err) {
    box.textContent = `실행 계획을 불러오지 못했습니다: ${err.message}`;
  }
}

/** 세부 설정 접기/펼치기. 접힌 상태의 컨트롤은 hidden이라 탭 순서에도 안 남는다. */
function toggleStartOptions() {
  const box = document.getElementById("startOptions");
  const button = document.getElementById("startOptionsToggle");
  if (!box || !button) return;
  const willOpen = box.hidden;
  box.hidden = !willOpen;
  button.setAttribute("aria-expanded", String(willOpen));
  button.textContent = willOpen ? "세부 설정 닫기" : "세부 설정";
  if (willOpen) refreshExecutionPlan();
}

// ──────────────────────────────────────────────────────────────
// 모바일 패널 (세션 목록 / 상태 인스펙터)
// ──────────────────────────────────────────────────────────────
// display:none으로 정보를 없애지 않고, 접이식 패널로 접근할 수 있게 한다.
let openMobilePanel = null;

function toggleMobilePanel(which) {
  if (openMobilePanel === which) { closeMobilePanels(); return; }
  openMobilePanel = which;
  const sidebar = $("sidebarPanel");
  const inspector = $("inspectorPanel");
  const scrim = $("mobileScrim");
  sidebar?.classList.toggle("is-open", which === "sessions");
  inspector?.classList.toggle("is-open", which === "inspector");
  document.body.classList.add("mobile-panel-open");
  if (scrim) scrim.hidden = false;
  $("mobileSessionsButton")?.setAttribute("aria-expanded", String(which === "sessions"));
  $("mobileInspectorButton")?.setAttribute("aria-expanded", String(which === "inspector"));
  const panel = which === "sessions" ? sidebar : inspector;
  panel?.querySelector(".mobile-panel-close")?.focus();
}

function closeMobilePanels() {
  openMobilePanel = null;
  $("sidebarPanel")?.classList.remove("is-open");
  $("inspectorPanel")?.classList.remove("is-open");
  document.body.classList.remove("mobile-panel-open");
  const scrim = $("mobileScrim");
  if (scrim) scrim.hidden = true;
  $("mobileSessionsButton")?.setAttribute("aria-expanded", "false");
  $("mobileInspectorButton")?.setAttribute("aria-expanded", "false");
}

// 키보드만으로도 패널을 닫을 수 있어야 한다.
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && openMobilePanel) {
    closeMobilePanels();
    $("mobileInspectorButton")?.focus();
  }
});

/** 제어 요청을 보내고, 실패하면 조용히 넘어가지 않고 알린다. 성공하면 응답을 돌려준다. */
async function postControl(url, failMessage) {
  try {
    const data = await postForm(url);
    if (data.error) throw new Error(data.error);
    return data;
  } catch (err) {
    alert(`${failMessage}: ${err.message}`);
    return null;
  }
}

async function recheck() {
  const previous = lastState.conn_html;
  $("conn").innerHTML = '<span class="conn-item">확인 중...</span>';
  // 결과가 이전과 같아도 연결 막대를 다시 그려야 "확인 중..."에 멈추지 않는다.
  const data = await postControl("/preflight", "연결 확인에 실패했습니다");
  renderConnection(data ? data.conn_html : previous);
  await poll(true);
}

async function ctrl(action) {
  const labels = { pause: "일시정지", resume: "재개" };
  await postControl(`/${action}`, `${labels[action] || action} 요청이 실패했습니다`);
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
  await postControl("/stop", "중단 요청이 실패했습니다");
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

// 최초 진입 시 실행 계획을 바로 보여준다 (무엇이 몇 번 실행되는지).
if (document.getElementById("executionPlan")) refreshExecutionPlan();
schedulePoll();
// 세션 목록은 변경이 있을 때만 다시 그린다 (renderSessionList 내부에서 서명 비교).
setInterval(() => { if (!document.hidden) loadSessions(); }, 6000);
initGuideCarousel();
poll(true);
loadSessions();
