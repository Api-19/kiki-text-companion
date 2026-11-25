"use strict";

const byId = (id) => document.getElementById(id);
const token = location.hash.slice(1);
history.replaceState(null, "", "/");
const statusPanel = byId("status");
const work = byId("work");
const fields = byId("fields");
const form = byId("answer");
const start = byId("generate");
const finish = byId("finish");
const confirm = byId("confirm");
const feedback = byId("feedback");
let state;
let jokeMeaning = null;
let busy = false;
let closed = !token;

async function request(path, body = {}, keepalive = false) {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Calibration-Token": token},
    cache: "no-store",
    credentials: "omit",
    body: JSON.stringify(body),
    keepalive,
  });
  const data = await response.json();
  if (!response.ok && !data.phase && data.code !== "note_consent_required") {
    throw new Error(data.message);
  }
  return data;
}

function renderStatus(title, description = "", canGenerate = false) {
  statusPanel.hidden = false;
  work.hidden = true;
  byId("status-title").textContent = title;
  byId("status-description").textContent = description;
  start.hidden = !canGenerate;
}

function fieldset(label) {
  const group = document.createElement("fieldset");
  const legend = document.createElement("legend");
  legend.textContent = label;
  group.append(legend);
  fields.append(group);
  return group;
}

function radio(group, name, value, text) {
  const label = document.createElement("label");
  label.className = "choice";
  const input = document.createElement("input");
  input.type = "radio";
  input.name = name;
  input.value = value;
  input.required = true;
  const caption = document.createElement("span");
  caption.textContent = text;
  label.append(input, caption);
  group.append(label);
  return input;
}

function textField(label, value, limit) {
  const title = document.createElement("label");
  title.htmlFor = "free-answer";
  title.textContent = label;
  const input = document.createElement("textarea");
  input.id = "free-answer";
  input.name = "free-answer";
  input.value = value;
  input.maxLength = limit;
  fields.append(title, input);
  return input;
}

function renderQuestion() {
  statusPanel.hidden = true;
  work.hidden = false;
  const {question, count, index} = state;
  byId("context").textContent = question.context;
  byId("reply").textContent = question.reply;
  const uncertainty = byId("uncertainty");
  uncertainty.hidden = !state.uncertainty;
  uncertainty.textContent = state.uncertainty ? `分析待确认：${state.uncertainty}` : "";
  byId("progress").textContent = `第 ${index + 1} / ${count} 题`;
  const title = byId("question-title");
  fields.replaceChildren();
  confirm.textContent = "确认保存";

  if (question.kind === "fact") {
    title.textContent = question.question;
    const note = textField("事实内容，可更正", question.context, 500);
    note.required = true;
    const group = fieldset("保存范围");
    radio(group, "scope", "durable", "长期记（需要已授权）");
    radio(group, "scope", "temporary", "仅本次聊天");
  } else if (jokeMeaning !== null) {
    title.textContent = "这个梗可以怎么用？";
    const meaning = document.createElement("p");
    meaning.textContent = `「${question.term}」的含义：${jokeMeaning}`;
    fields.append(meaning);
    const group = fieldset("使用边界");
    radio(group, "permission", "understand_only", "仅用于理解");
    radio(group, "permission", "allowed", "可以接");
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "quiet";
    edit.textContent = "返回修改含义";
    edit.addEventListener("click", () => {
      const previous = jokeMeaning;
      jokeMeaning = null;
      renderQuestion();
      fields.querySelector('input[value="free"]').checked = true;
      byId("free-answer").value = previous;
    });
    fields.append(edit);
  } else {
    title.textContent = question.question;
    const group = fieldset(question.kind === "joke" ? "梗的含义" : "你的选择");
    question.options.forEach((option, index) => radio(group, "option", String(index), option));
    const freeChoice = radio(group, "option", "free", "自己填写");
    const input = textField("自填回答", "", 160);
    input.addEventListener("input", () => { freeChoice.checked = true; });
    if (question.kind === "joke") {
      confirm.textContent = "下一步：使用边界";
    } else {
      const preview = document.createElement("p");
      preview.setAttribute("role", "status");
      preview.hidden = true;
      fields.append(preview);
      const updatePreview = () => {
        const answer = selectedAnswer(question, new FormData(form));
        preview.hidden = !answer?.trim();
        preview.textContent = preview.hidden ? "" :
          `仅在与「${question.context}」相似的场景参考。` +
          `对于「${question.question}」，你确认的做法是「${answer}」。`;
      };
      group.addEventListener("change", updatePreview);
      input.addEventListener("input", updatePreview);
    }
  }
  title.focus();
}

