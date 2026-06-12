# Linux.Do 签到

青龙脚本：登录 Linux.Do，随机浏览主题、随机点赞，并读取 `connect.linux.do` 活跃数据后推送通知。

## 依赖

```bash
pip install DrissionPage curl_cffi loguru beautifulsoup4 tabulate
```

`beautifulsoup4` 用于解析 Connect 页面，`tabulate` 只影响日志表格展示。

## 青龙任务

```text
0 */6 * * * python3 /ql/data/scripts/linuxdo/linuxdo.py
```

实际路径按你的青龙脚本目录调整。

## 登录配置

优先级：

1. `LINUXDO_COOKIES`
2. 本地保存的 Cookie 文件
3. `LINUXDO_USERNAME` + `LINUXDO_PASSWORD`

### Cookie 登录

```text
LINUXDO_COOKIES=session=xxx; _t=yyy
```

### 账号密码登录

```text
LINUXDO_USERNAME=你的用户名或邮箱
LINUXDO_PASSWORD=你的密码
```

账号密码登录成功后，脚本会自动保存 Cookie，下次优先复用。

## 可选环境变量

- `BROWSE_ENABLED`：是否执行浏览/点赞，默认 `true`
- `LINUXDO_COOKIE_FILE`：Cookie 保存路径，默认 `linuxdo_cookie.txt`
- `LINUXDO_SAVE_COOKIE`：是否保存 Cookie，默认 `true`

## 通知

使用青龙默认 `notify.py`。通知内容包含登录状态、浏览状态和 Connect 活跃数据。
