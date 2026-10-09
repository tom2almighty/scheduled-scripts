#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VMISS 补货监控（青龙面板版）

监控 VMISS 商店分类页的商品库存，商品补货时通过青龙 notify.py 推送通知。
分类页公开可访问、无需登录，脚本只读取库存，不执行任何下单操作。

cron: */5 * * * *
new Env("VMISS 补货监控")

环境变量：
  VMISS_TARGETS     监控目标，JSON 数组；不配置时监控 US.LA.TRI.Basic。
  VMISS_NOTIFY      通知策略，默认 restock。restock 仅补货时通知；always 每次运行都通知。
  VMISS_STATE_FILE  库存状态文件，默认脚本同级 .data/vmiss_stock.json。
  VMISS_TIMEOUT     单次请求超时（秒），默认 20。
  VMISS_PROXY       代理地址，如 http://127.0.0.1:7890，默认不启用。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print(
        "[VMISS] 缺少依赖 requests，请在青龙「依赖管理 → Python」中添加 requests 后重试。"
    )
    sys.exit(1)

BASE = "https://app.vmiss.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_FILE = os.path.join(SCRIPT_DIR, ".data", "vmiss_stock.json")
DEFAULT_TARGETS = [{"store": "us-los-angeles-tri", "plans": ["US.LA.TRI.Basic"]}]
CF_MARKERS = ("Attention Required", "Just a moment", "cf-chl", "cf_chl_opt")


def _load_ql_send():
    """加载青龙 notify.py 的 send 方法。"""
    try:
        from notify import send  # type: ignore

        return send
    except Exception:
        pass

    candidates = [
        os.environ.get("QL_DIR", "") and os.path.join(os.environ["QL_DIR"], "scripts"),
        "/ql/data/scripts",
        "/ql/scripts",
        SCRIPT_DIR,
    ]
    for path in candidates:
        if (
            path
            and os.path.isfile(os.path.join(path, "notify.py"))
            and path not in sys.path
        ):
            sys.path.append(path)
            try:
                from notify import send  # type: ignore

                return send
            except Exception:
                continue
    return None


QL_SEND = _load_ql_send()


def notify(title, content):
    if QL_SEND is None:
        print(f"[VMISS] 未找到青龙通知模块，通知内容：\n【{title}】\n{content}")
        return

    try:
        QL_SEND(title, content)
    except Exception as e:
        print(f"[VMISS] 通知发送失败: {e}")
        print(f"【{title}】\n{content}")


def parse_timeout(raw, default=20):
    try:
        value = int(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return default


def parse_targets(raw):
    """解析 VMISS_TARGETS；空值时返回默认目标。每项含 store 与可选的 plans。"""
    if not raw or not raw.strip():
        return DEFAULT_TARGETS

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("[VMISS] VMISS_TARGETS 不是合法 JSON，改用默认目标")
        return DEFAULT_TARGETS

    if isinstance(data, dict):
        data = [data]

    targets = []
    for item in data:
        if not isinstance(item, dict):
            print(f"[VMISS] 跳过无效目标：{item!r}")
            continue
        store = str(item.get("store") or "").strip()
        if not store:
            print(f"[VMISS] 跳过缺少 store 的目标：{item!r}")
            continue
        plans = item.get("plans") or []
        if isinstance(plans, str):
            plans = re.split(r"[,\n]+", plans)
        plans = [str(p).strip() for p in plans if str(p).strip()]
        targets.append({"store": store, "plans": plans})

    return targets or DEFAULT_TARGETS


def parse_products(html):
    """从分类页 HTML 解析商品卡片。返回 dict 列表。"""
    products = []
    blocks = re.split(r'(?=<div class="package"\s+id="product\d+")', html)[1:]
    for block in blocks:
        pid = re.search(r'id="product(\d+)"', block)
        name = re.search(r'class="package-title">([^<]+)<', block)
        if not (pid and name):
            continue

        qty = None
        qty_match = re.search(r'class="package-qty">\s*([^<]*?)\s*<', block)
        if qty_match:
            number = re.search(r"(\d+)", qty_match.group(1))
            if number:
                qty = int(number.group(1))

        button = re.search(r"<a[^>]*btn-order-now[^>]*>", block)
        if button is None:
            available = qty is not None and qty > 0
            url = None
        else:
            disabled = bool(re.search(r"\bdisabled\b", button.group(0)))
            available = (not disabled) or (qty is not None and qty > 0)
            href = re.search(r'href="([^"]+)"', button.group(0))
            url = href.group(1) if href else None

        if url and url.startswith("/"):
            url = BASE + url

        price = None
        price_match = re.search(r'class="price-amount">\s*([^<]+?)\s*<', block)
        if price_match:
            price = price_match.group(1).strip()

        products.append(
            {
                "id": pid.group(1),
                "name": name.group(1).strip(),
                "qty": qty,
                "available": available,
                "price": price,
                "url": url,
            }
        )
    return products


def fetch_store(session, store, timeout):
    """拉取分类页 HTML，非 200 或命中 Cloudflare 拦截时抛异常。"""
    url = f"{BASE}/store/{store}"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": UA,
    }
    resp = session.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    if any(marker in resp.text for marker in CF_MARKERS):
        raise RuntimeError("被 Cloudflare 拦截")
    return resp.text


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[VMISS] 读取状态文件失败，忽略历史状态：{e}")
    return {}


