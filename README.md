# XiaoAiPlayer

在 Windows 上轮询小爱音箱的对话，并将语音指令交给可挂载模块。当前模块：Spotify 播放、Windows 已安装软件启动。

## 结构

- `core/`：小爱连接、对话去重和轮询、Jev 语义路由、执行与音箱保护。
- `modules/spotify/`：Spotify API、播放命令和原有规则回退。
- `modules/windows/`：从开始菜单、注册表及常见安装目录发现软件并启动。

## 配置和运行

1. 安装 `requirements.txt` 中的依赖。
2. 复制 `.env.example` 为 `.env`，填写小米账号。需要 Spotify 功能时填写一对 Spotify 凭据；需要 Jev 语义选择时填写 `JEV_API_KEY`。
3. 在 Windows 上运行 `python main.py`，或使用原有开机启动脚本。

`JEV_API_KEY` 未设置、Jev 请求失败或置信度不足时，Spotify 回退到现有解析规则，软件启动只接受已发现软件的精确名称。Jev 正常工作时，它先选模块；对于歌单命令，再从当前 Spotify 歌单列表中选择目标。无论是“播放某歌单”还是“随机播放某歌单”，歌单都以随机模式播放。普通对话和模型明确判为 `none` 的内容不执行。

配置 Jev 后，每条新对话文本都会发送给 Jev 判断所属模块；选择软件或歌单时还会发送对应的候选名称。旧的 `WAKEUP_KEYWORD` 不再拦截命令，以便识别没有固定关键词的软件名或歌单说法。`JEV_MIN_CONFIDENCE` 控制第一层路由的最低置信度；启动软件与选择歌单使用不低于 0.75 的门槛。

Windows 软件清单每 `APP_SCAN_INTERVAL` 秒刷新一次。模型只能返回清单中的候选 ID，程序不会把语音或模型输出当作可执行路径。候选项超过 Jev 单次容量时会分批判断，结果互相冲突则回退精确名称匹配。开始菜单与常规安装目录之外的便携软件需要提供开始菜单快捷方式才能被发现。

运行离线测试：`python -m unittest -q`。
