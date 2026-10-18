# NodeSeek 签到

呆呆面板脚本：支持多账号 NodeSeek 签到，并查询鸡腿余额。

## 依赖

```bash
pip install requests
```

## 定时任务

```text
30 9 * * * task nodeseek/nodeseek.py
```

或直接在面板「定时任务」中添加任务，命令填写 `task nodeseek/nodeseek.py` 或 `python3 nodeseek/nodeseek.py`。

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

使用呆呆面板自带 `notify.py`，每次运行结束都会通知。
