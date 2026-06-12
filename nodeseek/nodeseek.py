#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NodeSeek 自动签到（青龙面板版），支持多账号。
签到后调用 /api/account/credit/page-1 读取最新一条积分记录的余额（鸡腿总数）作为成功确认。

环境变量：
  NS_COOKIE          必填。NodeSeek Cookie；多账号用换行或 & 分隔。
  NS_RANDOM          选填，默认 true。true 随机 0–10 个鸡腿；false 固定 5 个。
  NS_NOTIFY_SUCCESS  选填，默认 true。false 时仅在异常时推送通知。

建议 cron：30 8 * * *
"""

import os
import re
import sys

try:
    import requests
except ImportError:
    print("[NodeSeek] 缺少依赖 requests，请在青龙「依赖管理 → Python」中添加 requests 后重试。")
    sys.exit(1)

# 青龙内置通知；本地运行无该模块时回退到 print
try:
    from notify import send
except Exception:
    send = None

BASE = "https://www.nodeseek.com"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.7.2 Mobile/15E148 Safari/604.1")
TIMEOUT = 30


def parse_bool(v, default=True):
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def parse_cookies(raw):
    """多账号用换行或 & 分隔；# 开头的行忽略。"""
    out = []
    for line in re.split(r"[\n&]+", raw or ""):
        c = line.strip()
        if c and not c.startswith("#"):
            out.append(c)
    return out


def headers_for(cookie):
    return {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/board",
        "User-Agent": UA,
        "Cookie": cookie,
    }


def _record_field(rec, idx, *dict_keys):
    """积分记录既可能是数组 [amount, balance, desc, timestamp]，也兼容 dict。"""
    if isinstance(rec, dict):
        for k in dict_keys:
            if k in rec:
                return rec[k]
        return None
    if isinstance(rec, (list, tuple)) and len(rec) > idx:
        return rec[idx]
    return None


def get_balance(session, cookie):
    """取最新一条 credit 记录的 balance（鸡腿余额）。返回 (余额 或 None, 说明)。"""
    try:
        r = session.get(f"{BASE}/api/account/credit/page-1", headers=headers_for(cookie), timeout=TIMEOUT)
    except Exception as e:
        return None, f"网络错误: {e}"
    if r.status_code in (401, 403):
        return None, "Cookie 失效"
    try:
        obj = r.json()
    except Exception:
        return None, f"响应解析失败 ({r.status_code})"
    records = obj.get("data") or []
    if not records:
        return None, "无积分记录"
    try:
        latest = max(records, key=lambda x: _record_field(x, 3, "created_at", "timestamp") or "")
    except Exception:
        latest = records[0]
    bal = _record_field(latest, 1, "balance")
    if bal is None:
        return None, "记录格式异常"
    return bal, ""


def checkin_one(session, cookie, random_mode):
    """对单账号签到并查询余额。返回 (展示行, 是否异常)。"""
    try:
        r = session.post(f"{BASE}/api/attendance?random={'true' if random_mode else 'false'}",
                         headers=headers_for(cookie), data="", timeout=TIMEOUT)
    except Exception as e:
        return f"✗ 网络错误: {e}", True

    status = r.status_code
    obj = {}
    try:
        obj = r.json()
    except Exception:
        pass
    msg = str(obj.get("message") or "").strip()

    if status == 403:
        return f"❌ 403 风控/Cookie 失效 {msg}".strip(), True
    if status == 401 or obj.get("status") == 404:
        return "❌ Cookie 失效，请重新获取", True

    if obj.get("success") or ("鸡腿" in msg):
        note = f"✅ 签到成功 {msg}".strip()
    elif ("已完成签到" in msg) or ("已签到" in msg):
        note = f"今日已签到 {msg}".strip()
    elif 200 <= status < 300:
        note = msg or "签到完成（无 message）"
    else:
        return f"❌ 请求异常 {status} {msg}".strip(), True

    balance, bnote = get_balance(session, cookie)
    if balance is not None:
        return f"{note} | 鸡腿余额 {balance}", False
    return f"{note} | 余额查询失败({bnote})", True


def notify(title, content):
    if send:
        try:
            send(title, content)
        except Exception as e:
            print(f"[NodeSeek] 通知发送失败: {e}")


def main():
    cookies = parse_cookies(os.environ.get("NS_COOKIE", ""))
    if not cookies:
        msg = "未配置 NS_COOKIE"
        print("[NodeSeek]", msg)
        notify("NodeSeek 签到", msg)
        return

    random_mode = parse_bool(os.environ.get("NS_RANDOM"), True)
    notify_success = parse_bool(os.environ.get("NS_NOTIFY_SUCCESS"), True)
    multi = len(cookies) > 1

    print(f"[NodeSeek] 共 {len(cookies)} 个账号待签到（random={random_mode}）")
    session = requests.Session()
    lines = []
    any_problem = False
    for i, cookie in enumerate(cookies, 1):
        line, problem = checkin_one(session, cookie, random_mode)
        full = (f"账号{i}: " if multi else "") + line
        print("[NodeSeek]", full)
        lines.append(full)
        any_problem = any_problem or problem

    if notify_success or any_problem:
        notify("NodeSeek 签到", "\n".join(lines))


if __name__ == "__main__":
    main()
