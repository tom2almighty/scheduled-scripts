#!/usr/bin/env python3
"""
Telegram 多 Bot 签到。

cron: 30 9 * * *
new Env("Telegram 多 Bot 签到")
"""

import argparse
import asyncio
import fcntl
import json
import logging
import math
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("tg_checkin")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="seconds"
            ),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def positive_seconds(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是正数") from None
    if isinstance(value, bool) or not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} 必须是有限正数")
    return number


def load_bots(raw):
    try:
        bots = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("TG_BOTS 必须是 JSON 数组") from None
    if not isinstance(bots, list) or not bots:
        raise ValueError("TG_BOTS 必须是非空 JSON 数组")

    result = []
    seen = set()
    for index, item in enumerate(bots, 1):
        prefix = f"TG_BOTS 第 {index} 项"
        if not isinstance(item, dict) or item.keys() - {"bot", "command", "timeout"}:
            raise ValueError(f"{prefix} 必须是对象，字段仅支持 bot、command、timeout")
        username = item.get("bot", "")
        if not isinstance(username, str) or not re.fullmatch(
            r"@?[A-Za-z][A-Za-z0-9_]{4,31}", username
        ):
            raise ValueError(f"{prefix} 的 bot 必须是 Telegram bot 用户名")
        username = "@" + username.lstrip("@").lower()
        if username in seen:
            raise ValueError(f"{prefix} 的 bot 重复：{username}")
        seen.add(username)
        command = item.get("command", "/checkin")
        if not isinstance(command, str) or not command.strip() or len(command) > 4096:
            raise ValueError(f"{prefix} 的 command 必须是 1～4096 字符的非空文本")
        result.append(
            {
                "bot": username,
                "command": command,
                "timeout": positive_seconds(
                    item.get("timeout", 30), f"{prefix} timeout"
                ),
            }
        )
    return result


def load_proxy(raw):
    if not raw:
        return None
    try:
        url = urlsplit(raw)
        if (
            url.scheme not in {"socks5", "http"}
            or not url.hostname
            or not url.port
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError
        return {
            "proxy_type": url.scheme,
            "addr": url.hostname,
            "port": url.port,
            "username": unquote(url.username) if url.username else None,
            "password": unquote(url.password) if url.password else None,
            "rdns": True,
        }
    except ValueError:
        raise ValueError(
            "TG_PROXY 格式应为 socks5://主机:端口 或 http://主机:端口"
        ) from None


def load_config(login):
    try:
        api_id = int(os.environ.get("TG_API_ID", ""))
    except ValueError:
        raise ValueError("TG_API_ID 必须是正整数") from None
    if api_id <= 0:
        raise ValueError("TG_API_ID 必须是正整数")
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("TG_API_HASH 必须是 my.telegram.org 提供的 32 位十六进制值")
    session = Path(
        os.environ.get("TG_SESSION_FILE", "").strip() or ".data/account.session"
    ).expanduser()
    if not session.is_absolute():
        session = SCRIPT_DIR / session
    if session.suffix != ".session":
        raise ValueError("TG_SESSION_FILE 必须以 .session 结尾")
    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "session": session.resolve(),
        "proxy": load_proxy(os.environ.get("TG_PROXY", "").strip()),
        "reply_wait": positive_seconds(
            os.environ.get("TG_REPLY_WAIT", "3"), "TG_REPLY_WAIT"
        ),
        "bots": [] if login else load_bots(os.environ.get("TG_BOTS", "")),
    }


