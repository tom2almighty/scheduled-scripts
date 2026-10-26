# NewAPI 多站点签到

呆呆面板脚本：支持多个站点、每站点多个账号，自动签到并对比签到前后余额。

按站点 `provider` 区分三类签到方式：

| provider | 适用站点 | 签到方式 | 账号认证 |
| --- | --- | --- | --- |
| `default` | 通用 NewAPI / OneAPI | `POST /api/user/checkin` | 访问令牌（推荐）、Cookie、OAuth |
| `anyrouter` | AnyRouter | `POST /api/user/sign_in` | Cookie（推荐）、令牌、OAuth |
| `agentrouter` | AgentRouter | OAuth 重新登录，奖励由服务端在登录时发放 | `github_session` 或 `linuxdo_session` |

`provider` 不填时默认 `default`。脚本不做跨类型回退，站点类型配错会直接失败。

## 依赖

在呆呆面板「依赖管理 → Python」中添加：

```bash
curl_cffi loguru
```

无须安装任何 Linux 系统依赖。

## 定时任务

```text
30 9 * * * task newapi/newapi.py
```

或直接在面板「定时任务」中添加任务，命令填写 `task newapi/newapi.py` 或 `python3 newapi/newapi.py`。

## 环境变量

在呆呆面板「环境变量」中配置 `NEWAPI_SITES`，值为站点数组：

```json
[
  {
    "name": "NewAPI 主站",
    "base_url": "https://api.example.com",
    "provider": "default",
    "accounts": [
      {
        "name": "主号",
        "token": "sk-xxxxxxxx",
        "api_user": "1001"
      },
      {
        "name": "备用号",
        "token": "sk-yyyyyyyy"
      }
    ]
  },
  {
    "name": "AnyRouter",
    "base_url": "https://anyrouter.top",
    "provider": "anyrouter",
    "accounts": [
      {
        "name": "主号",
        "cookie": "session=xxxxxxxx"
      }
    ]
  },
  {
    "name": "AgentRouter",
    "base_url": "https://agentrouter.org",
    "provider": "agentrouter",
    "accounts": [
      {
        "name": "GitHub 号",
        "github_session": "GitHub user_session 的值"
      }
    ]
  }
]
```

### 站点字段

- `name`：站点名称，选填，默认从域名提取
- `base_url`：站点地址，必填
- `provider`：站点类型，选填，可选 `default`、`anyrouter`、`agentrouter`，默认 `default`
- `accounts`：账号数组，必填
- `checkin_endpoint`：签到接口路径，选填，默认取对应 provider 的接口
- `status_endpoint`：签到状态接口路径，选填，默认取对应 provider 的接口；显式传空字符串 `""` 表示关闭状态查询
- `user_info_endpoint`：余额接口路径，选填，默认 `/api/user/self`

`checkin_endpoint` 和 `status_endpoint` 一般不用改，只在站点改过接口路径时覆盖。`agentrouter` 没有签到接口，这两个字段对它无效。

### 账号字段

- `name`：账号名称，选填，默认自动编号
- `token`：系统访问令牌（Access Token），在站点「个人设置 - 安全设置 - 系统访问令牌」生成
- `cookie`：站点登录 Cookie，支持 `session=xxx` 字符串或 JSON 对象
- `api_user`：用户 ID，选填；配置后请求会带 `New-Api-User` 头，OAuth 账号可省略，脚本会从登录结果里取
- `github_session`：GitHub 的 `user_session` Cookie 值
- `linuxdo_session`：LinuxDO 的 `_forum_session` Cookie 值
- `github_cookies`：选填，额外 GitHub Cookie（字符串或对象），用于补充 `_gh_sess` 等
- `linuxdo_cookies`：选填，额外 LinuxDO Cookie（字符串或对象），用于补充 `cf_clearance`、`__cf_bm` 等

认证优先级按站点类型固定：

- `default`：`token` > `cookie` > OAuth
- `anyrouter`：`cookie` > `token` > OAuth
- `agentrouter`：只接受 OAuth，必须配置 `github_session` 或 `linuxdo_session`

`github_session` 与 `linuxdo_session` 同时存在时优先 GitHub。

## 配置示例

### 通用 NewAPI：访问令牌

通用站最常见的方式，不需要任何登录态，令牌直接签到：

```json
[
  {
    "name": "NewAPI",
    "base_url": "https://api.example.com",
    "provider": "default",
    "accounts": [
      {
        "name": "主号",
        "token": "sk-xxxxxxxx",
        "api_user": "1001"
      }
    ]
  }
]
```

