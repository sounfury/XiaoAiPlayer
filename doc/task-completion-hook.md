# 小爱任务完成通知

XiaoAiPlayer 提供通用 HTTP Hook。PiWeb 只负责在 Pi 最终结算成功时发送事件，其他程序也可以接同一个接口。

```text
本机 PiWeb ── HTTP ──────────────┐
                               ├── XiaoAiPlayer /hooks/task-completed ── 小爱播报
Oracle PiWeb ── SSH 反向隧道 ───┘
```

## 配置

先确认小米账号已登录，XiaoAiPlayer 可正常连接音箱。接入本机和 SSH 配置里的 `oracle`：

```bash
python integrations/pi/install.py --ssh-host oracle
```

仅接本机时运行 `python integrations/pi/install.py`。安装器生成或复用 `.env` 中的随机令牌，把事件扩展及配置安装到两边的 `~/.pi/agent`。远端需要可无交互登录的 SSH Host 别名，`ssh`、`scp` 必须可用。Windows 安装了 Git 时优先使用 Git 自带的 SSH。自定义端口使用 `--port` 和 `--remote-port`，未指定时复用已有 `.env` 端口。

当前这台电脑和 Oracle 已部署在 **28765** 端口；下面配置示例展示新安装的默认端口 8765。

重启 XiaoAiPlayer，在已有 PiWeb 会话执行 `/reload`，或者新建会话。PiWeb 服务本身无需重启。代码使用当前 Pi SDK 的 `agent_before_settle` 与 `agent_settled` 事件；旧版 Pi 如果没有这两个事件，需要先升级。

服务配置示例（真实令牌请由安装器生成）：

```dotenv
HOOK_ENABLED=true
HOOK_HOST=127.0.0.1
HOOK_PORT=8765
HOOK_TOKEN=替换为随机令牌
HOOK_TEXT="{source}提醒你，{task}已经完成啦"
HOOK_SSH_HOST=oracle
HOOK_SSH_REMOTE_PORT=8765
```

`HOOK_SSH_HOST` 留空则关闭隧道。隧道由小爱进程维护，断线每 5 秒重试，仅在远端 `127.0.0.1` 监听；电脑关机期间远端无法通知。

Pi 配置位于 `~/.pi/agent/xiaoai-hook.json`（支持 `PI_CODING_AGENT_DIR`），只管理通知开关、接口、认证和来源，每次通知都会重新读取：

```json
{
  "enabled": true,
  "piwebOnly": true,
  "url": "http://127.0.0.1:8765/hooks/task-completed",
  "token": "与 HOOK_TOKEN 相同的令牌",
  "source": "本机 PiWeb"
}
```

Pi 扩展只发送 `source`、`task` 和 `event_id`，不拼接播报文字。任务结束时读取最新会话名称；未命名时取首条用户消息，清理换行和控制字符并限制为 120 字，没有文字时使用「当前对话」。名称以 `task` 字段发送，不发送助手回复。旧插件配置中的 `text` 已停用，即使保留也不会发送。

**修改播报模板：** 只需编辑 XiaoAiPlayer 项目 `.env` 的 `HOOK_TEXT`，本机和 Oracle 通知共用这一份模板。修改后重启 XiaoAiPlayer 生效，无需修改两边 Pi 配置。首次升级扩展代码后，已有 PiWeb 会话需执行一次 `/reload`。

```dotenv
HOOK_TEXT="{source}提醒你，{task}已经完成啦"
```

支持 `{task}`（对话名）、`{source}`（来源）、`{event_id}`（事件编号），也可设成固定文字，例如 `HOOK_TEXT="任务完成啦"`。模板及替换后的文字均最多 300 字，不能为空；模板变量不合法会在服务启动时提示错误。变量值只替换一次，对话名中的花括号不会再次展开。

通用 Hook 仍允许其他调用方显式提供 `text` 覆盖模板；Pi 插件不提供该字段，统一由服务端渲染。

`piwebOnly` 默认 `true`，兼容 `bin/pi-web-ui.mjs` 和 `server/index.js` 启动入口；设为 `false` 也可通知 Pi 终端主会话。`source` 用来区分来源，可通过服务端模板决定是否读出。设置 `enabled: false` 立即停用该来源。关闭整个接收服务需在 `.env` 设置 `HOOK_ENABLED=false` 后重启。

## 通用事件协议

`POST /hooks/task-completed`，要求 `Content-Type: application/json` 和 `Authorization: Bearer <HOOK_TOKEN>`：

```json
{
  "source": "Oracle PiWeb",
  "event_id": "session-id:unique-completion-id",
  "task": "可选的任务名称",
  "text": "可选的自定义播报文本"
}
```

`source`（最多 80 字）与 `event_id`（最多 200 字）必填；`task` 最多 120 字，`text` 最多 300 字，且只接受这些字段。每次完成用新 ID，重试复用同一个 ID。服务在内存中按 `(source, event_id)` 去重，成功事件保留 1 小时，最多保存 4096 条，队列最多 64 条。

- `202 {"status":"queued"}`：已入队，音箱会依次播报。
- `200 {"status":"duplicate"}`：已接收过该事件。
- `400`：字段或文本不合法；`401`：令牌无效。
- `503`：队列或去重缓存已满，可稍后重试。
- `GET /health`：仅回环地址可访问，返回 `{"status":"ok"}`。

Pi 扩展对网络错误和服务繁忙最多尝试 3 次。`202` 表示成功入队，音箱播放失败会记入小爱日志并允许相同事件再次提交。队列和去重缓存重启后清空，不提供离线持久投递。

## 触发与声音行为

只在 Pi 的最终成功结算后通知，中间工具调用、自动续跑、子代理、临时会话、错误和用户中止均不通知。

小爱音箱 Play 增强版（L05C）使用 MIoT `intelligent-speaker.play-text`（`siid=5, aiid=3`）播报；该型号的 MiNA `text_to_speech` 会返回成功但不出声。实测 `Speaker.Mute` 与旧媒体队列暂停联动：`Mute=False` 会恢复旧音乐，`player_pause` 又会把它改为 `True`。MIoT 播放文本可在媒体静音/暂停时独立发声，因此 L05C 通知保持旧队列暂停，不解除媒体静音，也不改变电脑的 Spotify 播放。普通 TTS 同样使用 MIoT，但电脑媒体保护期间仍跳过普通回复。

每次通知都读取实际音量和静音状态，避免仅依赖重启后清空的内存标记。音量为 0 时调到 20，非零音量保持原值。其他型号仍使用 MiNA，播报前解除静音并确认生效，结束后恢复原有静音或媒体保护。

L05C 播报时的播放器状态仍显示旧音乐已暂停，不能据此判断语音结束。当前从 TTS 指令返回后保留至少 12 秒、或“文字数 / 3 + 10 秒”的播报窗口，再暂停旧队列并恢复保护；300 字不再被旧的 80 秒上限截断，worker 总超时为 180 秒。日志 `Notification speech window completed (audibility not measured)` 只表示指令及等待流程完成，实际声音仍需现场确认。

## 验证

```bash
python -m unittest -q
node --test integrations/pi/xiaoai-hook.test.mjs
```

健康检查使用配置中的端口，当前本机和 Oracle 均为 `http://127.0.0.1:28765/health`。远端无响应时，先检查 XiaoAiPlayer 是否运行，再检查日志中的 SSH 隧道错误。

运行 `node integrations/pi/verify.mjs` 可发送真实播报测试，并验证同一事件重复提交返回 `200`。此命令会让音箱发声。
