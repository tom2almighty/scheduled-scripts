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
  NEWAPI_SITES          多站点配置，JSON 格式，支持 Access Token 或 Cookie
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
DEFAULT_CHECKIN_ENDPOINT = '/api/user/checkin'
FALLBACK_CHECKIN_ENDPOINT = '/api/user/sign_in'
DEFAULT_USER_INFO_ENDPOINT = '/api/user/self'

DATA_DIR = os.getenv('NEWAPI_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
BALANCE_FILE = os.path.join(DATA_DIR, 'newapi_balance.json')

_ALREADY_KEYWORDS = ('已签到', '已经签', '重复', 'already', 'signed', '今日已')
WAF_KEY_DEFAULT = '3000176000856006061501533003690027800375'
WAF_MASK = (
	15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25, 13,
	6, 11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3, 28, 34,
	37, 12, 36,
)


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


def _normalize_account(raw: dict, site_name: str, site_index: int, account_index: int):
	if not isinstance(raw, dict):
		logger.warning(f'[{site_name}] 跳过无效账号配置：账号必须是对象')
		return None

	name = _strip(raw.get('name')) or f'账号{account_index + 1}'
	token = _strip(raw.get('token'))
	raw_cookie = raw.get('cookie')
	cookies = parse_cookies(raw_cookie) if raw_cookie else {}

	if not token and not cookies:
		logger.warning(f'[{site_name}/{name}] 跳过账号：缺少 token 或 cookie')
		return None

	ident = token or json.dumps(cookies, sort_keys=True)
	return {
		'name': name,
		'token': token,
		'cookies': cookies,
		'api_user': _strip(raw.get('api_user')),
		'ident_hash': hashlib.sha256(ident.encode('utf-8')).hexdigest(),
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
	raw_checkin = _strip(raw.get('checkin_endpoint'))
	checkin_endpoint = normalize_path(raw_checkin, DEFAULT_CHECKIN_ENDPOINT)
	user_info_endpoint = normalize_path(raw.get('user_info_endpoint'), DEFAULT_USER_INFO_ENDPOINT)

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
		'checkin_url': build_url(base_url, checkin_endpoint),
		'user_info_url': build_url(base_url, user_info_endpoint),
		'custom_checkin': bool(raw_checkin),
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
	identity = account.get('api_user') or account.get('token') or f'{account["name"]}#{account["index"]}'
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


def _build_headers(account: dict, is_post: bool = False) -> dict:
	headers = {
		'User-Agent': default_user_agent(),
		'Accept': 'application/json, text/plain, */*',
		'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
		'Connection': 'keep-alive',
		'Sec-Fetch-Dest': 'empty',
		'Sec-Fetch-Mode': 'cors',
		'Sec-Fetch-Site': 'same-origin',
	}
	token = account.get('token')
	if token:
		headers['Authorization'] = f'Bearer {token}'
	api_user = account.get('api_user')
	if api_user:
		headers['New-Api-User'] = str(api_user)
	if is_post:
		headers['Content-Type'] = 'application/json'
		headers['X-Requested-With'] = 'XMLHttpRequest'
	return headers


def request_with_waf_retry(session, method: str, url: str, headers: dict, cookies: dict, json_data=None):
	"""发送 HTTP 请求，若触发阿里 WAF 挑战则纯 Python 自动解算并重试一次。"""
	kwargs = {
		'headers': headers,
		'cookies': cookies,
		'timeout': 30,
	}
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


def get_user_info(session, site: dict, account: dict, cookies: dict):
	"""查询账号余额，返回 {'quota', 'used'} 或 None。"""
	headers = _build_headers(account, is_post=False)
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
	return {
		'quota': round(float(user.get('quota', 0) or 0) / QUOTA_PER_DOLLAR, 2),
		'used': round(float(user.get('used_quota', 0) or 0) / QUOTA_PER_DOLLAR, 2),
	}


def do_checkin(session, site: dict, account: dict, cookies: dict):
	"""执行签到，优先使用配置/默认端点，遇到 404 自适应 fallback 到旧版接口。"""
	endpoint = site['checkin_url']
	headers = _build_headers(account, is_post=True)

	resp, err = request_with_waf_retry(session, 'POST', endpoint, headers, cookies, json_data={})
	if err or resp is None:
		return 'failed', f'网络异常：{err}'

	# 若默认端点 404 且未手动自定义，自适应回退到 /api/user/sign_in
	if resp.status_code == 404 and not site.get('custom_checkin'):
		fallback_url = build_url(site['base_url'], FALLBACK_CHECKIN_ENDPOINT)
		resp_fb, err_fb = request_with_waf_retry(session, 'POST', fallback_url, headers, cookies, json_data={})
		if not err_fb and resp_fb and resp_fb.status_code != 404:
			resp = resp_fb

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
	"""单个账号：查余额 + 签到。"""
	try:
		from curl_cffi import requests as cffi_requests
	except ImportError:
		logger.error('缺少依赖 curl_cffi，请在呆呆面板「依赖管理 → Python」中安装 curl_cffi')
		return empty_result(site, account, error='缺少依赖 curl_cffi')

	result = empty_result(site, account)
	name = account['name']
	logger.info(f'==== 开始处理 [{site["name"]}/{name}] ====')

	cookies = dict(account['cookies'])
	session = cffi_requests.Session(impersonate='chrome')
	try:
		info = get_user_info(session, site, account, cookies)
		if info:
			result['quota'] = info['quota']
			result['used'] = info['used']
			logger.info(f'[{site["name"]}/{name}] 当前余额：${info["quota"]}（已用 ${info["used"]}）')

		status, message = do_checkin(session, site, account, cookies)
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