@contextmanager
def session_lock(session):
    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with session.with_suffix(".session.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("同一会话已有任务运行，请等待其结束") from None
        yield


def _load_ql_send():
    """沿用 NodeSeek 脚本的青龙 notify.py 加载方式。"""
    try:
        from notify import send

        return send
    except Exception:
        LOGGER.debug("默认导入路径未能加载青龙通知模块，继续查找", exc_info=True)

    candidates = [
        os.environ.get("QL_DIR", "") and os.path.join(os.environ["QL_DIR"], "scripts"),
        "/ql/data/scripts",
        "/ql/scripts",
        str(SCRIPT_DIR),
    ]
    for path in candidates:
        if (
            path
            and os.path.isfile(os.path.join(path, "notify.py"))
            and path not in sys.path
        ):
            sys.path.append(path)
            try:
                from notify import send

                return send
            except Exception:
                LOGGER.debug(
                    "从 %s 加载青龙通知模块失败，继续查找", path, exc_info=True
                )
    return None


def send_notification(content):
    LOGGER.info("运行汇总：\n%s", content)
    send = _load_ql_send()
    if send is None:
        LOGGER.error("未找到可用的青龙通知模块 notify.py，通知内容已保留在日志中")
        return False
    try:
        send("Telegram 多 Bot 签到", content)
        LOGGER.info("已调用青龙 notify.py 的 send 方法")
        return True
    except Exception:
        LOGGER.exception("青龙通知调用失败")
        return False


def reply_text(message):
    parts = [message.raw_text] if message.raw_text else []
    if message.photo:
        parts.append("[图片]")
    elif message.document:
        parts.append("[附件]")
    elif message.media and not parts:
        parts.append("[媒体消息]")
    return "\n".join(parts) or "[空消息]"


async def send_and_collect(client, bot, reply_wait, result):
    from telethon import events

    entity = await client.get_entity(bot["bot"])
    if not getattr(entity, "bot", False):
        raise ValueError("目标用户名对应的账号不是 bot")
    queue = asyncio.Queue()

    async def receive(event):
        queue.put_nowait(event.message)

    # 先注册监听，避免遗漏发送请求返回前就到达的回复。
    client.add_event_handler(receive, events.NewMessage(chats=entity, incoming=True))
    client.add_event_handler(receive, events.MessageEdited(chats=entity, incoming=True))
    try:
        sent = await client.send_message(
            entity, bot["command"], parse_mode=None, link_preview=False
        )
        result["sent"] = True
        LOGGER.info("%s 已发送命令：%s", bot["bot"], bot["command"])
        loop = asyncio.get_running_loop()
        quiet_deadline = None
        while True:
            wait = (
                None if quiet_deadline is None else max(0, quiet_deadline - loop.time())
            )
            try:
                message = await asyncio.wait_for(queue.get(), timeout=wait)
            except asyncio.TimeoutError:
                return
            if message.id <= sent.id:
                continue
            text = reply_text(message)
            if result["replies"].get(message.id) != text:
                result["replies"][message.id] = text
                quiet_deadline = loop.time() + reply_wait
    finally:
        client.remove_event_handler(receive)


def format_result(bot, result, status):
    lines = [bot["bot"], f"命令：{bot['command']}", f"状态：{status}"]
    for index, message_id in enumerate(sorted(result["replies"]), 1):
        lines.append(f"回复 {index}：\n{result['replies'][message_id]}")
    return "\n".join(lines)


async def run(config, login, reports):
    from telethon import TelegramClient, errors

    client = TelegramClient(
        str(config["session"]),
        config["api_id"],
        config["api_hash"],
        proxy=config["proxy"],
        timeout=10,
        connection_retries=2,
        request_retries=1,
        flood_sleep_threshold=0,
        raise_last_call_error=True,
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        if login:
            await client.start()
        elif not await asyncio.wait_for(client.is_user_authorized(), timeout=30):
            raise RuntimeError("会话未登录或已失效，请在终端执行 tg_checkin.py --login")
        user = await asyncio.wait_for(client.get_me(), timeout=30)
        if user is None or user.bot:
            raise RuntimeError("需要 Telegram 个人账号会话，请使用手机号登录")
        if login:
            LOGGER.info("登录完成，会话已保存：%s", config["session"])
            return True

        ok = True
        stop_reason = None
        for bot in config["bots"]:
            result = {"sent": False, "replies": {}}
            if stop_reason:
                reports.append(format_result(bot, result, f"未执行：{stop_reason}"))
                continue
            try:
                await asyncio.wait_for(
                    send_and_collect(client, bot, config["reply_wait"], result),
                    timeout=bot["timeout"],
                )
                status = "已收到回复"
            except asyncio.TimeoutError:
                if result["replies"]:
                    status = "已收到回复，达到本 bot 的等待上限"
                else:
                    ok = False
                    status = (
                        "已发送，等待回复超时"
                        if result["sent"]
                        else "执行超时，命令是否送达请在 Telegram 中确认"
                    )
            except errors.FloodWaitError as exc:
                ok = False
                stop_reason = f"Telegram 限流，需要等待 {exc.seconds} 秒"
                status = stop_reason
            except errors.UnauthorizedError:
                ok = False
                stop_reason = "账号登录失效，请重新执行 --login"
                status = stop_reason
            except (errors.RPCError, OSError, ValueError) as exc:
                ok = False
                status = f"失败：{type(exc).__name__}: {exc}"
            report = format_result(bot, result, status)
            reports.append(report)
            LOGGER.info("%s", report)
        return ok
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="单账号向多个 Telegram bot 发命令并汇总回复"
    )
    parser.add_argument("--login", action="store_true", help="交互登录并保存会话")
    args = parser.parse_args()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("telethon").setLevel(logging.WARNING)
    os.umask(0o077)
    reports = []
    ok = False
    try:
        from dotenv import load_dotenv

        load_dotenv(SCRIPT_DIR / ".env", override=False, interpolate=False)
        config = load_config(args.login)
        if args.login and not sys.stdin.isatty():
            raise RuntimeError("--login 需要交互式终端，请在终端运行")
        with session_lock(config["session"]):
            ok = asyncio.run(run(config, args.login, reports))
    except Exception as exc:
        error = f"运行失败：{type(exc).__name__}: {exc}"
        LOGGER.exception("%s", error)
        reports.append(error)
    if not args.login and not send_notification("\n\n".join(reports)):
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
