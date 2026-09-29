#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NewAPI 多站点自动签到 —— 青龙面板版

cron: 30 9 * * *
new Env('NewAPI 自动签到');

----------------------------------------------------------------------------
实现：多站点 + 多账号 + 签到 + 余额检查 + 余额变化检测 + 青龙通知
依赖：curl_cffi loguru（纯 Python 实现，零浏览器依赖，内置阿里 WAF 算解）
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

  NEWAPI_DATA_DIR       余额记录文件存放目录，默认脚本同级目录
  TZ                    时区，默认 Asia/Shanghai
----------------------------------------------------------------------------
"""

import hashlib
import json
import os
import re
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
RESPONSE_LOG_LIMIT = 180


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
WAF_KEY_DEFAULT = '3000176000856006061501533003690027800375'
WAF_MASK = (
	15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25, 13,
	6, 11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3, 28, 34,
	37, 12, 36,
)


def calc_acw_sc_v2(arg1: str, key: str = WAF_KEY_DEFAULT) -> str:
	"""纯 Python 算解阿里云盾 acw_sc__v2 cookie。"""
	if not arg1 or len(arg1) < len(WAF_MASK):
		return ''
	unsboxed = ''.join(arg1[pos - 1] for pos in WAF_MASK)
	res = []
	for i in range(0, min(len(unsboxed), len(key)), 2):
		v1 = int(unsboxed[i:i + 2], 16)
		v2 = int(key[i:i + 2], 16)
		res.append(f'{v1 ^ v2:02x}')
	return ''.join(res)


def extract_waf_challenge(html: str):
	"""从响应 HTML 中提取阿里 WAF 的 arg1 与 key。"""
	if not html or ('acw_sc__v2' not in html and 'arg1' not in html):
		return None, None
	match = re.search(r"(?:var\s+)?arg1\s*=\s*['\"]([0-9a-fA-F]{40})['\"]", html)
	if not match:
		match = re.search(r"arg1\s*=\s*['\"]([0-9a-fA-F]+)['\"]", html)
	if not match:
		return None, None
	arg1 = match.group(1)

	key_match = re.search(r"['\"]([0-9]{40})['\"]", html)
	key = key_match.group(1) if key_match else WAF_KEY_DEFAULT
	return arg1, key


def default_user_agent() -> str:
	return (
		'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
		'(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
	)


def fetch_waf_cookies(site: dict):
	"""
	纯 HTTP 访问站点登录页，若检测到阿里盾 WAF 挑战，纯 Python 秒解 acw_sc__v2。

	Returns:
		(cookies: dict, user_agent: str, ok: bool)
	"""
	try:
		from curl_cffi import requests as cffi_requests
	except ImportError:
		logger.error('缺少依赖 curl_cffi，请在青龙「依赖管理 → Python」中安装 curl_cffi')
		return {}, default_user_agent(), False

	user_agent = default_user_agent()
	logger.info(f'[{site["name"]}] 发起 HTTP 探测 WAF 状态 ...')

	session = cffi_requests.Session(impersonate='chrome')
	try:
		resp = session.get(
			site['login_url'],
			headers={
				'User-Agent': user_agent,
				'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
			},
			timeout=20,
		)
		cookies = dict(session.cookies.get_dict())
		html = getattr(resp, 'text', '') or ''
		arg1, key = extract_waf_challenge(html)

		if arg1:
			acw_sc_v2 = calc_acw_sc_v2(arg1, key)
			if acw_sc_v2:
				cookies['acw_sc__v2'] = acw_sc_v2
				logger.info(f'[{site["name"]}] 探测到阿里云 WAF 挑战，纯 Python 解算 acw_sc__v2 成功')
			else:
				logger.warning(f'[{site["name"]}] 阿里 WAF 挑战解算失败（arg1 异常）')
		else:
			logger.info(f'[{site["name"]}] 页面未触发 WAF 挑战，直接进入后续流程')

		got = [n for n in WAF_COOKIE_NAMES if n in cookies]
		if got:
			logger.info(f'[{site["name"]}] 已获取 WAF cookies：{got}')
		return cookies, user_agent, True

	except Exception as e:
		logger.warning(f'[{site["name"]}] HTTP 探测 WAF 出现异常：{e}')
		return {}, user_agent, False


def prepare_site_runtime(site: dict):
	mode = site['waf']
	if mode == 'false':
		logger.info(f'[{site["name"]}] 已关闭 WAF cookie 获取')
		return {'cookies': {}, 'user_agent': default_user_agent(), 'ready': True}

	cookies, user_agent, ok = fetch_waf_cookies(site)
	if ok or mode == 'auto':
		return {'cookies': cookies, 'user_agent': user_agent, 'ready': True}

	return {'cookies': {}, 'user_agent': user_agent, 'ready': False}


def merge_account_cookies(account_cookies: dict, runtime_cookies: dict) -> dict:
	"""
	账号 cookie 保留登录态；本次获取的 WAF cookie 优先级更高。
	复制账号 cookie 时经常会带上旧 WAF 值，不能让它覆盖当前运行环境的新值。
	"""
	cookies = dict(account_cookies or {})
	runtime_cookies = runtime_cookies or {}
	replaced = []

	for name in WAF_COOKIE_NAMES:
		if name in runtime_cookies:
			if cookies.get(name) and cookies.get(name) != runtime_cookies[name]:
				replaced.append(name)
			cookies[name] = runtime_cookies[name]

	if replaced:
		logger.info(f'已用本次获取的 WAF cookies 覆盖账号 cookie 中的旧值：{replaced}')
	return cookies

# --------------------------------------------------------------------------- #
# 接口请求
# --------------------------------------------------------------------------- #
def response_summary(resp) -> str:
	content_type = ''
	try:
		content_type = resp.headers.get('content-type', '')
	except Exception:
		pass

	text = (getattr(resp, 'text', '') or '').replace('\n', ' ').replace('\r', ' ').strip()
	if len(text) > RESPONSE_LOG_LIMIT:
		text = text[:RESPONSE_LOG_LIMIT] + '...'
	return f'HTTP {getattr(resp, "status_code", "未知")}，content-type={content_type or "未知"}，body={text or "空"}'


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

		try:
			data = resp.json()
		except Exception:
			html = getattr(resp, 'text', '') or ''
			arg1, key = extract_waf_challenge(html)
			if arg1:
				logger.info(f'[{site["name"]}] 接口触发阿里 WAF 挑战，纯 Python 解算 acw_sc__v2 并重试 ...')
				acw_sc_v2 = calc_acw_sc_v2(arg1, key)
				if acw_sc_v2:
					cookies['acw_sc__v2'] = acw_sc_v2
					resp = session.get(
						site['user_info_url'],
						headers=_build_headers(site, user_agent, api_user, is_post=False),
						cookies=cookies,
						timeout=30,
					)
					try:
						data = resp.json()
					except Exception:
						logger.warning(f'[{site["name"]}] 获取用户信息重试后仍非 JSON：{response_summary(resp)}')
						return None
				else:
					return None
			else:
				logger.warning(f'[{site["name"]}] 获取用户信息响应不是 JSON：{response_summary(resp)}')
				return None

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
			html = getattr(resp, 'text', '') or ''
			arg1, key = extract_waf_challenge(html)
			if arg1:
				logger.info(f'[{site["name"]}] 签到接口触发阿里 WAF 挑战，纯 Python 解算 acw_sc__v2 并重试 ...')
				acw_sc_v2 = calc_acw_sc_v2(arg1, key)
				if acw_sc_v2:
					cookies['acw_sc__v2'] = acw_sc_v2
					resp = session.post(
						site['checkin_url'],
						headers=_build_headers(site, user_agent, api_user, is_post=True),
						cookies=cookies,
						timeout=30,
					)
					try:
						data = resp.json()
					except Exception:
						text = (resp.text or '').lower()
						if 'success' in text:
							return 'success', '签到成功'
						if any(kw in text for kw in (k.lower() for k in _ALREADY_KEYWORDS)):
							return 'already', resp.text[:80] or '今日已签到'
						return 'failed', '重试后仍非有效 JSON'
				else:
					return 'failed', 'WAF 解算失败'

			text = (resp.text or '').lower()
			logger.warning(f'[{site["name"]}] 签到响应不是 JSON：{response_summary(resp)}')
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

	cookies = merge_account_cookies(account['cookies'], runtime.get('cookies') or {})
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
			error = 'WAF 探测失败，且该站点配置了 waf=true'
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
