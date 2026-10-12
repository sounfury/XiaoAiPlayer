import test from "node:test";
import assert from "node:assert/strict";
import { attachNotifier, postNotification, isPiWeb, conversationName } from "./xiaoai-hook.js";

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

test("未命名对话使用首条用户消息，清理控制字符并限制为 120 字", () => {
  const ctx = {sessionManager: {getEntries: () => [
    {type: "message", message: {role: "assistant", content: "ignore"}},
    {type: "message", message: {role: "user", content: [{type: "image"}, {type: "text", text: "测试\n通知\u0000"}]}}
  ]}};
  assert.equal(conversationName({}, ctx), "测试 通知");
  assert.equal(conversationName({getSessionName: () => "显式标题"}, ctx), "显式标题");
  ctx.sessionManager.getSessionName = () => "🎵".repeat(150);
  assert.equal(Array.from(conversationName({}, ctx)).length, 120);
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
