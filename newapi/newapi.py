#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NewAPI 多站点自动签到 —— 青龙面板版

cron: 30 9 * * *
new Env('NewAPI 自动签到');

----------------------------------------------------------------------------
实现：多站点 + 多账号 + 签到 + 余额检查 + 余额变化检测 + 青龙通知
依赖：curl_cffi；若站点启用 WAF，还需要 DrissionPage + chromium 浏览器
通知：直接复用青龙自带的 notify.py 的 send(title, content)

环境变量（在 青龙 > 环境变量 中配置）：

  NEWAPI_SITES          多站点配置，JSON 数组，例如：
      [
        {
          "name": "AnyRouter",
          "base_url": "https://anyrouter.top",
          "checkin_endpoint": "/api/user/sign_in",
          "user_info_endpoint": "/api/user/self",
          "waf": true,
          "accounts": [
            {"name": "主号", "cookie": "session=xxx", "api_user": "12345"}
          ]
        },
        {
          "name": "新站点",
          "base_url": "https://example.com",
          "checkin_endpoint": "/api/user/checkin",
          "waf": "auto",
          "accounts": [
            {"name": "账号A", "cookie": "session=yyy"}
          ]
        }
      ]

      · base_url 必填，脚本会自动去掉末尾 /
      · checkin_endpoint 默认 /api/user/checkin
      · user_info_endpoint 默认 /api/user/self
      · login_path 默认 /login，仅 waf 为 true/auto 时使用
      · console_path 默认 /console，用于 Referer
      · waf 可选 true / false / "auto"，默认 "auto"
      · account.cookie 支持 "k=v; k2=v2" 字符串，也支持 {"session":"xxx"} 对象
      · account.api_user 可选；存在时会写入请求头 new-api-user

  NOTIFY_TRIGGERS       通知触发条件，逗号分隔，默认 always
      可选值：always / never / success / failed / balance_changed

  NEWAPI_BROWSER_PATH   指定 chromium 可执行文件路径（自动探测失败时用）
  CHROME_PATH           NEWAPI_BROWSER_PATH 未设置时使用
  NEWAPI_DATA_DIR       余额记录文件存放目录，默认脚本同级目录
  TZ                    时区，默认 Asia/Shanghai
----------------------------------------------------------------------------
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


# --------------------------------------------------------------------------- #
# 日志（loguru）
# --------------------------------------------------------------------------- #
try:
	from loguru import logger

	logger.remove()
	logger.add(
		sys.stdout,
		colorize=False,
		level='INFO',
		format='{time:HH:mm:ss} | {level: <7} | {message}',
	)
except ImportError:
	class _Fallback:
		def _log(self, level, msg):
			print(f'{datetime.now():%H:%M:%S} | {level:<7} | {msg}')

		info = lambda self, m: self._log('INFO', m)
		success = lambda self, m: self._log('SUCCESS', m)
		warning = lambda self, m: self._log('WARNING', m)
		error = lambda self, m: self._log('ERROR', m)

	logger = _Fallback()


# --------------------------------------------------------------------------- #
# 青龙通知：复用面板自带的 notify.py
# --------------------------------------------------------------------------- #
def _load_ql_send():
	"""加载青龙的 notify.send，找不到则返回 None。"""
	try:
		from notify import send  # type: ignore
		return send
	except Exception:
		pass

	candidates = [
		os.environ.get('QL_DIR', '') and os.path.join(os.environ['QL_DIR'], 'scripts'),
		'/ql/data/scripts',
		'/ql/scripts',
		os.path.dirname(os.path.abspath(__file__)),
	]
	for path in candidates:
		if path and os.path.isfile(os.path.join(path, 'notify.py')) and path not in sys.path:
			sys.path.append(path)
			try:
				from notify import send  # type: ignore
				return send
			except Exception:
				continue

	return None


QL_SEND = _load_ql_send()


def notify(title: str, content: str):
	"""发送通知：优先走青龙 notify.send，否则仅打印到日志。"""
	if QL_SEND is None:
		logger.warning('未找到青龙通知模块 notify.py，以下为通知内容（仅打印）：')
		logger.info(f'\n【{title}】\n{content}')
		return

	try:
		QL_SEND(title, content)
		logger.success('通知已发送')
	except Exception as e:
		logger.error(f'通知发送失败：{e}')
		logger.info(f'\n【{title}】\n{content}')


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
QUOTA_PER_DOLLAR = 500000
DEFAULT_CHECKIN_ENDPOINT = '/api/user/checkin'
DEFAULT_USER_INFO_ENDPOINT = '/api/user/self'
DEFAULT_LOGIN_PATH = '/login'
DEFAULT_CONSOLE_PATH = '/console'