def save_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[VMISS] 保存状态文件失败：{e}")


def format_restock(info):
    qty = f"{info['qty']} Available" if info["qty"] is not None else "有货"
    lines = [f"[补货] {info['name']}", f"库存: {qty}"]
    if info["price"]:
        lines.append(f"价格: {info['price']}")
    if info["url"]:
        lines.append(f"下单: {info['url']}")
    return "\n".join(lines)


def format_status(info):
    tag = "[有货]" if info["available"] else "[缺货]"
    qty = info["qty"] if info["qty"] is not None else "?"
    return f"{tag} {info['name']} 库存 {qty}"


def main():
    targets = parse_targets(os.environ.get("VMISS_TARGETS"))
    mode = (os.environ.get("VMISS_NOTIFY") or "restock").strip().lower()
    if mode not in ("restock", "always"):
        print(f"[VMISS] VMISS_NOTIFY={mode} 无效，改用 restock")
        mode = "restock"
    timeout = parse_timeout(os.environ.get("VMISS_TIMEOUT"))
    state_file = os.environ.get("VMISS_STATE_FILE") or DEFAULT_STATE_FILE
    proxy = (os.environ.get("VMISS_PROXY") or "").strip()

    state = load_state(state_file)
    prev_products = state.get("products") or {}
    prev_stores = state.get("stores") or {}

    session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    store_names = []
    for target in targets:
        if target["store"] not in store_names:
            store_names.append(target["store"])

    fetched = {}
    for store in store_names:
        try:
            fetched[store] = parse_products(fetch_store(session, store, timeout))
            print(f"[VMISS] /store/{store} 解析到 {len(fetched[store])} 个商品")
        except Exception as e:
            print(f"[VMISS] 拉取 /store/{store} 失败：{e}")

    current_products = {}
    current_stores = {}
    for store in store_names:
        if store not in fetched:
            current_stores[store] = {"ok": False}
            for key, info in prev_products.items():
                if key.startswith(f"{store}/"):
                    current_products[key] = info
            continue
        current_stores[store] = {"ok": True}
        by_name = {p["name"].lower(): p for p in fetched[store]}
        for target in [t for t in targets if t["store"] == store]:
            plans = target["plans"]
            if plans:
                for plan in plans:
                    if plan.lower() not in by_name:
                        print(f"[VMISS] {store} 未找到型号 {plan}，请检查名称")
                selected = [by_name[p.lower()] for p in plans if p.lower() in by_name]
            else:
                selected = fetched[store]
            for product in selected:
                key = f"{store}/{product['name']}"
                current_products[key] = {
                    "name": product["name"],
                    "store": store,
                    "available": product["available"],
                    "qty": product["qty"],
                    "price": product["price"],
                    "url": product["url"],
                }

    restocked = [
        info
        for key, info in current_products.items()
        if info["available"] and prev_products.get(key, {}).get("available") is not True
    ]

    issues = []
    for store, status in current_stores.items():
        prev_ok = prev_stores.get(store, {}).get("ok")
        if not status["ok"] and prev_ok is not False:
            issues.append(f"[异常] /store/{store} 拉取失败，监控中断")
        elif status["ok"] and prev_ok is False:
            issues.append(f"[恢复] /store/{store} 恢复正常")

    if mode == "always":
        blocks = [format_status(info) for info in current_products.values()]
        should_notify = True
    else:
        blocks = [format_restock(info) for info in restocked]
        should_notify = bool(blocks) or bool(issues)

    for info in restocked:
        print(f"[VMISS] 补货：{info['name']} 库存 {info['qty']}")

    if not current_products:
        print("[VMISS] 未匹配到任何监控商品，请检查 VMISS_TARGETS")

    if should_notify:
        content = "\n\n".join(blocks + issues) if blocks else "\n".join(issues)
        notify("VMISS 补货监控", content or "无监控目标")

    save_state(
        state_file,
        {
            "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "products": current_products,
            "stores": current_stores,
        },
    )

    if store_names and not fetched:
        sys.exit(1)


if __name__ == "__main__":
    main()
