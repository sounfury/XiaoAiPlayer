// 发送真实测试通知；会让音箱播报。不会输出令牌。
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

const agentDir = process.env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
const config = JSON.parse(readFileSync(join(agentDir, "xiaoai-hook.json"), "utf8"));
const health = await fetch(new URL("/health", config.url), { signal: AbortSignal.timeout(5000) });
if (!health.ok) throw new Error(`健康检查失败 (${health.status})`);
const payload = { source: config.source, event_id: `verify:${randomUUID()}`, text: `${config.source}的任务通知测试成功` };
for (const expected of [202, 200]) {
  const response = await fetch(config.url, {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.token}` },
    body: JSON.stringify(payload), signal: AbortSignal.timeout(5000), redirect: "error",
  });
  const body = await response.json();
  console.log(JSON.stringify({ source: config.source, status: response.status, result: body.status }));
  if (response.status !== expected) throw new Error(`预期 ${expected}，收到 ${response.status}`);
}