DATA_DIR = os.getenv('NEWAPI_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
BALANCE_FILE = os.path.join(DATA_DIR, 'newapi_balance.json')

WAF_COOKIE_NAMES = ('acw_tc', 'cdn_sec_tc', 'acw_sc__v2')
WAF_MODES = {'true', 'false', 'auto'}


# --------------------------------------------------------------------------- #
# 配置读取与标准化
# --------------------------------------------------------------------------- #
def _strip(value) -> str:
	return str(value or '').strip()


def normalize_base_url(value: str) -> str:
	base_url = _strip(value).rstrip('/')
	if not base_url:
		return ''

	parsed = urlparse(base_url)
	if not parsed.scheme or not parsed.netloc:
		logger.warning(f'站点地址无效：{base_url}')
		return ''

	return base_url


def normalize_path(value, default: str) -> str:
	path = _strip(value) or default
	if path.startswith('http://') or path.startswith('https://'):
		return path
	return '/' + path.lstrip('/')


def build_url(base_url: str, path: str) -> str:
	if path.startswith('http://') or path.startswith('https://'):
		return path
	return f'{base_url}{normalize_path(path, "/")}'


def normalize_waf_mode(value) -> str:
	if isinstance(value, bool):
		return 'true' if value else 'false'

	mode = _strip(value).lower() or 'auto'
	if mode not in WAF_MODES:
		logger.warning(f'waf 配置值无效：{value}，已按 auto 处理')
		return 'auto'
	return mode


def site_host(base_url: str) -> str:
	return urlparse(base_url).netloc or base_url


def parse_cookies(cookies_data) -> dict:
	"""把 cookie 配置解析成 dict，支持对象与 'k=v; k2=v2' 字符串两种格式。"""
	if isinstance(cookies_data, dict):
		return {str(k).strip(): str(v).strip() for k, v in cookies_data.items() if str(k).strip()}

	if isinstance(cookies_data, str):
		result = {}
		for item in cookies_data.split(';'):
			item = item.strip()
			if not item or '=' not in item:
				continue
			k, v = item.split('=', 1)
			k = k.strip()
			if k:
				result[k] = v.strip()
		return result

	return {}


def _normalize_account(raw: dict, site_name: str, site_index: int, account_index: int):
	if not isinstance(raw, dict):
		logger.warning(f'[{site_name}] 跳过无效账号配置：账号必须是对象')
		return None

	name = _strip(raw.get('name')) or f'账号{account_index + 1}'
	cookie = raw.get('cookie')
	if cookie is None:
		logger.warning(f'[{site_name}/{name}] 跳过账号：缺少 cookie')
		return None

	cookies = parse_cookies(cookie)
	if not cookies:
		logger.warning(f'[{site_name}/{name}] 跳过账号：cookie 配置无效')
		return None

	return {
		'name': name,
		'api_user': _strip(raw.get('api_user')),
		'cookies': cookies,
		'cookie_hash': hashlib.sha256(json.dumps(cookies, sort_keys=True).encode('utf-8')).hexdigest(),
		'index': account_index,
		'site_index': site_index,
	}


def _normalize_site(raw: dict, index: int):
	if not isinstance(raw, dict):
		logger.warning(f'跳过无效站点配置：第 {index + 1} 项必须是对象')
		return None

	base_url = normalize_base_url(raw.get('base_url'))
	if not base_url:
		logger.warning(f'跳过无效站点配置：第 {index + 1} 项缺少有效 base_url')
		return None

	name = _strip(raw.get('name')) or site_host(base_url)
	checkin_endpoint = normalize_path(raw.get('checkin_endpoint'), DEFAULT_CHECKIN_ENDPOINT)
	user_info_endpoint = normalize_path(raw.get('user_info_endpoint'), DEFAULT_USER_INFO_ENDPOINT)
	login_path = normalize_path(raw.get('login_path'), DEFAULT_LOGIN_PATH)
	console_path = normalize_path(raw.get('console_path'), DEFAULT_CONSOLE_PATH)
	waf = normalize_waf_mode(raw.get('waf', 'auto'))

	raw_accounts = raw.get('accounts')
	if not isinstance(raw_accounts, list) or not raw_accounts:
		logger.warning(f'[{name}] 跳过站点：accounts 必须是非空数组')
		return None

	accounts = []
	seen = set()
	for account_index, account in enumerate(raw_accounts):
		normalized = _normalize_account(account, name, index, account_index)
		if not normalized:
			continue

		dedup_key = (
			normalized['name'],
			normalized['api_user'],
			normalized['cookie_hash'],
		)
		if dedup_key in seen:
			continue
		seen.add(dedup_key)
		accounts.append(normalized)

	if not accounts:
		logger.warning(f'[{name}] 跳过站点：没有有效账号')
		return None

	return {
		'name': name,
		'base_url': base_url,
		'host': site_host(base_url),
		'checkin_url': build_url(base_url, checkin_endpoint),
		'user_info_url': build_url(base_url, user_info_endpoint),
		'login_url': build_url(base_url, login_path),
		'console_url': build_url(base_url, console_path),
		'waf': waf,
		'accounts': accounts,
		'index': index,
	}


def load_sites() -> list:
	raw = os.getenv('NEWAPI_SITES', '').strip()
	if not raw:
		logger.error('未找到 NEWAPI_SITES 环境变量')
		logger.info(
			'示例：NEWAPI_SITES = '
			'[{"name":"新站点","base_url":"https://example.com","accounts":[{"name":"主号","cookie":"session=xxx"}]}]'
		)
		return []

	try:
		data = json.loads(raw)
	except Exception as e:
		logger.error(f'NEWAPI_SITES 解析失败：{e}')
		return []

	if isinstance(data, dict) and isinstance(data.get('sites'), list):
		data = data['sites']
	if not isinstance(data, list):
		logger.error('NEWAPI_SITES 必须是 JSON 数组')
		return []

	sites = []
	seen = set()
	for index, item in enumerate(data):
		site = _normalize_site(item, index)
		if not site:
			continue

		dedup_key = (site['base_url'], site['checkin_url'], site['user_info_url'])
		if dedup_key in seen:
			logger.warning(f'[{site["name"]}] 跳过重复站点配置')
			continue
		seen.add(dedup_key)
		sites.append(site)

	return sites


# --------------------------------------------------------------------------- #
# 余额记录（检测余额是否变化）
# --------------------------------------------------------------------------- #
def account_key(site: dict, account: dict) -> str:
	identity = account.get('api_user') or f'{account.get("name", "")}#{account.get("index", 0)}'
	raw = f'{site["base_url"]}|{identity}'
	return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def balance_hash(quota, used) -> str:
	return hashlib.sha256(f'{quota}_{used}'.encode('utf-8')).hexdigest()


def load_balance_record():
	"""读取上次的余额哈希记录，首次运行返回 None。"""
	try:
		if os.path.isfile(BALANCE_FILE):
			with open(BALANCE_FILE, 'r', encoding='utf-8') as f:
				content = f.read().strip()
				return json.loads(content) if content else None
	except Exception as e:
		logger.warning(f'读取余额记录失败：{e}')
	return None


def save_balance_record(record: dict):
	try:
		os.makedirs(os.path.dirname(BALANCE_FILE) or '.', exist_ok=True)
		with open(BALANCE_FILE, 'w', encoding='utf-8') as f:
			json.dump(record, f, ensure_ascii=False, indent=2)
	except Exception as e:
		logger.warning(f'保存余额记录失败：{e}')


# --------------------------------------------------------------------------- #
# WAF cookies
# --------------------------------------------------------------------------- #
def _cookies_to_dict(page) -> dict:
	"""兼容不同 DrissionPage 版本，取出 {name: value} 形式的 cookies。"""
	try:
		ck = page.cookies(as_dict=True)
		if isinstance(ck, dict):
			return dict(ck)
	except TypeError:
		pass

	out = {}
	try:
		for c in page.cookies():
			name = c['name'] if isinstance(c, dict) else getattr(c, 'name', None)
			if name:
				out[name] = c['value'] if isinstance(c, dict) else getattr(c, 'value', '')
	except Exception:
		pass
	return out


def fetch_waf_cookies(site: dict):
	"""
	启动 chromium 访问站点登录页，等待 WAF 的 JS 挑战完成后取出 cookies。

	Returns:
		(cookies: dict, user_agent: str, ok: bool)
	"""
	try:
		from DrissionPage import ChromiumPage, ChromiumOptions
	except ImportError:
		logger.error('未安装 DrissionPage，请先安装：pip install DrissionPage')
		return {}, default_user_agent(), False

	logger.info(f'[{site["name"]}] 启动浏览器获取 WAF cookies ...')

	co = ChromiumOptions()
	browser_path = os.getenv('NEWAPI_BROWSER_PATH') or os.getenv('CHROME_PATH')
	if browser_path:
		co.set_browser_path(browser_path)
	co.headless()
	co.auto_port()
	co.set_argument('--no-sandbox')
	co.set_argument('--disable-dev-shm-usage')
	co.set_argument('--disable-gpu')
	co.set_argument('--disable-blink-features=AutomationControlled')
	co.set_argument('--window-size', '1920,1080')

	page = None
	try:
		page = ChromiumPage(addr_or_opts=co)
		page.get(site['login_url'], retry=2, timeout=30)

		deadline = time.time() + 18
		while time.time() < deadline:
			if 'acw_sc__v2' in _cookies_to_dict(page):
				break
			page.wait(0.6)

		try:
			user_agent = page.run_js('return navigator.userAgent;')
		except Exception:
			user_agent = default_user_agent()

		cookies = _cookies_to_dict(page)
		got = [n for n in WAF_COOKIE_NAMES if n in cookies]
		logger.info(f'[{site["name"]}] 已获取 {len(cookies)} 个 cookies，其中 WAF cookies：{got or "无"}')

		if not got:
			logger.warning(f'[{site["name"]}] 未获取到常见 WAF cookies，后续请求会继续尝试')
		return cookies, user_agent, True

	except Exception as e:
		logger.error(f'[{site["name"]}] 获取 WAF cookies 失败：{e}')
		return {}, default_user_agent(), False

	finally:
		if page is not None:
			try:
				page.quit()
			except Exception:
				pass


def default_user_agent() -> str:
	return (
		'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
		'(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
	)


def prepare_site_runtime(site: dict):
	mode = site['waf']
	if mode == 'false':
		logger.info(f'[{site["name"]}] 已关闭 WAF cookie 获取')
		return {'cookies': {}, 'user_agent': default_user_agent(), 'ready': True}

	cookies, user_agent, ok = fetch_waf_cookies(site)
	if ok or mode == 'auto':
		return {'cookies': cookies, 'user_agent': user_agent, 'ready': True}

	return {'cookies': {}, 'user_agent': user_agent, 'ready': False}


# --------------------------------------------------------------------------- #
# 接口请求
# --------------------------------------------------------------------------- #
def _build_headers(site: dict, user_agent: str, api_user: str, is_post: bool) -> dict:
	headers = {
		'User-Agent': user_agent,
		'Referer': site['console_url'],
		'Origin': site['base_url'],
		'Accept': 'application/json, text/plain, */*',
		'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
		'Connection': 'keep-alive',
		'Sec-Fetch-Dest': 'empty',
		'Sec-Fetch-Mode': 'cors',
		'Sec-Fetch-Site': 'same-origin',
	}
	if api_user:
		headers['new-api-user'] = str(api_user)
	if is_post:
		headers['Content-Type'] = 'application/json'
		headers['X-Requested-With'] = 'XMLHttpRequest'
	return headers


def get_user_info(session, site: dict, cookies: dict, user_agent: str, api_user: str):
	"""查询账号余额，返回 {'quota','used'} 或 None。"""
	try:
		resp = session.get(
			site['user_info_url'],
			headers=_build_headers(site, user_agent, api_user, is_post=False),
			cookies=cookies,
			timeout=30,
		)
		if resp.status_code != 200:
			logger.warning(f'[{site["name"]}] 获取用户信息失败：HTTP {resp.status_code}')
			return None

		data = resp.json()
		if not data.get('success'):
			logger.warning(f'[{site["name"]}] 获取用户信息失败：{data.get("message") or data.get("msg") or "接口返回失败"}')
			return None

		user = data.get('data') or {}
		return {
			'quota': round(float(user.get('quota', 0) or 0) / QUOTA_PER_DOLLAR, 2),
			'used': round(float(user.get('used_quota', 0) or 0) / QUOTA_PER_DOLLAR, 2),
		}
	except Exception as e:
		logger.warning(f'[{site["name"]}] 获取用户信息异常：{str(e)[:80]}')
		return None


_ALREADY_KEYWORDS = ('已签到', '已经签', '重复', 'already', 'signed', '今日已')


def do_checkin(session, site: dict, cookies: dict, user_agent: str, api_user: str):
	"""执行签到，返回 (status, message)；status ∈ {'success','already','failed'}。"""
	try:
		resp = session.post(
			site['checkin_url'],
			headers=_build_headers(site, user_agent, api_user, is_post=True),
			cookies=cookies,
			timeout=30,
		)
		if resp.status_code != 200:
			return 'failed', f'HTTP {resp.status_code}'

		try:
			data = resp.json()
		except Exception:
			text = (resp.text or '').lower()
			if 'success' in text:
				return 'success', '签到成功'
			if any(kw in text for kw in (k.lower() for k in _ALREADY_KEYWORDS)):
				return 'already', resp.text[:80] or '今日已签到'
			return 'failed', '无效响应格式'

		if data.get('ret') == 1 or data.get('code') == 0 or data.get('success'):
			return 'success', str(data.get('msg') or data.get('message') or '签到成功')

		msg = str(data.get('msg') or data.get('message') or data.get('error') or '未知错误')
		if any(kw in msg.lower() for kw in (k.lower() for k in _ALREADY_KEYWORDS)):
			return 'already', msg
		return 'failed', msg

	except Exception as e:
		return 'failed', str(e)[:80]


def empty_result(site: dict, account: dict, status='failed', error=None) -> dict:
	return {
		'site_name': site['name'],
		'base_url': site['base_url'],
		'host': site['host'],
		'name': account['name'],
		'api_user': account.get('api_user', ''),
		'status': status,
		'quota': None,
		'used': None,
		'balance_changed': None,
		'message': '',
		'error': error,
	}


def check_in_account(site: dict, account: dict, runtime: dict) -> dict:
	"""处理单个账号：查余额 + 签到，返回结构化结果。"""
	from curl_cffi import requests as cffi_requests

	result = empty_result(site, account)
	name = account['name']
	api_user = account.get('api_user', '')

	logger.info(f'==== 开始处理 [{site["name"]}/{name}] ====')

	cookies = {**(runtime.get('cookies') or {}), **account['cookies']}
	user_agent = runtime.get('user_agent') or default_user_agent()

	session = cffi_requests.Session(impersonate='chrome')
	try:
		info = get_user_info(session, site, cookies, user_agent, api_user)
		if info:
			result['quota'] = info['quota']
			result['used'] = info['used']
			logger.info(f'[{site["name"]}/{name}] 当前余额：${info["quota"]}（已用 ${info["used"]}）')

		status, message = do_checkin(session, site, cookies, user_agent, api_user)
		result['status'] = status
		result['message'] = message
		if status == 'success':
			logger.success(f'[{site["name"]}/{name}] 签到成功：{message}')
		elif status == 'already':
			logger.info(f'[{site["name"]}/{name}] 今日已签到：{message}')
		else:
			result['error'] = message
			logger.error(f'[{site["name"]}/{name}] 签到失败：{message}')

		return result
	finally:
		try:
			session.close()
		except Exception:
			pass


# --------------------------------------------------------------------------- #
# 通知触发判断
# --------------------------------------------------------------------------- #
def parse_triggers() -> set:
	raw = os.getenv('NOTIFY_TRIGGERS', '').strip().lower()
	if not raw:
		return {'always'}
	valid = {'always', 'never', 'success', 'failed', 'balance_changed'}
	triggers = {t.strip() for t in raw.split(',') if t.strip() in valid}
	return triggers or {'always'}


def should_notify(triggers, has_success, has_failed, has_balance_changed, is_first_run) -> bool:
	if 'never' in triggers:
		return False
	if 'always' in triggers:
		return True
	if 'balance_changed' in triggers and (is_first_run or has_balance_changed):
		return True
	if 'failed' in triggers and has_failed:
		return True
	if 'success' in triggers and has_success:
		return True
	return False


# --------------------------------------------------------------------------- #
# 通知内容拼装
# --------------------------------------------------------------------------- #
def build_notification(results: list, timestamp: str):
	total = len(results)
	ok = [r for r in results if r['status'] in ('success', 'already')]
	failed = [r for r in results if r['status'] == 'failed']
	success_count = len(ok)

	if not failed:
		head = f'✅ 全部签到成功 ({success_count}/{total})'
	elif ok:
		head = f'⚠️ 部分签到成功 ({success_count}/{total})'
	else:
		head = f'❌ 全部签到失败 (0/{total})'

	title = f'NewAPI 签到 {head}'
	lines = [head, f'🕐 {timestamp}', '']

	current_site = None
	for r in results:
		site_label = f'{r["site_name"]} ({r["host"]})'
		if site_label != current_site:
			if current_site is not None:
				lines.append('')
			lines.append(f'【{site_label}】')
			current_site = site_label

		if r['status'] == 'success':
			icon = '✅'
		elif r['status'] == 'already':
			icon = '☑️'
		else:
			icon = '❌'

		lines.append(f'{icon} {r["name"]}')

		if r['status'] == 'failed':
			lines.append(f'    错误：{r["error"]}')
		else:
			if r['quota'] is not None:
				mark = '  🔔余额有变化' if r['balance_changed'] is True else ''
				lines.append(f'    余额：${r["quota"]}（已用 ${r["used"]}）{mark}')
			if r['status'] == 'already':
				lines.append(f'    {r["message"]}')

	return title, '\n'.join(lines).strip()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
	logger.info('NewAPI 自动签到脚本启动（青龙版）')

	sites = load_sites()
	if not sites:
		return

	total_accounts = sum(len(site['accounts']) for site in sites)
	logger.info(f'共找到 {len(sites)} 个站点，{total_accounts} 个账号')

	last_record = load_balance_record()
	is_first_run = last_record is None
	current_record = {}
	results = []
	has_balance_changed = False

	for site in sites:
		logger.info(f'---- 开始处理站点 [{site["name"]}] {site["base_url"]} ----')
		runtime = prepare_site_runtime(site)

		if not runtime.get('ready'):
			error = '无法获取 WAF cookies，且该站点 waf=true'
			logger.error(f'[{site["name"]}] {error}')
			for account in site['accounts']:
				results.append(empty_result(site, account, error=error))
			continue

		for account in site['accounts']:
			try:
				result = check_in_account(site, account, runtime)
			except Exception as e:
				result = empty_result(site, account, error=f'处理异常：{str(e)[:80]}')
				logger.error(f'[{site["name"]}/{account["name"]}] 处理异常：{e}')

			if result['quota'] is not None:
				key = account_key(site, account)
				cur_hash = balance_hash(result['quota'], result['used'])
				current_record[key] = cur_hash
				if last_record and key in last_record:
					if last_record[key] != cur_hash:
						result['balance_changed'] = True
						has_balance_changed = True
					else:
						result['balance_changed'] = False
				else:
					result['balance_changed'] = False

			results.append(result)

	if current_record:
		save_balance_record(current_record)

	success_count = sum(1 for r in results if r['status'] in ('success', 'already'))
	failed_count = len(results) - success_count
	has_success = success_count > 0
	has_failed = failed_count > 0
	logger.info(f'签到结果：成功 {success_count}/{len(results)}，失败 {failed_count}/{len(results)}')

	tz_name = os.getenv('TZ') or 'Asia/Shanghai'
	try:
		tz = ZoneInfo(tz_name)
	except Exception:
		tz = ZoneInfo('Asia/Shanghai')
	timestamp = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')

	triggers = parse_triggers()
	if should_notify(triggers, has_success, has_failed, has_balance_changed, is_first_run):
		title, content = build_notification(results, timestamp)
		notify(title, content)
	else:
		logger.info(f'当前触发条件 {triggers} 未满足，跳过通知')


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		logger.warning('已手动中断')
	except Exception as e:
		logger.error(f'脚本运行异常：{e}')
		try:
			notify('NewAPI 签到 ❌ 运行异常', f'脚本运行异常：{e}')
		except Exception:
			pass
