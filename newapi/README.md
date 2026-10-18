# NewAPI 多站点签到

呆呆面板脚本：支持多个 NewAPI 站点、每站点多个账号，自动签到并检查余额变化。

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

在呆呆面板「环境变量」中配置 `NEWAPI_SITES`：

```json
[
  {
    "name": "站点A",
    "base_url": "https://api.example.com",
    "accounts": [
      {
        "name": "主号（系统令牌）",
        "token": "sk-xxxx",
        "api_user": "1001"
      },
      {
        "name": "副号（Cookie）",
        "cookie": "session=xxxx"
      }
    ]
  }
]
```

### 字段说明

#### 站点配置
- `name`：站点名称，选填，默认从域名自动提取
- `base_url`：站点地址，必填
- `accounts`：账号列表，必填
- `checkin_endpoint`：签到接口路径，选填，默认 `/api/user/checkin`（遇 404 会自动尝试 `/api/user/sign_in`）
- `user_info_endpoint`：余额接口路径，选填，默认 `/api/user/self`

#### 账号配置
- `name`：账号名称，选填，默认自动编号
- `token`：系统访问令牌（网站「个人设置 - 安全设置 - 系统访问令牌」生成），推荐
- `cookie`：登录 Cookie，支持 `session=xxx` 字符串或 JSON 对象；与 `token` 二选一
- `api_user`：用户 ID，选填，存在时会携带 `New-Api-User` 请求头

## 可选环境变量

- `NOTIFY_TRIGGERS`：通知条件，默认 `always`；可选 `always, never, success, failed, balance_changed`
- `NEWAPI_DATA_DIR`：余额记录目录，默认脚本同级目录
- `TZ`：时区，默认 `Asia/Shanghai`
