import { appendFileSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

function agentDir() {
  return process.env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
}

function debug(event, details = {}) {
  try {
    appendFileSync(join(agentDir(), "xiaoai-hook.log"), `${JSON.stringify({
      t: new Date().toISOString(), event, ...details,
    })}\n`);
  } catch {}
}

export function readConfig() {
  try {
    return JSON.parse(readFileSync(join(agentDir(), "xiaoai-hook.json"), "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return {};
    throw new Error("无法读取 xiaoai-hook.json，请检查 JSON 格式");
  }
}

export function isPiWeb(env = process.env, argv = process.argv) {
  if (env.PI_WEB_LAUNCHED_BY || env.PI_WEB_PORT) return true;
  return argv.slice(1).some((arg) => /(?:^|[\\/])pi-web-ui[\\/](?:bin[\\/]pi-web-ui\.mjs|(?:dist[\\/])?server[\\/]index\.js)$/.test(arg));
}

// PiWeb 重命名会另开 SessionManager 写 session_info，运行中会话的内存状态看不到，
// 因此以会话文件里最后一条 session_info 为准。
export function persistedSessionName(file, read = readFileSync) {
  if (!file) return "";
  let name = "";
  try {
    for (const line of read(file, "utf8").split("\n")) {
      if (!line.includes('"session_info"')) continue;
      try {
        const entry = JSON.parse(line);
        if (entry.type === "session_info" && typeof entry.name === "string") name = entry.name;
      } catch {}
    }
  } catch {}
  return name;
}

export function conversationName(pi, ctx, readName = persistedSessionName) {
  const clean = (value, max = 80) => typeof value === "string"
    ? Array.from(value.replace(/[\p{Cc}\p{Cf}]/gu, " ").replace(/\s+/g, " ").trim()).slice(0, max).join("")
    : "";
  let file;
  try { file = ctx.sessionManager.getSessionFile?.(); } catch {}
  const name = clean(readName(file)) || clean(ctx.sessionManager.getSessionName?.()) || clean(pi.getSessionName?.());
  if (name) return name;
  // 与 PiWeb 未命名会话的显示方式一致：首条用户消息前 30 字，去掉注入的 <skill> 等标签块。
  const first = ctx.sessionManager.getEntries?.().find((entry) =>
    entry.type === "message" && entry.message?.role === "user");
  const content = first?.message?.content;
  const text = (typeof content === "string" ? content :
    Array.isArray(content) ? content.filter((part) => part.type === "text").map((part) => part.text).join(" ") : "")
    .replace(/<([\w-]+)(?:\s[^>]*)?>[\s\S]*?<\/\1>/g, " ");
  if (/^\s*You are an? /i.test(text)) return "当前对话";
  return clean(text, 30) || "当前对话";
}

export async function postNotification(config, payload, fetchImpl = fetch) {
  const endpoint = new URL(config.url);
  if (!["http:", "https:"].includes(endpoint.protocol)) throw new Error("通知 URL 必须使用 HTTP 或 HTTPS");
  if (!config.token || typeof config.token !== "string") throw new Error("缺少通知令牌");
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const response = await fetchImpl(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.token}` },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(4000),
        redirect: "error",
      });
      const body = await response.json();
      if (response.ok && ["queued", "duplicate"].includes(body.status)) return;
      if (response.status < 500 && response.status !== 429) {
        throw Object.assign(new Error(`通知接口拒绝请求 (${response.status})`), { permanent: true });
      }
      throw new Error(`通知接口暂时不可用 (${response.status})`);
    } catch (error) {
      // 隧道没连（对端没开机）时本地端口无人监听：属于正常离线，不重试。
      if (error.cause?.code === "ECONNREFUSED") throw Object.assign(error, { offline: true });
      if (error.permanent || attempt === 2) throw error;
      await new Promise((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
    }
  }
}

export function attachNotifier(pi, options = {}) {
  const getConfig = options.getConfig || readConfig;
  const detectPiWeb = options.isPiWeb || isPiWeb;
  const send = options.send || postNotification;
  const makeId = options.makeId || randomUUID;
  let outcome;
  let active = false;
  const offStart = pi.on("agent_start", () => {
    active = true;
    outcome = undefined;
    debug("agent_start");
  });
  const offBoundary = pi.on("agent_before_settle", (event) => {
    outcome = event.outcome;
    debug("agent_before_settle", { outcome });
  });
  const offSettled = pi.on("agent_settled", async (event, ctx) => {
    const aborted = !!event?.aborted;
    const current = outcome;
    debug("agent_settled", { active, aborted, outcome: current });
    if (!active) return;
    active = false;
    // 未收到 before_settle 时按成功处理；error/aborted 仍跳过。
    if (aborted || (current && current !== "completed")) return;
    let sessionDir = "";
    let sessionId = "unknown";
    try {
      sessionDir = ctx.sessionManager.getSessionDir() || "";
      sessionId = ctx.sessionManager.getSessionId() || "unknown";
    } catch (error) {
      debug("session_error", { name: error.name });
      return;
    }
    // PiWeb 的子代理和临时补全有独立目录，不能重复播报主任务。
    if (!sessionDir || /(?:^|[\\/])(?:subagent|ephemeral)-sessions(?:[\\/]|$)/.test(sessionDir)) {
      debug("skip_session", { kind: sessionDir ? "ephemeral" : "memory" });
      return;
    }
    try {
      const config = getConfig();
      const piweb = detectPiWeb();
      if (config.enabled !== true || (config.piwebOnly !== false && !piweb)) {
        debug("skip_config", { enabled: config.enabled === true, piweb });
        return;
      }
      const source = config.source || "PiWeb";
      const task = conversationName(pi, ctx);
      // 插件只报告事件；播报模板由 XiaoAiPlayer 的 HOOK_TEXT 统一管理。
      const payload = { source, event_id: `${sessionId}:${makeId()}`, task };
      await send(config, payload);
      debug("sent", { source });
    } catch (error) {
      if (error.offline) {
        debug("skip_offline");
        return;
      }
      debug("send_failed", { name: error.name });
      console.error("[xiaoai-hook] 完成通知发送失败，请检查接口、令牌和 SSH 隧道", error.name);
      try {
        if (ctx.hasUI) ctx.ui.notify("小爱完成通知发送失败，请检查通知接口和 SSH 隧道", "warning");
      } catch {}
    }
  });
  return () => { offStart(); offBoundary(); offSettled(); };
}

export default function (pi) {
  debug("loaded", { piweb: isPiWeb() });
  attachNotifier(pi);
}
