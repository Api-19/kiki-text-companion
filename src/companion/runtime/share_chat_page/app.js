"use strict";

const byId = (id) => document.getElementById(id);
const token = location.hash.slice(1);
history.replaceState(null, "", "/");
const chatPanel = byId("chat-panel");
const blindPanel = byId("blind-panel");
const tabs = byId("tabs");
const log = byId("log");
const feedback = byId("feedback");
const chatText = byId("chat-text");
const blindText = byId("blind-text");
let state;
let busy = false;
let closed = !token;
let pollTimer = null;

const summaryLabels = {
  total_rounds: "总轮次",
  comparable_rounds: "已比较",
  decisive_votes: "明确选择",
  layered_wins: "新方案胜",
  original_wins: "原方案胜",
  tie: "差不多",
  both_bad: "都不好",
  skipped: "跳过",
  failed: "生成失败",
};

async function request(path, body = {}, keepalive = false) {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Chat-Token": token},
    cache: "no-store",
    credentials: "omit",
    body: JSON.stringify(body),
    keepalive,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.message || "请求失败。");
  }
  return data;
}

function setButtonsDisabled(disabled) {
  document.querySelectorAll("button").forEach((button) => { button.disabled = disabled; });
}

function applyLocks() {
  const calibration = state?.calibration?.active === true;
  const ended = state?.phase === "closed";
  const blindPhase = state?.blind?.phase;
  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.disabled = busy || calibration || ended;
  });
  byId("calibrate").disabled = busy || calibration || ended || state?.mode !== "chat";
  chatText.disabled = busy || calibration || ended;
  byId("chat-send").disabled = busy || calibration || ended;
  const blindLocked = busy || calibration || ended ||
    blindPhase === "waiting" || blindPhase === "complete";
  blindText.disabled = blindLocked;
  byId("blind-send").disabled = blindLocked;
  byId("blind-finish").disabled = busy || calibration || ended;
}

function renderSummary(target, summary) {
  target.replaceChildren();
  if (!summary) return;
  for (const [key, label] of Object.entries(summaryLabels)) {
    const item = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const value = document.createElement("dd");
    value.textContent = String(summary[key] ?? 0);
    item.append(term, value);
    target.append(item);
  }
}

function renderChat(data) {
  log.replaceChildren();
  if (!data.messages.length) {
    const hint = document.createElement("p");
    hint.className = "bubble assistant";
    hint.textContent = "输入一句话开始聊天。";
    log.append(hint);
    return;
  }
  for (const message of data.messages) {
    const bubble = document.createElement("p");
    bubble.className = `bubble ${message.role}`;
    bubble.textContent = message.text;
    log.append(bubble);
  }
  log.scrollTop = log.scrollHeight;
}

function renderBlind(data) {
  const blind = data.blind;
  if (!blind) return;
  const consent = byId("blind-consent");
  const work = byId("blind-work");
  const round = byId("blind-round");
  const summary = byId("blind-summary");
  consent.hidden = blind.phase !== "consent";
  work.hidden = blind.phase === "consent";
  byId("blind-notice").textContent = blind.notice || "";

  const waiting = blind.phase === "waiting" && blind.round;
  round.hidden = !waiting;
  if (waiting) {
    byId("blind-echo").textContent = blind.round.text || "";
    byId("blind-reply-a").textContent = blind.round.replies[0]?.text ?? "";
    byId("blind-reply-b").textContent = blind.round.replies[1]?.text ?? "";
    const counts = blind.summary || {};
    byId("blind-progress").textContent =
      `已比较 ${counts.comparable_rounds ?? 0} 轮 · 明确选择 ${counts.decisive_votes ?? 0} 轮`;
    byId("blind-round-title").focus();
  } else {
    byId("blind-echo").textContent = "";
    byId("blind-reply-a").textContent = "";
    byId("blind-reply-b").textContent = "";
  }

  const finished = blind.phase === "complete" || blind.phase === "closed";
  summary.hidden = !finished;
  if (finished) renderSummary(byId("blind-summary-list"), blind.summary);
  byId("blind-finish").hidden = blind.phase === "consent" || finished;
}

function renderCalibration(data) {
  const banner = byId("calibration-banner");
  const active = data.calibration.active === true && data.phase !== "closed";
  banner.hidden = !active;
  if (active) {
    byId("calibration-link").href = data.calibration.url || "#";
  }
}