function render(data) {
  state = data;
  feedback.textContent = data.message || "";
  if (data.phase === "active") {
    renderQuestion();
  } else if (data.phase === "ready") {
    renderStatus(
      `找到 ${data.count} 个可校准场景`,
      "开始出题会调用已配置的模型并可能计费。模型只收到候选类型和长度分类。确认保存的内容可能随正常聊天发送。",
      true,
    );
  } else if (data.phase === "complete") {
    renderStatus("本轮校准完成", "可以结束网页并返回聊天。");
  } else if (data.phase === "empty") {
    renderStatus("没有找到适合校准的片段", "可以结束网页并返回聊天。");
  } else if (data.phase === "closed") {
    renderStatus("已返回终端聊天", "可以关闭这个页面。");
    finish.hidden = true;
  } else {
    renderStatus("本轮校准不可用", "请返回聊天后再试。");
  }
}

async function perform(operation) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    await operation();
  } catch (error) {
    feedback.textContent = error instanceof TypeError
      ? "无法连接本机聊天。请检查终端。"
      : error.message;
  } finally {
    busy = false;
    document.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  }
}

start.addEventListener("click", () => perform(async () => {
  renderStatus("正在出题", "通常需要几秒，请稍候。");
  render(await request("/generate"));
}));

function selectedAnswer(question, values) {
  const option = values.get("option");
  if (option === null) return "";
  return option === "free" ? values.get("free-answer") : question.options[Number(option)];
}

async function submitAnswer(data) {
  const next = await request("/answer", data);
  if (next.code === "note_consent_required") {
    byId("note-consent")?.remove();
    const group = fieldset("长期记忆授权");
    group.id = "note-consent";
    const explanation = document.createElement("p");
    explanation.textContent = `${next.message} 本次要保存：「${data.answer}」。`;
    const grant = document.createElement("button");
    grant.type = "button";
    grant.textContent = "同意并记住这条事实";
    grant.addEventListener("click", () => perform(() => submitAnswer({...data, authorize: true})));
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "quiet";
    cancel.textContent = "取消授权";
    cancel.addEventListener("click", () => perform(async () => {
      const declined = await request("/answer", {index: data.index, action: "decline"});
      group.remove();
      feedback.textContent = declined.message;
    }));
    group.append(explanation, grant, cancel);
    grant.focus();
    return;
  }
  jokeMeaning = null;
  render(next);
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const {question, index} = state;
  const values = new FormData(form);
  const data = {index, action: "save"};
  if (question.kind === "fact") {
    data.answer = values.get("free-answer");
    data.scope = values.get("scope");
  } else if (jokeMeaning !== null) {
    data.answer = jokeMeaning;
    data.permission = values.get("permission");
  } else {
    data.answer = selectedAnswer(question, values);
    if (!data.answer.trim()) {
      feedback.textContent = "请填写回答，或选择不确定或跳过。";
      return;
    }
    if (question.kind === "joke") {
      jokeMeaning = data.answer;
      feedback.textContent = "";
      renderQuestion();
      return;
    }
  }
  perform(() => submitAnswer(data));
});

for (const action of ["skip", "uncertain"]) {
  byId(action).addEventListener("click", () => perform(async () => {
    const next = await request("/answer", {index: state.index, action});
    jokeMeaning = null;
    render(next);
  }));
}

finish.addEventListener("click", () => perform(async () => {
  const result = await request("/finish");
  closed = true;
  clearPrivateView();
  render(result);
}));

function clearPrivateView() {
  state = undefined;
  jokeMeaning = null;
  fields.replaceChildren();
  byId("context").textContent = "";
  byId("reply").textContent = "";
  byId("question-title").textContent = "";
  byId("uncertainty").textContent = "";
}

window.addEventListener("pagehide", () => {
  clearPrivateView();
  if (!closed) {
    closed = true;
    renderStatus("本轮校准已结束", "请回到终端继续聊天。");
    finish.hidden = true;
    request("/finish", {}, true).catch(() => {});
  }
});

if (token) {
  perform(async () => render(await request("/state")));
} else {
  renderStatus("此入口已失效", "请回到终端输入 /calibrate-web，打开新的地址。");
  finish.hidden = true;
}
