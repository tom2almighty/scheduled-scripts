# NewAPI 多站点签到

青龙脚本：支持多个 NewAPI 站点、每站点多个账号，自动签到并检查余额变化。

## 依赖

```bash
pip install curl_cffi loguru DrissionPage
```

确保青龙环境里有可用的 Chromium/Chrome。

## 青龙任务

```text
13 8 * * * python3 /ql/data/scripts/newapi/newapi.py
```

实际路径按你的青龙脚本目录调整。

## 环境变量

只需要配置 `NEWAPI_SITES`：

```json
[
  {
    "name": "AnyRouter",
    "base_url": "https://anyrouter.top",
    "checkin_endpoint": "/api/user/sign_in",
    "user_info_endpoint": "/api/user/self",
    "waf": true,
    "accounts": [
      {
        "name": "主号",
        "cookie": "session=xxx",
        "api_user": "12345"
      }
    ]
  },
  {
    "name": "其他站点",
    "base_url": "https://example.com",
    "checkin_endpoint": "/api/user/checkin",
    "waf": "auto",
    "accounts": [
      {
        "name": "账号A",
        "cookie": "session=yyy"
      }
    ]
  }
]
```

字段说明：

- `base_url`：站点地址，必填
- `checkin_endpoint`：签到接口，默认 `/api/user/checkin`
- `user_info_endpoint`：余额接口，默认 `/api/user/self`
- `waf`：`true` / `false` / `"auto"`，默认 `"auto"`
- `cookie`：账号 Cookie，支持字符串或对象
- `api_user`：可选，存在时写入请求头 `new-api-user`

## 可选环境变量

- `NOTIFY_TRIGGERS`：通知条件，默认 `always`；可选 `always, never, success, failed, balance_changed`
- `NEWAPI_BROWSER_PATH`：指定 Chromium/Chrome 路径
- `NEWAPI_DATA_DIR`：余额记录目录，默认脚本同级目录
- `TZ`：时区，默认 `Asia/Shanghai`
