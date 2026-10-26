#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NewAPI 多站点自动签到 —— 呆呆面板版

cron: 30 9 * * *
new Env('NewAPI 自动签到');

----------------------------------------------------------------------------
实现：多站点 + 多账号 + 签到 + 余额检查 + 余额变化检测 + 面板通知
依赖：curl_cffi loguru（纯 Python，零浏览器依赖，内置阿里 WAF 自动自愈）
通知：复用呆呆面板自带 notify.py 的 send(title, content)

环境变量：
  NEWAPI_SITES          多站点配置，JSON 格式。站点 provider 支持：
                        default     通用 NewAPI：访问令牌直接签到，也支持 Cookie / OAuth
                        anyrouter   Cookie 签到
                        agentrouter OAuth 重新登录签到
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
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# 日志（loguru）
# --------------------------------------------------------------------------- #
try:
	from loguru import logger

	logger.remove()
	logger.add(
		sys.stdout,
		level='INFO',
		format='<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>',
	)
except ImportError:
	class _Fallback:
		def info(self, msg, *a, **k):
			print(f'[INFO] {msg}')

		def success(self, msg, *a, **k):
			print(f'[SUCCESS] {msg}')

		def warning(self, msg, *a, **k):
			print(f'[WARNING] {msg}')

		def error(self, msg, *a, **k):
			print(f'[ERROR] {msg}')

	logger = _Fallback()


# --------------------------------------------------------------------------- #
# 面板通知
# --------------------------------------------------------------------------- #
def _load_notify_send():
	"""加载面板 notify.py 的 send，找不到则返回 None。"""
	try:
		from notify import send

		return send
	except Exception:
		pass

	candidates = [
		os.getenv('DAIDAI_NOTIFY_PY', '') and os.path.dirname(os.environ['DAIDAI_NOTIFY_PY']),
		os.getenv('DAIDAI_SCRIPTS_DIR', ''),
		os.path.dirname(os.path.abspath(__file__)),
	]
	for path in candidates:
		if path and os.path.isfile(os.path.join(path, 'notify.py')) and path not in sys.path:
			sys.path.append(path)
			try:
				from notify import send

				return send
			except Exception:
				continue
	return None


NOTIFY_SEND = _load_notify_send()


def notify(title: str, content: str):
	"""发送通知：优先走面板 notify.send，否则仅打印到日志。"""
	if NOTIFY_SEND:
		try:
			NOTIFY_SEND(title, content)
			logger.info('已触发面板通知推送')
			return
		except Exception as e:
			logger.warning(f'面板通知推送失败：{e}，回退到日志输出')
	logger.info(f'\n【{title}】\n{content}')


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
QUOTA_PER_DOLLAR = 500000
PROVIDERS = {
	'default': {
		'user_info_endpoint': '/api/user/self',
		'checkin_endpoint': '/api/user/checkin',
		'status_endpoint': '/api/user/checkin',
	},
	'anyrouter': {
		'user_info_endpoint': '/api/user/self',
		'checkin_endpoint': '/api/user/sign_in',
		'status_endpoint': None,
	},
	'agentrouter': {
		'user_info_endpoint': '/api/user/self',
		'checkin_endpoint': None,
		'status_endpoint': None,
	},
}

