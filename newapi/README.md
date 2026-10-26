# NewAPI 多站点签到

呆呆面板脚本：支持多个 NewAPI 站点、每站点多个账号，自动签到并对比签到前后余额。

站点分两种：

| provider | 适用站点 | 账号认证 |
| --- | --- | --- |
| `default` | 通用 NewAPI / OneAPI（含 AgentRouter） | `token` 或 `username` + `password` |
| `anyrouter` | AnyRouter | `cookie` |

`provider` 不填时默认 `default`。脚本不做跨类型回退，站点类型配错会直接失败。

旧配置里写过 `"provider": "agentrouter"` 的，改成 `"provider": "default"` 或删掉该字段，否则脚本会按未知类型跳过整个站点。

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
    "accounts": [
      {
        "name": "主号",
        "username": "you@example.com",
        "password": "你的登录密码"
      }
    ]
  }
]
```

### 站点字段

- `name`：站点名称，选填，默认从域名提取
- `base_url`：站点地址，必填
- `provider`：站点类型，选填，可选 `default`、`anyrouter`，默认 `default`
- `accounts`：账号数组，必填
- `login_endpoint`：账号密码登录接口路径，选填，默认 `/api/user/login`；显式传空字符串 `""` 表示关闭账号密码登录
- `checkin_endpoint`：签到接口路径，选填，默认取对应 provider 的接口
- `status_endpoint`：签到状态接口路径，选填，默认取对应 provider 的接口；显式传空字符串 `""` 表示关闭状态查询
- `user_info_endpoint`：余额接口路径，选填，默认 `/api/user/self`

### 账号字段

- `name`：账号名称，选填，默认自动编号
- `token`：系统访问令牌（Access Token），在站点「个人设置 - 安全设置 - 系统访问令牌」生成
- `username`：站点登录用户名，邮箱注册的站点直接填邮箱
- `password`：站点登录密码，和 `username` 同时配置才生效
- `cookie`：站点登录 Cookie，支持 `session=xxx` 字符串或 JSON 对象，仅 `anyrouter` 使用
- `api_user`：用户 ID，选填；老版本 NewAPI 的令牌鉴权需要这个字段，遇到 401 时补上

认证方式按站点类型固定：

- `default`：配置了 `token` 就用令牌，否则用 `username` + `password`
- `anyrouter`：只认 `cookie`

同时配置多个字段时，只使用命中的那一种。

## 配置示例

### 通用 NewAPI：访问令牌

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

### 通用 NewAPI：账号密码

站点没提供访问令牌，或者更习惯用账号密码时使用。AgentRouter 也走这一套：

```json
[
  {
    "name": "AgentRouter",
    "base_url": "https://agentrouter.org",
    "accounts": [
      {
        "name": "主号",
        "username": "you@example.com",
        "password": "你的登录密码"
      }
    ]
  }
]
```

AgentRouter 的签到奖励在登录时发放，配置账号密码即可，不需要额外的签到设置。

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

## 可选环境变量

- `NOTIFY_TRIGGERS`：通知条件，默认 `always`；可选 `always, never, success, failed, balance_changed`
- `NEWAPI_DATA_DIR`：余额记录目录，默认脚本同级目录
- `TZ`：时区，默认 `Asia/Shanghai`