function syncPolling() {
  const active = state?.calibration?.active === true && state?.phase !== "closed";
  if (active && !pollTimer) {
    pollTimer = setInterval(refreshState, 2000);
  } else if (!active && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function refreshState() {
  if (busy) return;
  try {
    render(await request("/state"));
  } catch {
    // The page can be closed from the terminal at any time.
  }
}

function render(data) {
  state = data;
  feedback.textContent = data.message || "";
  const ended = data.phase === "closed";
  tabs.hidden = ended;
  chatPanel.hidden = ended || data.mode !== "chat";
  blindPanel.hidden = ended || data.mode !== "blind";
  byId("closed").hidden = !ended;
  byId("character").textContent = data.character || "对方";
  byId("model").textContent = data.model || "";
  byId("tab-chat").classList.toggle("active", data.mode === "chat");
  byId("tab-blind").classList.toggle("active", data.mode === "blind");
  if (ended) {
    byId("closed-message").textContent = data.message || "可以关闭这个页面，回到终端。";
  } else if (data.mode === "chat") {
    renderChat(data);
  } else {
    renderBlind(data);
  }
  renderCalibration(data);
  syncPolling();
  applyLocks();
}

async function perform(operation) {
  if (busy) return;
  busy = true;
  setButtonsDisabled(true);
  try {
    await operation();
  } catch (error) {
    feedback.textContent = error instanceof TypeError
      ? "无法连接本机聊天。请检查终端。"
      : error.message;
  } finally {
    busy = false;
    setButtonsDisabled(false);
    applyLocks();
  }
}

document.querySelectorAll("[data-mode]").forEach((button) => {
  button.addEventListener("click", () => {
    if (state?.mode === button.dataset.mode) return;
    perform(async () => render(await request("/mode", {mode: button.dataset.mode})));
  });
});

byId("chat-send").addEventListener("click", () => {
  const text = chatText.value.trim();
  if (!text) {
    feedback.textContent = "请先输入一句话。";
    chatText.focus();
    return;
  }
  perform(async () => {
    feedback.textContent = "正在回复，通常需要几秒。";
    const next = await request("/chat", {text});
    chatText.value = "";
    render(next);
  });
});

chatText.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    byId("chat-send").click();
  }
});

byId("blind-start").addEventListener("click", () => perform(async () => {
  render(await request("/blind/start", {consent: "同意"}));
}));

byId("blind-send").addEventListener("click", () => {
  const text = blindText.value.trim();
  if (!text) {
    feedback.textContent = "请先输入一句话。";
    blindText.focus();
    return;
  }
  perform(async () => {
    feedback.textContent = "正在生成两条回复，通常需要几秒。";
    const next = await request("/blind/turn", {text});
    blindText.value = "";
    render(next);
  });
});

blindText.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    byId("blind-send").click();
  }
});

document.querySelectorAll("[data-vote]").forEach((button) => {
  button.addEventListener("click", () => perform(async () => {
    if (!state?.blind?.round) return;
    render(await request("/blind/vote", {
      round_id: state.blind.round.id,
      vote: button.dataset.vote,
    }));
  }));
});

byId("blind-finish").addEventListener("click", () => perform(async () => {
  render(await request("/blind/finish"));
}));

byId("calibrate").addEventListener("click", () => perform(async () => {
  const next = await request("/calibrate");
  render(next);
  if (next.url) byId("calibration-link").href = next.url;
}));

function finishSession() {
  perform(async () => {
    const next = await request("/finish");
    closed = true;
    clearPrivateView();
    render(next);
  });
}

byId("finish").addEventListener("click", finishSession);

function clearPrivateView() {
  chatText.value = "";
  blindText.value = "";
  log.replaceChildren();
  byId("blind-echo").textContent = "";
  byId("blind-reply-a").textContent = "";
  byId("blind-reply-b").textContent = "";
  byId("blind-progress").textContent = "";
  byId("blind-summary-list").replaceChildren();
}

window.addEventListener("pagehide", () => {
  clearPrivateView();
  if (!closed) {
    closed = true;
    request("/finish", {}, true).catch(() => {});
  }
});

if (token) {
  perform(async () => render(await request("/state")));
} else {
  render({
    phase: "closed",
    mode: "chat",
    character: "对方",
    model: "",
    messages: [],
    blind: null,
    calibration: {active: false, url: null},
    message: "此入口已失效，请回到终端重新打开网页聊天。",
  });
}
