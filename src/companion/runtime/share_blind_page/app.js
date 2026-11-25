"use strict";

const byId = (id) => document.getElementById(id);
const token = location.hash.slice(1);
history.replaceState(null, "", "/");
const consent = byId("consent");
const work = byId("work");
const round = byId("round");
const done = byId("done");
const textarea = byId("text");
const send = byId("send");
const feedback = byId("feedback");
const factPanel = document.createElement("section");
factPanel.setAttribute("aria-label", "确认记忆");
factPanel.hidden = true;
feedback.before(factPanel);
let state;
let busy = false;
let closed = !token;

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
    headers: {"Content-Type": "application/json", "X-Blind-Token": token},
    cache: "no-store",
    credentials: "omit",
    body: JSON.stringify(body),
    keepalive,
  });
  const data = await response.json();
  if (!response.ok) {
    if (!keepalive && data.phase) render(data);
    throw new Error(data.message || "盲评请求失败。");
  }
  return data;
}

function setButtonsDisabled(disabled) {
  document.querySelectorAll("button").forEach((button) => { button.disabled = disabled; });
  if (!disabled && state?.phase === "waiting") {
    send.disabled = true;
  }
}

function applyComposerLock() {
  const locked = busy || state?.phase === "waiting" || Boolean(state?.fact);
  textarea.disabled = locked;
  send.disabled = locked;
}

function renderFact(fact, path) {
  factPanel.replaceChildren();
  factPanel.hidden = !fact;
  if (!fact) return;
  const question = document.createElement("p");
  question.textContent = `请确认事实：${fact.value}`;
  factPanel.append(question);

  const edit = document.createElement("textarea");
  edit.maxLength = 500;
  edit.hidden = true;
  edit.value = fact.value;
  edit.setAttribute("aria-label", "自填事实");
  const confirm = (choice, value) => perform(async () => {
    render(await request(path, {id: fact.id, choice, value}));
  });
  for (const [choice, label] of [
    ["durable", "长期记（需要授权）"], ["temporary", "仅本次"],
    ["skip", "跳过"], ["edit", "自填"],
  ]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => {
      if (choice !== "edit") return confirm(choice);
      if (!edit.hidden) return confirm(choice, edit.value);
      edit.hidden = false;
      button.textContent = "确认自填";
      edit.focus();
    });
    factPanel.append(button);
  }
  factPanel.append(edit);
}

function renderSummary(summary) {
  const list = byId("summary");
  list.replaceChildren();
  if (!summary) return;
  for (const [key, label] of Object.entries(summaryLabels)) {
    if (!(key in summary)) continue;
    const item = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const value = document.createElement("dd");
    value.textContent = String(summary[key]);
    item.append(term, value);
    list.append(item);
  }
}

function clearPrivateView() {
  renderFact(null);
  textarea.value = "";
  byId("reply-a").textContent = "";
  byId("reply-b").textContent = "";
  byId("progress").textContent = "";
}

function renderRound(data) {
  const view = data.round;
  if (!view) {
    round.hidden = true;
    applyComposerLock();
    return;
  }
  round.hidden = false;
  const [a, b] = view.replies;
  byId("reply-a").textContent = a?.text ?? "";
  byId("reply-b").textContent = b?.text ?? "";
  const summary = data.summary || {};
  byId("progress").textContent =
    `已比较 ${summary.comparable_rounds ?? 0} 轮 · 明确选择 ${summary.decisive_votes ?? 0} 轮`;
  applyComposerLock();
  byId("round-title").focus();
}

function renderDone(data) {
  clearPrivateView();
  byId("done-message").textContent = data.message || "已结束盲评。";
  renderSummary(data.summary);
}

function render(data) {
  state = data;
  feedback.textContent = data.message || "";
  renderFact(data.fact, "/fact");
  consent.hidden = data.phase !== "consent";
  work.hidden = !(data.phase === "ready" || data.phase === "waiting");
  done.hidden = !(data.phase === "complete" || data.phase === "closed");
  if (data.phase === "consent") {
    byId("notice").textContent = data.notice || "";
  } else if (data.phase === "ready" || data.phase === "waiting") {
    renderRound(data);
    if (data.phase === "ready" && !busy) textarea.focus();
  } else {
    renderDone(data);
  }
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
    applyComposerLock();
  }
}

byId("start").addEventListener("click", () => perform(async () => {
  render(await request("/start", {consent: "同意"}));
}));

send.addEventListener("click", () => {
  const text = textarea.value.trim();
  if (!text) {
    feedback.textContent = "请先输入一句话。";
    textarea.focus();
    return;
  }
  perform(async () => {
    feedback.textContent = "正在生成两条回复，通常需要几秒。";
    const next = await request("/turn", {text});
    textarea.value = "";
    render(next);
  });
});

textarea.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send.click();
  }
});

document.querySelectorAll("[data-vote]").forEach((button) => {
  button.addEventListener("click", () => perform(async () => {
    if (!state?.round) return;
    render(await request("/vote", {round_id: state.round.id, vote: button.dataset.vote}));
  }));
});

function finishSession() {
  perform(async () => {
    const next = await request("/finish");
    closed = true;
    clearPrivateView();
    render(next);
  });
}

byId("finish").addEventListener("click", finishSession);
byId("leave").addEventListener("click", finishSession);

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
    message: "此入口已失效，请回到终端重新打开盲评网页。",
    summary: null,
  });
}
