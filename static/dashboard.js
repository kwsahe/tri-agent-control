let viewingId = null;
let lastVersion = null;

const AGENTS = {
  codex: { label: "Codex", color: "#4f8cff", avatar: "/static/agents/codex.png" },
  antigravity: { label: "Antigravity", color: "#a66cff", avatar: "/static/agents/antigravity.png" },
  claude: { label: "Claude Code", color: "#ff8a3d", avatar: "/static/agents/claude.svg" },
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
  return `${Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 1 })}s`;
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
      <p>저장 폴더: ${data.memory_dir || "-"}</p>
    </div>`;
}

function renderSessionList(sessions, activeId) {
  const list = $("sessionList");
  if (!sessions.length) {
    list.innerHTML = '<div class="empty-state">아직 세션이 없습니다.</div>';
    return;
  }
  list.innerHTML = sessions.map((s) => {
    const cls = `session-item${s.id === (viewingId || activeId) ? " active" : ""}`;
    const status = !s.topic ? "준비" : (s.finished ? "완료" : (s.id === activeId ? "진행 중" : "보류"));
    return `<button class="${cls}" onclick="viewSession('${s.id}')">
      <span class="s-topic">${escapeHtml(s.topic)}</span>
      <span class="s-meta">${status} · ${escapeHtml(s.mode_label)} · ${s.message_count}개</span>
    </button>`;
  }).join("");
}

function updateStatsBoard(tokens, time) {
  $("statTokens").textContent = formatTokens(tokens);
  $("statTime").textContent = formatTime(time);
  $("statCost").textContent = estimateCost(tokens);
}

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
    return `<div class="${cls}" style="--agent-color:${info.color}">
      <div class="agent-chip-identity"><img src="${info.avatar}" alt=""><div><strong>${info.label}</strong><small>${escapeHtml(model)}</small></div></div>
      <span>${escapeHtml(state)}</span>
    </div>`;
  }).join("");
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
    return;
  }
  const agent = AGENTS[data.active_agent] || { label: data.active_agent, color: "#4f8cff" };
  $("liveCard").classList.add("active");
  $("liveCard").style.setProperty("--live-color", agent.color);
  $("liveLine").textContent = `${agent.label} · ${data.active_phase || "생각 중"}`;
  $("liveDetail").textContent = `${formatTime(data.active_elapsed)} · ${data.active_cli_mode || "-"} · 입력 ${formatTokens(data.active_prompt_chars)}자`;
}

function updateInspector(data) {
  if (lastState.sideStatus !== data.status) {
    $("sideStatus").textContent = data.status || "-";
    lastState.sideStatus = data.status;
  }
  if (lastState.sideMode !== data.mode_label) {
    $("sideMode").textContent = data.mode_label || "-";
    lastState.sideMode = data.mode_label;
  }
  if ($("sessionModeSelect") && data.mode && $("sessionModeSelect").value !== data.mode) {
    $("sessionModeSelect").value = data.mode;
  }
  if ($("composerModeSelect") && data.mode && $("composerModeSelect").value !== data.mode) {
    $("composerModeSelect").value = data.mode;
  }
  if (lastState.sideMessages !== data.message_count) {
    $("sideMessages").textContent = data.message_count || 0;
    lastState.sideMessages = data.message_count;
  }
  if (lastState.memory_dir !== data.memory_dir) {
    $("memoryPath").textContent = data.memory_dir || "세션 시작 후 표시됩니다.";
    lastState.memory_dir = data.memory_dir;
  }
  const currentProfilePath = data.profile_path ? `Profile: ${data.profile_path}` : "Profile.md도 여기에 표시됩니다.";
  if (lastState.profilePath !== currentProfilePath) {
    $("profilePath").textContent = currentProfilePath;
    lastState.profilePath = currentProfilePath;
  }

  if (lastState.total_est_tokens !== data.total_est_tokens || lastState.total_elapsed_time !== data.total_elapsed_time) {
    updateStatsBoard(data.total_est_tokens, data.total_elapsed_time);
    lastState.total_est_tokens = data.total_est_tokens;
    lastState.total_elapsed_time = data.total_elapsed_time;
  }

  // renderAgentStack 캐싱
  const enabled = (data.enabled_agents || []).length
    ? data.enabled_agents
    : Object.keys(AGENTS).filter((key) => (data.enabled_agents_label || "").includes(AGENTS[key].label));
  const active = data.active_agent;
  const agentStackKey = `${enabled.join(",")}_${active}_${data.active_phase}_${JSON.stringify(data.agent_setting_labels || {})}`;
  if (lastState.agentStackKey !== agentStackKey) {
    renderAgentStack(data);
    lastState.agentStackKey = agentStackKey;
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
}

function updateThinking(data) {
  const thinkingKey = `${data.active_agent}_${data.active_phase}_${data.active_elapsed}_${data.active_cli_mode}_${data.active_prompt_chars}`;
  if (lastState.thinkingKey === thinkingKey) return;
  lastState.thinkingKey = thinkingKey;

  const box = $("thinkingIndicator");
  if (!data.active_agent) {
    box.style.display = "none";
    return;
  }
  const agent = AGENTS[data.active_agent] || { label: data.active_agent, color: "#4f8cff" };
  box.style.setProperty("--spinner-color", agent.color);
  box.innerHTML = `
    <div class="spinner"></div>
    <div class="thinking-text">
      <strong>${agent.label} · ${escapeHtml(data.active_phase || "생각 중")}</strong>
      <span>${formatTime(data.active_elapsed)} · ${escapeHtml(data.active_cli_mode || "-")} · 입력 ${formatTokens(data.active_prompt_chars)}자</span>
    </div>`;
  box.style.display = "flex";
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

function applyState(data) {
  if (lastState.feed_html !== data.feed_html) {
    updateFeedHtml(data.feed_html);
    lastState.feed_html = data.feed_html;
  }
  if (lastState.status !== data.status) {
    $("status").textContent = data.status;
    lastState.status = data.status;
  }
  if (lastState.conn_html !== data.conn_html) {
    $("conn").innerHTML = `<div class="conn-list">${data.conn_html}</div><button class="ghost compact" onclick="recheck()"><svg viewBox="0 0 24 24" style="margin-right:4px"><path d="M17.65 6.35A7.958 7.958 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>다시 확인</button>`;
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
  const resumeDisabled = !hasTopic || data.finished || data.stopped || !data.paused;
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

  const showApprove = (data.awaiting_approval && !data.stopped) ? "block" : "none";
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
}

async function loadSessions() {
  try {
    const r = await fetch("/sessions.json");
    const data = await r.json();
    renderSessionList(data.sessions, data.active_id);
  } catch (_e) {}
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
    $("thinkingIndicator").style.display = "none";
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

async function submitTopic() {
  const topic = $("topicInput").value.trim();
  if (!topic) return;
  const modeEl = document.querySelector('input[name="mode"]:checked');
  const mode = modeEl ? modeEl.value : "discussion";
  const agents = Array.from(document.querySelectorAll('input[name="agent"]:checked')).map((el) => el.value);
  if (!agents.length) {
    alert("최소 한 명의 에이전트를 선택해주세요.");
    return;
  }
  const params = new URLSearchParams({ topic, mode });
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
  if (!confirm("중단할까요? 현재 진행 중인 턴이 끝나면 멈춥니다.")) return;
  await fetch("/stop", { method: "POST" });
  await poll(true);
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
      body: `text=${encodeURIComponent(text)}&intent=${encodeURIComponent(intent)}${targetBody}`,
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
      body: `text=${encodeURIComponent(text)}&intent=${encodeURIComponent(intent)}${targetBody}`,
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
