# Telegram 多 Bot 签到

青龙脚本：使用一个 Telegram 账号，向多个 bot 发送各自配置的命令，收集回复并通过青龙自带的 `notify.py` 的 `send(title, content)` 汇总通知。

## 依赖

运行环境：Linux / macOS，Python 3.10 及以上。

在青龙「依赖管理 → Python」中安装：

```text
telethon>=1.40,<2
python-dotenv>=1.0,<2
python-socks[asyncio]>=2.0,<3
```

## 配置与登录

1. 在 [my.telegram.org](https://my.telegram.org) 的 API development tools 中创建应用，获取 `api_id` 和 `api_hash`。
2. 将本目录的 `.env.example` 复制为 `.env`，填写配置。也可在青龙环境变量中填写同名变量；青龙环境变量优先。
3. 在青龙容器的交互式终端执行一次登录：

```bash
python3 /ql/data/scripts/qlscripts/telegram/tg_checkin.py --login
```

按提示输入手机号、登录验证码，以及账号开启两步验证时的密码。`--login` 只保存会话。定时任务会复用会话；会话失效时会报错并通知，需要重新在终端登录。

青龙面板中配置的变量不一定会自动进入手动打开的终端。首次登录时可使用脚本同目录的 `.env`，确保登录和定时任务使用相同的 API 凭据及 `TG_SESSION_FILE`。

必填变量：

| 变量 | 说明 |
| --- | --- |
| `TG_API_ID` | Telegram 应用的 API ID |
| `TG_API_HASH` | Telegram 应用的 API hash |
| `TG_BOTS` | bot 配置的 JSON 数组；仅执行 `--login` 时可省略 |

`TG_BOTS` 示例，在青龙环境变量中直接粘贴以下 JSON：

```json
[
  {"bot": "@example_a_bot", "command": "/checkin", "timeout": 30},
  {"bot": "@example_b_bot", "command": "签到", "timeout": 45}
]
```

- `bot`：必填，bot 用户名，`@` 可省略；同一 bot 只配置一次。
- `command`：发送的文本，默认 `/checkin`，支持命令参数及普通文本。
- `timeout`：该 bot 从解析用户名、发送命令到收集回复的总时限，默认 30 秒。

可选变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TG_SESSION_FILE` | `.data/account.session` | 会话文件路径，必须以 `.session` 结尾；相对路径以脚本目录为基准 |
| `TG_REPLY_WAIT` | `3` | 收到回复后，连续多少秒没有新回复或内容更新就结束收集 |
| `TG_PROXY` | 空 | `socks5://主机:端口` 或 `http://主机:端口`，可包含用户名和密码 |

青龙建议将 `TG_SESSION_FILE` 设为 `/ql/data/tg-checkin/account.session`，存放在持久化数据目录中。会话文件包含账号登录凭据，应与 `.env` 一起妥善保管。脚本通过文件锁避免同一会话同时运行多个任务。

运行环境需能连接 Telegram；使用代理时，填写容器内可访问的地址。需要先启动或绑定账号的 bot，请先在 Telegram 中完成准备。

## 青龙任务与通知

任务命令（路径相对于青龙的 `/ql/data/scripts`，按实际拉取目录调整）：

```text
task qlscripts/telegram/tg_checkin.py
```

定时规则：每天 09:30 执行，使用青龙面板的时区。

```text
30 9 * * *
```

每次运行结束调用一次青龙 `notify.py` 的 `send`，通知包含各 bot 的用户名、命令、执行状态及回复原文。通知渠道沿用面板配置。

- 收集本次命令之后该 bot 发来的多条消息；同一条回复被编辑时，保留最后收到的内容。
- 回复文字及媒体说明文字原样保留，图片和附件显示文字标记。通知为文本内容。
- 默认首条回复等待受 30 秒总时限约束；收到后连续 3 秒没有变化便结束。回复间隔较长时，增大 `TG_REPLY_WAIT` 和对应 bot 的 `timeout`。
- 「已收到回复」表示收到了消息，具体签到结果以 bot 原文为准。
- 单个 bot 出错或未回复会继续处理其他 bot；Telegram 限流或账号登录失效时，停止后续发送并通知。
- 配置错误、连接失败、等待超时仍无回复和通知模块调用异常会以非零状态退出。通知渠道的实际投递结果以青龙通知模块日志为准。

找不到 `notify.py` 或调用失败时，汇总内容仍会保留在日志中，并以非零状态退出。脚本自身日志使用 JSON 格式。

## 本地开发

在仓库根目录执行：

```bash
uv venv
uv pip install -r telegram/requirements.txt
cp telegram/.env.example telegram/.env
```

填写 `.env` 后，使用 `.venv/bin/python telegram/tg_checkin.py --login` 登录。

格式与错误检查：

```bash
uvx ruff format telegram/tg_checkin.py
uvx ruff check telegram/tg_checkin.py
```
