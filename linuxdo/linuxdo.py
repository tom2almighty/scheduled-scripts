"""
cron: 30 9 * * *
new Env("Linux.Do 签到")
"""

import os
import random
import re
import time
import functools
from html import unescape
from DrissionPage import ChromiumOptions, Chromium
from curl_cffi import requests

try:
    from loguru import logger
except ImportError:
    class _FallbackLogger:
        def _log(self, level, message):
            print(f"{time.strftime('%H:%M:%S')} | {level:<7} | {message}")

        def info(self, message):
            self._log("INFO", message)

        def success(self, message):
            self._log("SUCCESS", message)

        def warning(self, message):
            self._log("WARNING", message)

        def error(self, message):
            self._log("ERROR", message)

    logger = _FallbackLogger()

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from tabulate import tabulate
except ImportError:
    tabulate = None


def retry_decorator(retries=3, min_delay=5, max_delay=10):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries - 1:  # 最后一次尝试
                        logger.error(f"函数 {func.__name__} 最终执行失败: {str(e)}")
                    logger.warning(
                        f"函数 {func.__name__} 第 {attempt + 1}/{retries} 次尝试失败: {str(e)}"
                    )
                    if attempt < retries - 1:
                        sleep_s = random.uniform(min_delay, max_delay)
                        logger.info(
                            f"将在 {sleep_s:.2f}s 后重试 ({min_delay}-{max_delay}s 随机延迟)"
                        )
                        time.sleep(sleep_s)
            return None

        return wrapper

    return decorator


os.environ.pop("DISPLAY", None)
os.environ.pop("DYLD_LIBRARY_PATH", None)

USERNAME = os.environ.get("LINUXDO_USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD")
COOKIES = os.environ.get("LINUXDO_COOKIES", "").strip()  # 手动设置的 Cookie 字符串，优先使用
COOKIE_FILE = os.environ.get("LINUXDO_COOKIE_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "linuxdo_cookie.txt"
)
SAVE_COOKIE_ENABLED = os.environ.get("LINUXDO_SAVE_COOKIE", "true").strip().lower() not in [
    "false",
    "0",
    "off",
]
BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in [
    "false",
    "0",
    "off",
]
if not USERNAME:
    USERNAME = os.environ.get("USERNAME")
if not PASSWORD:
    PASSWORD = os.environ.get("PASSWORD")

HOME_URL = "https://linux.do/"
LOGIN_URL = "https://linux.do/login"
SESSION_URL = "https://linux.do/session"
CSRF_URL = "https://linux.do/session/csrf"
CONNECT_URL = "https://connect.linux.do/"


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
        os.path.dirname(os.path.abspath(__file__)),
    ]
    import sys

    for path in candidates:
        if path and os.path.isfile(os.path.join(path, "notify.py")) and path not in sys.path:
            sys.path.append(path)
            try:
                from notify import send  # type: ignore

                return send
            except Exception:
                continue
    return None


QL_SEND = _load_ql_send()


def ql_notify(title: str, content: str):
    if QL_SEND is None:
        logger.warning("未找到青龙通知模块 notify.py，以下为通知内容（仅打印）：")
        logger.info(f"\n【{title}】\n{content}")
        return

    try:
        QL_SEND(title, content)
        logger.success("通知已发送")
    except Exception as e:
        logger.error(f"通知发送失败: {e}")
        logger.info(f"\n【{title}】\n{content}")


def read_saved_cookie() -> str:
    try:
        if os.path.isfile(COOKIE_FILE):
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception as e:
        logger.warning(f"读取本地 Cookie 失败: {e}")
    return ""


def write_saved_cookie(cookie_str: str):
    if not SAVE_COOKIE_ENABLED or not cookie_str:
        return
    try:
        os.makedirs(os.path.dirname(COOKIE_FILE) or ".", exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie_str)
        try:
            os.chmod(COOKIE_FILE, 0o600)
        except Exception:
            pass
        logger.info(f"Cookie 已保存到 {COOKIE_FILE}")
    except Exception as e:
        logger.warning(f"保存 Cookie 失败: {e}")