DATA_DIR = os.getenv('NEWAPI_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
BALANCE_FILE = os.path.join(DATA_DIR, 'newapi_balance.json')

_ALREADY_KEYWORDS = ('已签到', '已经签', '重复', 'already', 'signed', '今日已')
WAF_KEY_DEFAULT = '3000176000856006061501533003690027800375'
WAF_MASK = (
	15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25, 13,
	6, 11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3, 28, 34,
	37, 12, 36,
)

GITHUB_AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
LINUXDO_AUTHORIZE_URL = 'https://connect.linux.do/oauth2/authorize'
OAUTH_LABELS = {'github': 'GitHub', 'linuxdo': 'LinuxDO'}
OAUTH_MAX_HOPS = 10
_STATUS_CACHE = {}


# --------------------------------------------------------------------------- #
# 阿里 WAF 纯算法秒解
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 配置读取与标准化
# --------------------------------------------------------------------------- #
def _strip(value) -> str:
	return str(value or '').strip()


def normalize_base_url(value: str) -> str:
	base_url = _strip(value).rstrip('/')
	if not base_url:
		return ''
	if not (base_url.startswith('http://') or base_url.startswith('https://')):
		base_url = f'https://{base_url}'
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


def site_host(base_url: str) -> str:
	return urlparse(base_url).netloc or base_url


def parse_cookies(cookies_data) -> dict:
	"""将 cookie 配置解析为 dict，支持对象与 'k=v; k2=v2' 字符串。"""
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


def _normalize_account(raw: dict, site_provider: str, site_name: str, site_index: int, account_index: int):
	if not isinstance(raw, dict):
		logger.warning(f'[{site_name}] 跳过无效账号配置：账号必须是对象')
		return None

	name = _strip(raw.get('name')) or f'账号{account_index + 1}'
	token = _strip(raw.get('token'))
	raw_cookie = raw.get('cookie')
	cookies = parse_cookies(raw_cookie) if raw_cookie else {}
	github_session = _strip(raw.get('github_session'))
	linuxdo_session = _strip(raw.get('linuxdo_session'))
	oauth_provider = 'github' if github_session else ('linuxdo' if linuxdo_session else '')
	has_oauth = bool(oauth_provider)

	if not token and not cookies and not has_oauth:
		logger.warning(f'[{site_name}/{name}] 跳过账号：缺少 token、cookie、github_session 或 linuxdo_session')
		return None

	# 认证优先级：AgentRouter 只认 OAuth；通用站优先访问令牌，其次 Cookie，最后 OAuth
	if site_provider == 'agentrouter':
		if not has_oauth:
			logger.warning(
				f'[{site_name}/{name}] 跳过账号：AgentRouter 需要配置 github_session 或 linuxdo_session'
			)
			return None
		auth_mode = 'oauth'
	elif site_provider == 'default':
		auth_mode = 'token' if token else ('cookie' if cookies else ('oauth' if has_oauth else ''))
	else:
		auth_mode = 'cookie' if cookies else ('token' if token else ('oauth' if has_oauth else ''))

	if auth_mode == 'token':
		ident = token
	elif auth_mode == 'oauth':
		ident = f'{oauth_provider}:{github_session or linuxdo_session}'
	elif auth_mode == 'cookie':
		ident = json.dumps(cookies, sort_keys=True)
	else:
		ident = f'{site_name}:{name}:{account_index}'

	return {
		'name': name,
		'token': token,
		'cookies': cookies,
		'api_user': _strip(raw.get('api_user')),
		'github_session': github_session,
		'linuxdo_session': linuxdo_session,
		'github_cookies': parse_cookies(raw.get('github_cookies')) if raw.get('github_cookies') else {},
		'linuxdo_cookies': parse_cookies(raw.get('linuxdo_cookies')) if raw.get('linuxdo_cookies') else {},
		'oauth_provider': oauth_provider,
		'auth_mode': auth_mode,
		'ident_hash': hashlib.sha256(ident.encode('utf-8')).hexdigest(),
		'index': account_index,
		'site_index': site_index,
	}


def _resolve_endpoint(raw: dict, key: str, default):
	"""读取可覆盖的接口路径；显式传空字符串表示禁用该接口。"""
	if key not in raw:
		return default
	value = _strip(raw.get(key))
	return value or None


def _normalize_site(raw: dict, index: int):
	if not isinstance(raw, dict):
		logger.warning(f'跳过无效站点配置：第 {index + 1} 项必须是对象')
		return None

	base_url = normalize_base_url(raw.get('base_url'))
	if not base_url:
		logger.warning(f'跳过无效站点配置：第 {index + 1} 项缺少有效 base_url')
		return None

	name = _strip(raw.get('name')) or site_host(base_url)
	provider = _strip(raw.get('provider')).lower() or 'default'
	if provider not in PROVIDERS:
		logger.warning(f'[{name}] 未知站点类型 provider={provider}，可选值：{", ".join(PROVIDERS)}')
		return None

	defaults = PROVIDERS[provider]
	user_info_endpoint = normalize_path(raw.get('user_info_endpoint'), defaults['user_info_endpoint'])
	checkin_endpoint = _resolve_endpoint(raw, 'checkin_endpoint', defaults['checkin_endpoint'])
	status_endpoint = _resolve_endpoint(raw, 'status_endpoint', defaults['status_endpoint'])

	raw_accounts = raw.get('accounts')
	if not isinstance(raw_accounts, list) or not raw_accounts:
		logger.warning(f'[{name}] 跳过站点：accounts 必须是非空数组')
		return None

	accounts = []
	seen = set()
	for account_index, account in enumerate(raw_accounts):
		normalized = _normalize_account(account, provider, name, index, account_index)
		if not normalized:
			continue

		dedup_key = (
			normalized['name'],
			normalized['api_user'],
			normalized['ident_hash'],
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
		'provider': provider,
		'checkin_url': build_url(base_url, checkin_endpoint) if checkin_endpoint else None,
		'status_url': build_url(base_url, status_endpoint) if status_endpoint else None,
		'user_info_url': build_url(base_url, user_info_endpoint),
		'accounts': accounts,
		'index': index,
	}


def load_sites() -> list:
	raw = os.getenv('NEWAPI_SITES', '').strip()
	if not raw:
		logger.error('未找到 NEWAPI_SITES 环境变量')
		logger.info(
			'示例：NEWAPI_SITES = '
			'[{"name":"新站点","base_url":"https://example.com","accounts":[{"name":"主号","token":"sk-xxx"}]}]'
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

		dedup_key = (
			site['base_url'],
			site['provider'],
			site['checkin_url'],
			site['status_url'],
			site['user_info_url'],
		)
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
	identity = (
		account.get('api_user')
		or account.get('token')
		or account.get('ident_hash')
		or f'{account["name"]}#{account["index"]}'
	)
	raw = f'{site["base_url"]}|{identity}'
	return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def balance_hash(quota, used) -> str:
	return hashlib.sha256(f'{quota}_{used}'.encode('utf-8')).hexdigest()


def load_balance_record():
	"""读取上次的余额记录，首次运行返回 None。"""
	if not os.path.isfile(BALANCE_FILE):
		return None
	try:
		with open(BALANCE_FILE, 'r', encoding='utf-8') as f:
			data = json.load(f)
		return data if isinstance(data, dict) else None
	except Exception as e:
		logger.warning(f'读取余额记录失败：{e}')
		return None


def save_balance_record(record: dict):
	try:
		os.makedirs(os.path.dirname(os.path.abspath(BALANCE_FILE)), exist_ok=True)
		with open(BALANCE_FILE, 'w', encoding='utf-8') as f:
			json.dump(record, f, ensure_ascii=False, indent=2)
	except Exception as e:
		logger.warning(f'保存余额记录失败：{e}')


# --------------------------------------------------------------------------- #
# 网络请求与 WAF 自愈重试
# --------------------------------------------------------------------------- #
def default_user_agent() -> str:
	return (
		'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
		'(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
	)


def _build_headers(ctx: dict, is_post: bool = False) -> dict:
	headers = {
		'User-Agent': default_user_agent(),
		'Accept': 'application/json, text/plain, */*',
		'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
		'Connection': 'keep-alive',
		'Sec-Fetch-Dest': 'empty',
		'Sec-Fetch-Mode': 'cors',
		'Sec-Fetch-Site': 'same-origin',
	}
	token = _strip(ctx.get('access_token')) or _strip(ctx.get('token'))
	if token:
		headers['Authorization'] = f'Bearer {token}'
	api_user = _strip(ctx.get('api_user'))
	if api_user:
		headers['New-Api-User'] = api_user
	if is_post:
		headers['Content-Type'] = 'application/json'
		headers['X-Requested-With'] = 'XMLHttpRequest'
	return headers


def request_with_waf_retry(
	session, method: str, url: str, headers: dict, cookies: dict, json_data=None, params=None
):
	"""发送 HTTP 请求，若触发阿里 WAF 挑战则纯 Python 自动解算并重试一次。"""
	kwargs = {
		'headers': headers,
		'cookies': cookies,
		'timeout': 30,
	}
	if params is not None:
		kwargs['params'] = params
	if json_data is not None:
		kwargs['json'] = json_data

	try:
		resp = session.request(method, url, **kwargs)
	except Exception as e:
		return None, str(e)

	# 检查是否返回了阿里 WAF 拦截页面
	html = getattr(resp, 'text', '') or ''
	arg1, key = extract_waf_challenge(html)
	if arg1:
		acw_sc_v2 = calc_acw_sc_v2(arg1, key)
		if acw_sc_v2:
			cookies['acw_sc__v2'] = acw_sc_v2
			if hasattr(resp, 'cookies') and resp.cookies:
				for k, v in resp.cookies.items():
					cookies[k] = v
			kwargs['cookies'] = cookies
			try:
				resp = session.request(method, url, **kwargs)
			except Exception as e:
				return None, str(e)

	return resp, None


def get_user_info(session, site: dict, account: dict, ctx: dict, cookies: dict):
	"""查询账号余额，同时用响应里的用户 ID 补全 api_user。"""
	headers = _build_headers(ctx, is_post=False)
	resp, err = request_with_waf_retry(session, 'GET', site['user_info_url'], headers, cookies)
	if err or resp is None:
		logger.warning(f'[{site["name"]}/{account["name"]}] 获取用户信息网络异常：{err}')
		return None

	if resp.status_code != 200:
		logger.warning(f'[{site["name"]}/{account["name"]}] 获取用户信息失败：HTTP {resp.status_code}')
		return None

	try:
		data = resp.json()
	except Exception:
		logger.warning(f'[{site["name"]}/{account["name"]}] 用户信息响应非 JSON')
		return None

	if not (data.get('success') or data.get('ret') == 1 or data.get('code') == 0):
		msg = data.get('message') or data.get('msg') or '接口返回失败'
		logger.warning(f'[{site["name"]}/{account["name"]}] 获取用户信息失败：{msg}')
		return None

	user = data.get('data') or {}
	api_user = _strip(user.get('id'))
	if api_user and not _strip(ctx.get('api_user')):
		ctx['api_user'] = api_user
	return {
		'quota': round(float(user.get('quota', 0) or 0) / QUOTA_PER_DOLLAR, 2),
		'used': round(float(user.get('used_quota', 0) or 0) / QUOTA_PER_DOLLAR, 2),
	}


def get_checkin_status(session, site: dict, ctx: dict, cookies: dict):
	"""查询今日签到状态：True 已签、False 未签、None 无状态接口或查询失败。"""
	if not site.get('status_url'):
		return None

	tz_name = os.getenv('TZ') or 'Asia/Shanghai'
	try:
		month = datetime.now(ZoneInfo(tz_name)).strftime('%Y-%m')
	except Exception:
		month = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m')

	resp, err = request_with_waf_retry(
		session,
		'GET',
		site['status_url'],
		_build_headers(ctx),
		cookies,
		params={'month': month},
	)
	if err or resp is None or resp.status_code != 200:
		return None
	try:
		data = resp.json()
	except Exception:
		return None
	if not (data.get('success') or data.get('ret') == 1 or data.get('code') == 0):
		return None

	stats = ((data.get('data') or {}).get('stats') or {}) if isinstance(data.get('data'), dict) else {}
	value = stats.get('checked_in_today')
	return bool(value) if value is not None else None


def run_api_checkin(session, site: dict, ctx: dict, cookies: dict):
	"""调用站点签到接口（NewAPI: /api/user/checkin，AnyRouter: /api/user/sign_in）。"""
	endpoint = site.get('checkin_url')
	if not endpoint:
		return 'failed', '站点未配置签到接口'

	if get_checkin_status(session, site, ctx, cookies) is True:
		return 'already', '今日已签到'

	resp, err = request_with_waf_retry(
		session, 'POST', endpoint, _build_headers(ctx, is_post=True), cookies, json_data={}
	)
	if err or resp is None:
		return 'failed', f'网络异常：{err}'

	if resp.status_code != 200:
		return 'failed', f'HTTP {resp.status_code}'

	try:
		data = resp.json()
	except Exception:
		text = (resp.text or '').lower()
		if 'success' in text:
			return 'success', '签到成功'
		if any(kw in text for kw in _ALREADY_KEYWORDS):
			return 'already', (resp.text or '')[:80].strip() or '今日已签到'
		return 'failed', '响应非有效 JSON 格式'

	msg = str(data.get('message') or data.get('msg') or data.get('error') or '')
	is_success = bool(data.get('success') or data.get('ret') == 1 or data.get('code') == 0)

	if is_success:
		# 新版 NewAPI 官方返回格式包含 data: { quota_awarded: 500000, checkin_date: '...' }
		res_data = data.get('data')
		extra = ''
		if isinstance(res_data, dict) and 'quota_awarded' in res_data:
			awarded = res_data.get('quota_awarded', 0)
			dollars = round(float(awarded) / QUOTA_PER_DOLLAR, 2)
			extra = f'（获得 ${dollars}）'
		return 'success', f'{msg or "签到成功"}{extra}'

	if any(kw in msg.lower() for kw in _ALREADY_KEYWORDS):
		return 'already', msg or '今日已签到'

	return 'failed', msg or '签到失败'


# --------------------------------------------------------------------------- #
# OAuth 重新登录签到（GitHub / LinuxDO session）
# --------------------------------------------------------------------------- #
def build_github_cookies(account: dict) -> dict:
	session_value = account.get('github_session') or ''
	cookies = {
		'user_session': session_value,
		'logged_in': 'yes',
		'__Host-user_session_same_site': session_value,
	}
	cookies.update(account.get('github_cookies') or {})
	return cookies


def build_linuxdo_cookies(account: dict) -> dict:
	cookies = {'_forum_session': account.get('linuxdo_session') or ''}
	cookies.update(account.get('linuxdo_cookies') or {})
	return cookies


def extract_oauth_code(value: str) -> str:
	"""从重定向地址或 HTML 中提取 OAuth code，找不到返回空字符串。"""
	if not value or 'access_denied' in value:
		return ''
	if value.startswith(('http://', 'https://', '/')):
		codes = parse_qs(urlparse(value).query).get('code')
		if codes:
			return codes[0]
	match = re.search(r'code(?:=|%3D)([A-Za-z0-9_-]+)', value)
	return match.group(1) if match else ''


def _sync_session_cookies(session, cookies: dict) -> None:
	"""把 curl_cffi 会话中由回调写入的 cookie 同步到请求字典。"""
	try:
		for cookie in session.cookies.jar:
			if cookie.name and cookie.value is not None:
				cookies[cookie.name] = cookie.value
	except Exception:
		pass


def get_oauth_client_id(session, site: dict, provider: str, cookies: dict) -> str:
	"""从站点 /api/status 读取 OAuth client_id，避免写死。"""
	cache_key = f'{site["base_url"]}|{provider}'
	if cache_key in _STATUS_CACHE:
		return _STATUS_CACHE[cache_key]

	url = build_url(site['base_url'], '/api/status')
	resp, err = request_with_waf_retry(session, 'GET', url, _build_headers({}), cookies)
	if err or resp is None:
		raise RuntimeError(f'获取站点状态失败：{err}')
	if resp.status_code != 200:
		raise RuntimeError(f'获取站点状态失败：HTTP {resp.status_code}')
	try:
		data = (resp.json() or {}).get('data') or {}
	except Exception:
		raise RuntimeError(f'获取站点状态失败：响应非 JSON（HTTP {resp.status_code}）')

	field = f'{provider}_client_id'
	client_id = _strip(data.get(field))
	if not client_id:
		raise RuntimeError(f'站点未开启 {OAUTH_LABELS[provider]} 登录或 /api/status 未返回 {field}')
	_STATUS_CACHE[cache_key] = client_id
	return client_id


def fetch_oauth_state(session, site: dict, provider: str, cookies: dict):
	"""获取 OAuth state，返回 (state, legacy)。

	新版 NewAPI 用 POST {provider, intent} 返回 flow_token；旧版（AgentRouter）用 GET 返回签名 state。
	"""
	url = build_url(site['base_url'], '/api/oauth/state')

	resp, err = request_with_waf_retry(
		session,
		'POST',
		url,
		_build_headers({}, is_post=True),
		cookies,
		json_data={'provider': provider, 'intent': 'login'},
	)
	if not err and resp is not None and resp.status_code == 200:
		try:
			body = resp.json()
		except Exception:
			body = None
		if isinstance(body, dict) and body.get('success'):
			payload = body.get('data')
			if isinstance(payload, dict):
				payload = payload.get('flow_token')
			state = _strip(payload)
			if state:
				return state, False

	resp, err = request_with_waf_retry(session, 'GET', url, _build_headers({}), cookies)
	if err or resp is None:
		raise RuntimeError(f'获取 OAuth state 失败：{err}')
	if resp.status_code != 200:
		raise RuntimeError(f'获取 OAuth state 失败：HTTP {resp.status_code}')
	try:
		data = resp.json()
	except Exception:
		text = (resp.text or '').lower()
		hint = '（疑似被阿里 WAF 拦截）' if 'aliyun_waf' in text or 'acw_sc__v2' in text else ''
		raise RuntimeError(f'获取 OAuth state 失败：响应非 JSON（HTTP {resp.status_code}）{hint}')
	if not (data.get('success') and data.get('data')):
		raise RuntimeError(f'获取 OAuth state 失败：{data.get("message") or "未知错误"}')
	return _strip(data['data']), True


def _authorize_github(session, client_id: str, state: str, account: dict) -> str:
	cookies = build_github_cookies(account)
	params = {'client_id': client_id, 'state': state, 'scope': 'user:email'}

	resp = session.get(
		GITHUB_AUTHORIZE_URL,
		params=params,
		cookies=cookies,
		allow_redirects=False,
		timeout=30,
	)
	location = resp.headers.get('location') or ''

	if resp.status_code in (301, 302, 303, 307, 308):
		code = extract_oauth_code(location)
		if code:
			return code
		if '/login' in location:
			raise RuntimeError('GitHub 登录态无效或已过期，请更新 github_session')
		raise RuntimeError(f'GitHub 未返回授权码，跳转地址：{location[:150]}')

	if resp.status_code != 200:
		raise RuntimeError(f'GitHub 授权请求失败：HTTP {resp.status_code}')

	html = resp.text or ''
	code = extract_oauth_code(html)
	if code:
		return code

	# 首次授权会返回确认表单，提交 authorize=1 与隐藏字段
	action_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.I)
	if not action_match:
		title_match = re.search(r'<title>(.*?)</title>', html, re.I | re.S)
		title = (title_match.group(1).strip() if title_match else '无标题')[:80]
		raise RuntimeError(f'GitHub 未返回授权码（HTTP {resp.status_code}，页面标题：{title}）')

	action = action_match.group(1)
	if '/login/oauth/authorize' not in action:
		raise RuntimeError(f'GitHub 返回了非授权页面（form action: {action[:120]}），请检查 github_session 是否有效')
	if not action.startswith('http'):
		action = f'https://github.com{action if action.startswith("/") else "/" + action}'

	payload = {'authorize': '1'}
	for tag in re.findall(r'<input[^>]*>', html, re.I):
		if 'hidden' not in tag.lower():
			continue
		name_match = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
		value_match = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
		if name_match:
			payload[name_match.group(1)] = value_match.group(1) if value_match else ''

	post_resp = session.post(
		action,
		data=payload,
		cookies=cookies,
		allow_redirects=False,
		timeout=30,
	)
	code = extract_oauth_code(post_resp.headers.get('location') or '')
	if not code:
		code = extract_oauth_code(post_resp.text or '')
	if code:
		return code
	raise RuntimeError(f'GitHub 授权确认失败（HTTP {post_resp.status_code}）')


def _authorize_linuxdo(session, client_id: str, state: str, account: dict) -> str:
	"""手动跟随 LinuxDO SSO 跳转，在拿到回调 code 时立即停止，避免 code 被提前消费。"""
	cookies = build_linuxdo_cookies(account)
	params = {'response_type': 'code', 'client_id': client_id, 'state': state}
	url = LINUXDO_AUTHORIZE_URL
	resp = None

	for hop in range(OAUTH_MAX_HOPS):
		resp = session.get(
			url,
			params=params if hop == 0 else None,
			cookies=cookies,
			allow_redirects=False,
			timeout=30,
		)
		location = resp.headers.get('location') or ''
		code = extract_oauth_code(location)
		if code:
			return code
		if resp.status_code in (301, 302, 303, 307, 308) and location:
			url = urljoin(url, location)
			continue
		break

	body = (resp.text or '') if resp is not None else ''
	body_lower = body.lower()
	if '/login' in url or 'login-account-name' in body:
		raise RuntimeError('LinuxDO 登录态无效或已过期，请更新 linuxdo_session')
	if 'just a moment' in body_lower or 'cf-chl' in body_lower or 'cf_chl_opt' in body_lower:
		raise RuntimeError(
			'LinuxDO 被 Cloudflare 拦截；建议优先使用 github_session，'
			'或在 linuxdo_cookies 中补充同一出口 IP 与 UA 下的 cf_clearance'
		)
	if resp is not None and resp.status_code in (403, 429, 503):
		raise RuntimeError(f'LinuxDO 授权被拦截（HTTP {resp.status_code}）；请稍后重试或改用 github_session')
	if resp is not None and resp.status_code == 200:
		raise RuntimeError('LinuxDO 未直接返回授权码，可能停在授权确认页；请先在浏览器完成一次授权')
	status = resp.status_code if resp is not None else 'N/A'
	raise RuntimeError(f'LinuxDO 授权失败（HTTP {status}）')


def _oauth_callback(
	session, site: dict, provider: str, code: str, state: str, cookies: dict, legacy: bool
):
	url = build_url(site['base_url'], f'/api/oauth/{provider}')
	params = {'code': code, 'state': state}
	if legacy:
		params['mode'] = 'login'
	resp, err = request_with_waf_retry(
		session, 'GET', url, _build_headers({}), cookies, params=params
	)
	if err or resp is None:
		return False, {}, f'OAuth 回调网络异常：{err}'
	if resp.status_code != 200:
		return False, {}, f'OAuth 回调失败：HTTP {resp.status_code}'
	try:
		body = resp.json()
	except Exception:
		return False, {}, 'OAuth 回调响应非 JSON'
	if not body.get('success'):
		return False, {}, body.get('message') or 'OAuth 回调登录失败'
	return True, body.get('data') or {}, body.get('message') or ''


def _oauth_login(session, site: dict, account: dict, ctx: dict, cookies: dict) -> dict:
	"""用 GitHub / LinuxDO session 完成一次站点 OAuth 登录，返回回调数据。

	登录成功后把 access_token、api_user 写入 ctx，站点会话 Cookie 同步进 cookies。
	"""
	provider = account['oauth_provider']
	label = OAUTH_LABELS[provider]
	logger.info(f'[{site["name"]}/{account["name"]}] 使用 {label} session 登录')

	client_id = get_oauth_client_id(session, site, provider, cookies)
	state, legacy = fetch_oauth_state(session, site, provider, cookies)
	if provider == 'github':
		code = _authorize_github(session, client_id, state, account)
	else:
		code = _authorize_linuxdo(session, client_id, state, account)

	ok, data, message = _oauth_callback(session, site, provider, code, state, cookies, legacy)
	if not ok:
		raise RuntimeError(message)

	_sync_session_cookies(session, cookies)

	access_token = _strip(data.get('access_token'))
	if access_token:
		ctx['access_token'] = access_token

	user = data.get('user') if isinstance(data.get('user'), dict) else {}
	api_user = _strip(data.get('id') or user.get('id'))
	if api_user:
		ctx['api_user'] = api_user

	if message:
		logger.info(f'[{site["name"]}/{account["name"]}] OAuth 登录：{message}')
	return data


def _read_balance(session, site: dict, account: dict, ctx: dict, cookies: dict, label: str):
	info = get_user_info(session, site, account, ctx, cookies)
	if info:
		logger.info(
			f'[{site["name"]}/{account["name"]}] {label}余额：'
			f'${info["quota"]}（已用 ${info["used"]}）'
		)
	return info


def _fill_balance(result: dict, info):
	if info:
		result['quota'] = info['quota']
		result['used'] = info['used']


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


def check_in_account(site: dict, account: dict) -> dict:
	"""单个账号：登录 / 签到 / 余额对比。"""
	try:
		from curl_cffi import requests as cffi_requests
	except ImportError:
		logger.error('缺少依赖 curl_cffi，请在呆呆面板「依赖管理 → Python」中安装 curl_cffi')
		return empty_result(site, account, error='缺少依赖 curl_cffi')

	name = account['name']
	auth = account.get('auth_mode') or 'token'
	cookies = dict(account['cookies'])
	ctx = {
		'token': (account.get('token') or '') if auth == 'token' else '',
		'access_token': '',
		'api_user': account.get('api_user') or '',
	}
	session = cffi_requests.Session(impersonate='chrome')
	result = empty_result(site, account)

	try:
		label = {'oauth': 'OAuth', 'cookie': 'Cookie', 'token': '令牌'}.get(auth, auth)
		logger.info(f'==== 开始处理 [{site["name"]}/{name}]（{label}）====')

		try:
			if site['provider'] == 'agentrouter':
				if cookies:
					_fill_balance(
						result, _read_balance(session, site, account, ctx, cookies, '当前')
					)
				else:
					logger.info(f'[{site["name"]}/{name}] 未配置站点 cookie，跳过签到前余额基线')

				data = _oauth_login(session, site, account, ctx, cookies)
				result['api_user'] = ctx.get('api_user') or result['api_user']
				_fill_balance(
					result, _read_balance(session, site, account, ctx, cookies, '签到后')
				)

				if data.get('checked_in') is False:
					result['status'] = 'already'
					result['message'] = '登录成功，今日已签到'
					logger.info(f'[{site["name"]}/{name}] {result["message"]}')
				else:
					result['status'] = 'success'
					result['message'] = 'OAuth 重新登录完成，签到奖励已发放'
					logger.success(f'[{site["name"]}/{name}] {result["message"]}')
				return result

			if auth == 'oauth':
				_oauth_login(session, site, account, ctx, cookies)
				result['api_user'] = ctx.get('api_user') or result['api_user']

			_fill_balance(result, _read_balance(session, site, account, ctx, cookies, '当前'))

			status, message = run_api_checkin(session, site, ctx, cookies)
			result['status'] = status
			result['message'] = message
			if status == 'failed':
				result['error'] = message
				logger.error(f'[{site["name"]}/{name}] 签到失败：{message}')
				return result

			if status == 'success':
				_fill_balance(result, _read_balance(session, site, account, ctx, cookies, '签到后'))
				logger.success(f'[{site["name"]}/{name}] 签到成功：{message}')
			else:
				logger.info(f'[{site["name"]}/{name}] 今日已签到：{message}')
			return result

		except Exception as e:
			result['status'] = 'failed'
			result['error'] = str(e)[:200]
			logger.error(f'[{site["name"]}/{name}] 处理失败：{result["error"]}')
			return result
	finally:
		try:
			session.close()
		except Exception:
			pass


# --------------------------------------------------------------------------- #
# 通知与主流程
# --------------------------------------------------------------------------- #
def parse_triggers() -> set:
	raw = os.getenv('NOTIFY_TRIGGERS', '').strip().lower()
	if not raw:
		return {'always'}
	triggers = {t.strip() for t in raw.split(',') if t.strip()}
	return triggers or {'always'}


def should_notify(triggers, has_success, has_failed, has_balance_changed, is_first_run) -> bool:
	if 'never' in triggers:
		return False
	if 'always' in triggers:
		return True
	if 'success' in triggers and has_success:
		return True
	if 'failed' in triggers and has_failed:
		return True
	if 'balance_changed' in triggers and (has_balance_changed or is_first_run):
		return True
	return False


def build_notification(results: list, timestamp: str):
	total = len(results)
	success_count = sum(1 for r in results if r['status'] in ('success', 'already'))
	failed_count = total - success_count

	status_icon = '✅' if failed_count == 0 else ('⚠️' if success_count > 0 else '❌')
	title = f'NewAPI 签到 {status_icon} 成功 {success_count}/{total}'

	lines = [
		f'时间：{timestamp}',
		f'统计：共 {total} 个账号，成功 {success_count}，失败 {failed_count}',
		'',
		'── 详细结果 ──',
	]

	for r in results:
		site_label = f'{r["site_name"]} ({r["host"]})'
		user_label = r['name']
		if r.get('api_user'):
			user_label += f' [ID: {r["api_user"]}]'

		icon = '✅' if r['status'] == 'success' else ('ℹ️' if r['status'] == 'already' else '❌')
		lines.append(f'{icon} 【{site_label}】{user_label}')

		if r['status'] == 'failed':
			reason = r.get('error') or r.get('message') or '未知错误'
			lines.append(f'    失败原因：{reason}')
		else:
			if r['quota'] is not None:
				mark = '  🔔余额有变化' if r['balance_changed'] is True else ''
				lines.append(f'    余额：${r["quota"]}（已用 ${r["used"]}）{mark}')
			if r['status'] == 'already':
				lines.append(f'    {r["message"]}')
			elif r['status'] == 'success' and r.get('message'):
				lines.append(f'    {r["message"]}')

	return title, '\n'.join(lines).strip()


def main():
	logger.info('NewAPI 自动签到脚本启动（呆呆面板版）')

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
		for account in site['accounts']:
			try:
				result = check_in_account(site, account)
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