执行流程：`GET /api/user/self` 取余额 → `GET /api/user/checkin?month=YYYY-MM` 判断今日是否已签 → 未签时 `POST /api/user/checkin` → 成功后再取一次余额。已经签到时不重复请求余额。

新版 NewAPI 的令牌鉴权不再强制 `New-Api-User`；老版本仍需要，遇到 401 时补上 `api_user`。

站点开启了 Turnstile 人机验证时，`POST /api/user/checkin` 会被服务端拦截，脚本只能读到「今日未签到」状态，签到会报失败。这种情况改用 Cookie 或 OAuth 账号也不能绕过，需要站点关闭签到验证。

部分通用站沿用旧接口 `/api/user/sign_in`，把签到接口覆盖掉即可，行为与 AnyRouter 一致：

```json
{
  "name": "旧接口站",
  "base_url": "https://old.example.com",
  "provider": "default",
  "checkin_endpoint": "/api/user/sign_in",
  "status_endpoint": "",
  "accounts": [
    { "name": "主号", "token": "sk-xxxxxxxx" }
  ]
}
```

### 通用 NewAPI：OAuth 登录

```json
[
  {
    "name": "NewAPI OAuth",
    "base_url": "https://api.example.com",
    "provider": "default",
    "accounts": [
      {
        "name": "GitHub 号",
        "github_session": "GitHub user_session 的值"
      }
    ]
  }
]
```

执行流程：用第三方 session 完成 OAuth 登录，拿到站点会话和 `access_token`，再走上面的签到接口流程。新版 NewAPI 的 OAuth state 通过 `POST /api/oauth/state` 返回 `flow_token`；旧版返回签名 state 时脚本会自动切换，回调带不带 `mode=login` 也由脚本判断。

如果站点同时支持访问令牌，直接配 `token` 更省事，不需要维护第三方 session。

### AnyRouter：Cookie

```json
[
  {
    "name": "AnyRouter",
    "base_url": "https://anyrouter.top",
    "provider": "anyrouter",
    "accounts": [
      {
        "name": "主号",
        "cookie": "session=xxxxxxxx",
        "api_user": "12345"
      }
    ]
  }
]
```

Cookie 从浏览器 F12 → Application → Cookies 复制完整 Cookie，至少包含 `session`。AnyRouter 的余额接口需要 `New-Api-User` 头，建议同时配置 `api_user`。

AnyRouter 没有签到状态接口，脚本直接 `POST /api/user/sign_in`，靠响应里的「已签到」文案判断重复签到。

### AgentRouter：OAuth 重新登录

```json
[
  {
    "name": "AgentRouter",
    "base_url": "https://agentrouter.org",
    "provider": "agentrouter",
    "accounts": [
      {
        "name": "GitHub 主号",
        "github_session": "GitHub user_session 的值"
      },
      {
        "name": "LinuxDO 备用",
        "linuxdo_session": "LinuxDO _forum_session 的值"
      }
    ]
  }
]
```

AgentRouter 的签到奖励在 OAuth 重新登录时发放，脚本流程为：读取 `/api/status` 里的 OAuth client_id → 请求 `/api/oauth/state` → 用 session 到第三方授权页换 code → 请求 `/api/oauth/github` 或 `/api/oauth/linuxdo` 完成登录。

只配置 OAuth session 时，站点没有登录态，通知里只有登录后余额。需要对比签到前后变化时，给账号额外配置站点 `cookie`，脚本会先查一次余额再登录。

### 获取 session

GitHub：

1. 浏览器登录 github.com
2. F12 → Application（存储）→ Cookies → `https://github.com`
3. 复制 `user_session` 的值，不要带 `user_session=` 前缀

LinuxDO：

1. 浏览器登录 linux.do
2. F12 → Application → Cookies → `https://linux.do`
3. 复制 `_forum_session` 的值

session 等同于完整登录态，只放在面板环境变量里，不要提交到仓库。

LinuxDO 授权链路会经过 `linux.do/session/sso_provider`，该域名有 Cloudflare 防护，服务器 IP 可能被拦截。优先使用 `github_session`；如果只能用 LinuxDO，可在 `linuxdo_cookies` 中补充同一出口 IP 与 UA 下获取的 `cf_clearance`，但该 Cookie 会过期且绑定 IP/UA，稳定性不如 GitHub。

## 可选环境变量

- `NOTIFY_TRIGGERS`：通知条件，默认 `always`；可选 `always, never, success, failed, balance_changed`
- `NEWAPI_DATA_DIR`：余额记录目录，默认脚本同级目录
- `TZ`：时区，默认 `Asia/Shanghai`
