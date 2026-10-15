import test from "node:test";
import assert from "node:assert/strict";
import { attachNotifier, postNotification, isPiWeb, conversationName, persistedSessionName } from "./xiaoai-hook.js";

function setup(config = {}) {
  const handlers = new Map();
  const sent = [];
  attachNotifier({ on(name, fn) { handlers.set(name, fn); return () => handlers.delete(name); } }, {
    getConfig: () => ({ enabled: true, source: "Oracle PiWeb", ...config }),
    isPiWeb: () => true,
    makeId: () => "event-1",
    send: async (...args) => sent.push(args),
  });
  const ctx = { sessionManager: { getSessionDir: () => "/home/ubuntu/.pi/agent/sessions/project", getSessionId: () => "session-1" }, hasUI: false };
  return { handlers, sent, ctx };
}

test("只在最终成功结算后发送一次，忽略中间 agent_end", async () => {
  const { handlers, sent, ctx } = setup();
  handlers.get("agent_start")({});
  handlers.get("agent_before_settle")({ outcome: "completed" });
  assert.equal(sent.length, 0);
  await handlers.get("agent_settled")({ aborted: false }, ctx);
  await handlers.get("agent_settled")({ aborted: false }, ctx);
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0][1], { source: "Oracle PiWeb", event_id: "session-1:event-1", task: "当前对话" });
});

test("错误、中止、禁用、子代理和临时会话不播报", async () => {
  for (const variant of ["error", "aborted", "disabled", "subagent", "ephemeral", "memory"]) {
    const { handlers, sent, ctx } = setup({ enabled: variant !== "disabled" });
    if (variant === "subagent") ctx.sessionManager.getSessionDir = () => "C:\\Users\\a\\.pi\\agent\\subagent-sessions\\123";
    if (variant === "ephemeral") ctx.sessionManager.getSessionDir = () => "/home/a/.pi/agent/ephemeral-sessions/123";
    if (variant === "memory") ctx.sessionManager.getSessionDir = () => "";
    handlers.get("agent_start")({});
    handlers.get("agent_before_settle")({ outcome: variant === "error" ? "error" : "completed" });
    await handlers.get("agent_settled")({ aborted: variant === "aborted" }, ctx);
    assert.equal(sent.length, 0, variant);
  }
});

test("没有 before_settle 时仍发送最终通知", async () => {
  const { handlers, sent, ctx } = setup();
  handlers.get("agent_start")({});
  await handlers.get("agent_settled")({ aborted: false }, ctx);
  assert.equal(sent.length, 1);
});

test("错误后自动续跑成功仍然只发送最终通知", async () => {
  const { handlers, sent, ctx } = setup();
  handlers.get("agent_start")({});
  handlers.get("agent_before_settle")({ outcome: "error" });
  handlers.get("agent_start")({});
  handlers.get("agent_before_settle")({ outcome: "completed" });
  await handlers.get("agent_settled")({ aborted: false }, ctx);
  assert.equal(sent.length, 1);
});

test("PiWeb 启动入口识别兼容 Windows CLI 和 Linux server", () => {
  for (const entry of [
    "C:\\Users\\a\\node_modules\\pi-web-ui\\bin\\pi-web-ui.mjs",
    "/home/a/node_modules/pi-web-ui/bin/pi-web-ui.mjs",
    "/opt/pi-web-ui/dist/server/index.js",
    "/opt/pi-web-ui/server/index.js",
  ]) assert.equal(isPiWeb({}, ["node", entry]), true, entry);
  assert.equal(isPiWeb({}, ["node", "/opt/pi/dist/cli.js"]), false);
  assert.equal(isPiWeb({ PI_WEB_PORT: "8787" }, ["node", "app.js"]), true);
});

test("任务结束时发送最新对话名，忽略遗留的插件模板配置", async () => {
  for (const text of [undefined, "自定义播报"]) {
    const { handlers, sent, ctx } = setup({ text });
    let title = "旧名";
    ctx.sessionManager.getSessionName = () => title;
    handlers.get("agent_start")({});
    title = "修复通知";
    handlers.get("agent_before_settle")({ outcome: "completed" });
    await handlers.get("agent_settled")({}, ctx);
    assert.equal(sent[0][1].task, "修复通知");
    assert.equal(Object.hasOwn(sent[0][1], "text"), false);
  }
});

test("未命名对话使用首条用户消息前 30 字，清理控制字符和注入标签", () => {
  let first = [{type: "image"}, {type: "text", text: "测试\n通知\u0000"}];
  const ctx = {sessionManager: {getEntries: () => [
    {type: "message", message: {role: "assistant", content: "ignore"}},
    {type: "message", message: {role: "user", content: first}}
  ]}};
  const none = () => "";
  assert.equal(conversationName({}, ctx, none), "测试 通知");
  assert.equal(conversationName({getSessionName: () => "显式标题"}, ctx, none), "显式标题");
  first.splice(0, 2, {type: "text", text: '<skill name="ssh">\n很长的技能正文\n</skill>\n\n连上服务器' + "啊".repeat(50)});
  assert.equal(conversationName({}, ctx, none), "连上服务器" + "啊".repeat(25));
  first.splice(0, 1, {type: "text", text: "You are an expert coding assistant operating inside pi"});
  assert.equal(conversationName({}, ctx, none), "当前对话");
  ctx.sessionManager.getSessionName = () => "🎵".repeat(150);
  assert.equal(Array.from(conversationName({}, ctx, none)).length, 80);
});

test("优先使用会话文件中最新的 session_info（PiWeb 重命名不更新内存）", () => {
  const lines = [
    JSON.stringify({type: "session_info", name: "旧标题"}),
    JSON.stringify({type: "message", message: {role: "user", content: "提到 \"session_info\" 的消息"}}),
    "{半行损坏 \"session_info\"",
    JSON.stringify({type: "session_info", name: "新标题"}),
  ].join("\n");
  assert.equal(persistedSessionName("/s.jsonl", () => lines), "新标题");
  assert.equal(persistedSessionName(undefined), "");
  assert.equal(persistedSessionName("/missing", () => { throw new Error("ENOENT"); }), "");
  const ctx = {sessionManager: {getSessionFile: () => "/s.jsonl", getSessionName: () => "内存旧名"}};
  assert.equal(conversationName({}, ctx, (file) => persistedSessionName(file, () => lines)), "新标题");
});

test("重试携带同一事件 ID 和 Bearer 令牌", async () => {
  const requests = [];
  await postNotification({ url: "http://127.0.0.1:8765/hooks/task-completed", token: "test-key" },
    { source: "PiWeb", event_id: "same-event" }, async (_url, init) => {
      requests.push(init);
      return requests.length === 1
        ? { ok: false, status: 503, json: async () => ({ ok: false }) }
        : { ok: true, status: 202, json: async () => ({ status: "queued" }) };
    });
  assert.equal(requests.length, 2);
  assert.equal(requests[0].body, requests[1].body);
  assert.equal(requests[1].headers.Authorization, "Bearer test-key");
});

test("认证失败不反复重试", async () => {
  let count = 0;
  await assert.rejects(postNotification({ url: "http://127.0.0.1:8765/hooks/task-completed", token: "bad" }, {}, async () => {
    count++;
    return { ok: false, status: 401, json: async () => ({ ok: false }) };
  }), /401/);
  assert.equal(count, 1);
});
