# NodeSeek 签到

青龙脚本：支持多账号 NodeSeek 签到，并查询鸡腿余额。

## 依赖

```bash
pip install requests
```

## 青龙任务

```text
30 9 * * * python3 /ql/data/scripts/nodeseek/nodeseek.py
```

实际路径按你的青龙脚本目录调整。

## 环境变量

### 必填

```text
NS_COOKIE=你的 NodeSeek Cookie
```

多账号支持换行或 `&` 分隔：

```text
cookie1
cookie2
```

或：

```text
cookie1&cookie2
```

### 可选

- `NS_RANDOM`：是否随机鸡腿，默认 `true`；设为 `false` 时固定 5 个

## 通知

使用青龙默认 `notify.py`，每次运行结束都会通知。