class LinuxDoBrowser:
    def __init__(self) -> None:
        from sys import platform

        if platform == "linux" or platform == "linux2":
            platformIdentifier = "X11; Linux x86_64"
        elif platform == "darwin":
            platformIdentifier = "Macintosh; Intel Mac OS X 10_15_7"
        elif platform == "win32":
            platformIdentifier = "Windows NT 10.0; Win64; x64"
        else:
            platformIdentifier = "X11; Linux x86_64"

        co = (
            ChromiumOptions()
            .headless(True)
            .incognito(True)
            .set_argument("--no-sandbox")
        )
        co.set_user_agent(
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        self.browser = Chromium(co)
        self.page = self.browser.new_tab()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )

    @staticmethod
    def parse_cookie_string(cookie_str: str) -> list[dict]:
        """
        解析浏览器复制的 Cookie 字符串格式: "name1=value1; name2=value2"
        返回 DrissionPage 所需的 cookie 列表格式。
        """
        cookies = []
        for part in cookie_str.strip().split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                cookies.append(
                    {
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".linux.do",
                        "path": "/",
                    }
                )
        return cookies

    def current_cookie_string(self) -> str:
        cookies = self.session.cookies.get_dict()
        return "; ".join(f"{name}={value}" for name, value in cookies.items() if name and value)

    def save_current_cookies(self):
        cookie_str = self.current_cookie_string()
        if cookie_str:
            write_saved_cookie(cookie_str)

    def login_with_cookies(self, cookie_str: str) -> bool:
        """使用手动设置的 Cookie 直接登录，跳过账号密码流程"""
        logger.info("检测到手动 Cookie，尝试 Cookie 登录...")
        dp_cookies = self.parse_cookie_string(cookie_str)
        if not dp_cookies:
            logger.error("Cookie 解析失败或为空，无法使用 Cookie 登录")
            return False

        logger.info(f"成功解析 {len(dp_cookies)} 个 Cookie 条目")

        # 同步到 requests.Session，以便后续 Connect 请求使用
        for ck in dp_cookies:
            self.session.cookies.set(ck["name"], ck["value"], domain="linux.do")

        # 同步到 DrissionPage
        self.page.set.cookies(dp_cookies)
        logger.info("Cookie 设置完成，导航至 linux.do...")
        self.page.get(HOME_URL)
        time.sleep(5)

        # 验证登录状态
        try:
            user_ele = self.page.ele("@id=current-user")
        except Exception as e:
            logger.warning(f"Cookie 登录验证异常: {str(e)}")
            return False
        if not user_ele:
            logger.error("Cookie 登录验证失败 (未找到 current-user)，Cookie 可能已过期")
            return False
        else:
            logger.info("Cookie 登录验证成功")
            self.save_current_cookies()
            return True

    def login(self):
        logger.info("开始账号密码登录")
        # Step 1: Get CSRF Token
        logger.info("获取 CSRF token...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_URL,
        }
        resp_csrf = self.session.get(CSRF_URL, headers=headers, impersonate="firefox135")
        if resp_csrf.status_code != 200:
            logger.error(f"获取 CSRF token 失败: {resp_csrf.status_code}")
            return False        
        csrf_data = resp_csrf.json()
        csrf_token = csrf_data.get("csrf")
        logger.info(f"CSRF Token obtained: {csrf_token[:10]}...")

        # Step 2: Login
        logger.info("正在登录...")
        headers.update(
            {
                "X-CSRF-Token": csrf_token,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://linux.do",
            }
        )

        data = {
            "login": USERNAME,
            "password": PASSWORD,
            "second_factor_method": "1",
            "timezone": "Asia/Shanghai",
        }

        try:
            resp_login = self.session.post(
                SESSION_URL, data=data, impersonate="chrome136", headers=headers
            )

            if resp_login.status_code == 200:
                response_json = resp_login.json()
                if response_json.get("error"):
                    logger.error(f"登录失败: {response_json.get('error')}")
                    return False
                logger.info("登录成功!")
            else:
                logger.error(f"登录失败，状态码: {resp_login.status_code}")
                logger.error(resp_login.text)
                return False
        except Exception as e:
            logger.error(f"登录请求异常: {e}")
            return False

        # Step 3: Pass cookies to DrissionPage
        logger.info("同步 Cookie 到 DrissionPage...")

        cookies_dict = self.session.cookies.get_dict()

        dp_cookies = []
        for name, value in cookies_dict.items():
            dp_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".linux.do",
                    "path": "/",
                }
            )

        self.page.set.cookies(dp_cookies)

        logger.info("Cookie 设置完成，导航至 linux.do...")
        self.page.get(HOME_URL)

        time.sleep(5)
        try:
            user_ele = self.page.ele("@id=current-user")
        except Exception as e:
            logger.warning(f"登录验证失败: {str(e)}")
            return False
        if not user_ele:
            logger.error("登录验证失败 (未找到 current-user)")
            return False
        else:
            logger.info("登录验证成功")
            self.save_current_cookies()
            return True

    def click_topic(self):
        topic_list = self.page.ele("@id=list-area").eles(".:title")
        if not topic_list:
            logger.error("未找到主题帖")
            return False
        logger.info(f"发现 {len(topic_list)} 个主题帖，随机选择10个")
        for topic in random.sample(topic_list, 10):
            self.click_one_topic(topic.attr("href"))
        return True

    @retry_decorator()
    def click_one_topic(self, topic_url):
        new_page = self.browser.new_tab()
        try:
            new_page.get(topic_url)
            if random.random() < 0.3:  # 0.3 * 30 = 9
                self.click_like(new_page)
            self.browse_post(new_page)
        finally:
            try:
                new_page.close()
            except Exception:
                pass

    def browse_post(self, page):
        prev_url = None
        # 开始自动滚动，最多滚动10次
        for _ in range(10):
            # 随机滚动一段距离
            scroll_distance = random.randint(550, 650)  # 随机滚动 550-650 像素
            logger.info(f"向下滚动 {scroll_distance} 像素...")
            page.run_js(f"window.scrollBy(0, {scroll_distance})")
            logger.info(f"已加载页面: {page.url}")

            if random.random() < 0.03:  # 33 * 4 = 132
                logger.success("随机退出浏览")
                break

            # 检查是否到达页面底部
            at_bottom = page.run_js(
                "window.scrollY + window.innerHeight >= document.body.scrollHeight"
            )
            current_url = page.url
            if current_url != prev_url:
                prev_url = current_url
            elif at_bottom and prev_url == current_url:
                logger.success("已到达页面底部，退出浏览")
                break

            # 动态随机等待
            wait_time = random.uniform(2, 4)  # 随机等待 2-4 秒
            logger.info(f"等待 {wait_time:.2f} 秒...")
            time.sleep(wait_time)

    def run(self):
        try:
            cookie_str = COOKIES or read_saved_cookie()
            cookie_source = "环境变量" if COOKIES else "本地文件"

            # 优先使用 Cookie 登录，没有再使用账号密码
            if cookie_str:
                logger.info(f"尝试使用{cookie_source} Cookie 登录")
                login_res = self.login_with_cookies(cookie_str)
                if not login_res:
                    if USERNAME and PASSWORD:
                        logger.warning("Cookie 登录失败，尝试账号密码登录...")
                        login_res = self.login()
                    else:
                        logger.warning("Cookie 登录失败，且未配置账号密码")
            else:
                login_res = self.login()
            if not login_res:  # 登录
                logger.warning("登录验证失败，程序终止")
                self.send_notifications(False, [], login_success=False)
                return

            if BROWSE_ENABLED:
                click_topic_res = self.click_topic()  # 点击主题
                if not click_topic_res:
                    logger.error("点击主题失败，程序终止")
                    connect_info = self.fetch_connect_info()
                    self.send_notifications(True, connect_info, login_success=True, browse_success=False)
                    return
                logger.info("完成浏览任务")
            connect_info = self.fetch_connect_info()  # 获取连接信息
            self.send_notifications(BROWSE_ENABLED, connect_info, login_success=True)  # 发送通知
        finally:
            try:
                self.page.close()
            except Exception:
                pass
            try:
                self.browser.quit()
            except Exception:
                pass

    def click_like(self, page):
        try:
            # 专门查找未点赞的按钮
            like_button = page.ele(".discourse-reactions-reaction-button")
            if like_button:
                logger.info("找到未点赞的帖子，准备点赞")
                like_button.click()
                logger.info("点赞成功")
                time.sleep(random.uniform(1, 2))
            else:
                logger.info("帖子可能已经点过赞了")
        except Exception as e:
            logger.error(f"点赞失败: {str(e)}")

    @staticmethod
    def _split_value(text: str):
        text = re.sub(r"\s+", " ", unescape(text or "")).strip()
        if "/" not in text:
            return text, ""
        current, target = text.split("/", 1)
        return current.strip(), target.strip()

    @staticmethod
    def parse_connect_info(html: str) -> dict:
        result = {"items": [], "status": ""}

        def add_item(label, current, target=""):
            label = re.sub(r"\s+", " ", label or "").strip()
            current = re.sub(r"\s+", " ", current or "").strip()
            target = re.sub(r"\s+", " ", target or "").strip().lstrip("/").strip()
            if label and current:
                result["items"].append(
                    {"label": label, "current": current, "target": target}
                )

        if BeautifulSoup is not None:
            soup = BeautifulSoup(html or "", "html.parser")

            for row in soup.select("table tr"):
                cells = row.select("td")
                if len(cells) >= 3:
                    add_item(cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True), cells[2].get_text(" ", strip=True))

            for item in soup.select(".tl3-ring"):
                label = item.select_one(".tl3-ring-label")
                current = item.select_one(".tl3-ring-current")
                target = item.select_one(".tl3-ring-target")
                if label and current:
                    add_item(
                        label.get_text(" ", strip=True),
                        current.get_text(" ", strip=True),
                        target.get_text(" ", strip=True) if target else "",
                    )

            for item in soup.select(".tl3-bar-item"):
                label = item.select_one(".tl3-bar-label")
                nums = item.select_one(".tl3-bar-nums")
                if label and nums:
                    current, target = LinuxDoBrowser._split_value(nums.get_text(" ", strip=True))
                    add_item(label.get_text(" ", strip=True), current, target)

            for item in soup.select(".tl3-quota-card"):
                label = item.select_one(".tl3-quota-label")
                nums = item.select_one(".tl3-quota-nums")
                if label and nums:
                    current, target = LinuxDoBrowser._split_value(nums.get_text(" ", strip=True))
                    add_item(label.get_text(" ", strip=True), current, target)

            for item in soup.select(".tl3-veto-face.tl3-veto-front"):
                label = item.select_one(".tl3-veto-label")
                value = item.select_one(".tl3-veto-value")
                if label and value:
                    add_item(label.get_text(" ", strip=True), value.get_text(" ", strip=True))

            status = soup.select_one(".status-met, .status-warning, .status-danger")
            if status:
                result["status"] = status.get_text(" ", strip=True)

            if result["items"] or result["status"]:
                return result

        # 无 bs4 或页面结构轻微变化时的兜底解析。
        for block in re.findall(r'<div class="tl3-ring">(.*?)</div>\s*</div>', html or "", re.S):
            label = re.search(r'tl3-ring-label">([^<]+)', block)
            current = re.search(r'tl3-ring-current">([^<]+)', block)
            target = re.search(r'tl3-ring-target">([^<]+)', block)
            if label and current:
                add_item(label.group(1), current.group(1), target.group(1) if target else "")

        for block in re.findall(r'<div class="tl3-bar-item">(.*?)</div>\s*</div>\s*</div>', html or "", re.S):
            label = re.search(r'tl3-bar-label">([^<]+)', block)
            nums = re.search(r'tl3-bar-nums[^>]*>([^<]+)', block)
            if label and nums:
                current, target = LinuxDoBrowser._split_value(nums.group(1))
                add_item(label.group(1), current, target)

        for block in re.findall(r'<div class="tl3-quota-card[^>]*>(.*?)</div>\s*</div>', html or "", re.S):
            label = re.search(r'tl3-quota-label">([^<]+)', block)
            nums = re.search(r'tl3-quota-nums">([^<]+)', block)
            if label and nums:
                current, target = LinuxDoBrowser._split_value(nums.group(1))
                add_item(label.group(1), current, target)

        status = re.search(r'<p class="status-[^"]+[^"]*">(.*?)</p>', html or "", re.S)
        if status:
            result["status"] = re.sub(r"\s+", " ", re.sub(r"<.*?>", "", status.group(1))).strip()

        return result

    def fetch_connect_info(self):
        logger.info("获取 Connect 活跃信息")
        try:
            # Connect 依赖浏览器中的登录、跨子域 Cookie 及可能的 SSO 状态，
            # 必须复用已登录页面，避免独立请求会话因状态不完整返回 403。
            self.page.get(CONNECT_URL)
            time.sleep(5)
            final_url = self.page.url or ""
            title = getattr(self.page, "title", "") or ""
            html = self.page.html or ""
            logger.info(f"Connect 最终页面: {final_url} ({title})")
        except Exception as e:
            logger.warning(f"浏览器获取 Connect 信息失败: {e}")
            return {"items": [], "status": ""}

        info = self.parse_connect_info(html)
        if not info["items"] and not info["status"]:
            if "/login" in final_url:
                logger.warning("Connect 跳转到登录页面，当前 Cookie 登录态无效或未完成授权")
            elif "403" in title or "forbidden" in html.lower():
                logger.warning("Connect 页面返回 403，浏览器会话可能被安全策略拦截")
            else:
                logger.warning("Connect 页面已打开，但未解析到活跃数据，页面结构可能已变化")

        self.log_connect_info(info)
        return info

    def log_connect_info(self, info: dict):
        items = info.get("items") or []
        if not items:
            logger.warning("未获取到 Connect 活跃数据")
            if info.get("status"):
                logger.info(f"Connect 状态: {info['status']}")
            return

        rows = [
            [item["label"], item["current"], item.get("target") or "-"]
            for item in items
        ]
        logger.info("--------------Connect Info-----------------")
        if tabulate:
            logger.info("\n" + tabulate(rows, headers=["项目", "当前", "要求"], tablefmt="pretty"))
        else:
            logger.info("\n" + "\n".join(f"{r[0]}: {r[1]} / {r[2]}" for r in rows))
        if info.get("status"):
            logger.info(f"Connect 状态: {info['status']}")

    @staticmethod
    def format_connect_info(info: dict) -> str:
        lines = []
        for item in (info or {}).get("items", []):
            if item.get("target"):
                lines.append(f"{item['label']}：{item['current']} / {item['target']}")
            else:
                lines.append(f"{item['label']}：{item['current']}")
        if (info or {}).get("status"):
            lines.append(f"状态：{info['status']}")
        return "\n".join(lines)

    def send_notifications(self, browse_enabled, connect_info=None, login_success=True, browse_success=True):
        """发送签到通知"""
        if login_success:
            lines = ["✅ 每日登录成功"]
        else:
            lines = ["❌ 登录失败"]

        if USERNAME:
            lines.append(f"账号：{USERNAME}")
        if browse_enabled:
            lines.append(f"浏览：{'已完成' if browse_success else '失败'}")
        else:
            lines.append("浏览：已跳过")

        connect_text = self.format_connect_info(connect_info or {})
        if connect_text:
            lines.extend(["", "Connect：", connect_text])

        ql_notify("Linux.Do 签到", "\n".join(lines))


if __name__ == "__main__":
    if not COOKIES and not read_saved_cookie() and (not USERNAME or not PASSWORD):
        print("请设置 LINUXDO_COOKIES（Cookie 登录）、本地 Cookie 文件，或同时设置 USERNAME 和 PASSWORD（账号密码登录）")
        exit(1)
    browser = LinuxDoBrowser()
    browser.run()
