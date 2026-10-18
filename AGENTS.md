# 呆呆面板脚本开发与维护规范

本项目为呆呆面板（Daidai Panel）定时任务脚本集合。项目面向呆呆面板原生 Docker 环境，不包含青龙面板历史兼容逻辑。

## 参考资源

- 仓库地址：https://github.com/linzixuanzz/daidai-panel
- API 文档：https://dumb-panel.apifox.cn/
- 脚本内调用面板能力官方文档：https://github.com/linzixuanzz/daidai-panel/blob/main/docs/script-api.md

## 运行环境与目录约定

- 部署方式：标准 Docker / Docker Compose 部署。
- 数据卷挂载：宿主机数据目录通常挂载到容器内 `/app/Dumb-Panel`。
- 持久化路径：
  - 脚本根目录：由面板统一托管，路径保存在环境变量 `DAIDAI_SCRIPTS_DIR`。
  - 会话与长期存储文件：统一保存在持久化挂载卷内（例如 `/app/Dumb-Panel/data/<业务目录>/` 或脚本子目录内经过持久化挂载的路径），禁止写入容器易失层。

## 环境变量规范

面板在执行定时任务时会自动向进程注入以下环境变量，脚本中通过标准 `os.environ` 读取：

| 变量名                     | 说明                                                            |
| -------------------------- | --------------------------------------------------------------- |
| `DAIDAI_API_BASE`          | 面板内部 API 根地址，例如 `http://127.0.0.1:5701/api/v1`        |
| `DAIDAI_TOKEN`             | 任务执行期间有效的 Bearer 鉴权令牌                              |
| `DAIDAI_NOTIFY_URL`        | 通知发送接口地址，等价于 `$DAIDAI_API_BASE/notifications/send`  |
| `DAIDAI_NOTIFY_TOKEN`      | 通知接口临时令牌，与 `DAIDAI_TOKEN` 同值                        |
| `DAIDAI_NOTIFY_TIMEOUT`    | 通知超时时间，默认 `15000`（毫秒）                              |
| `DAIDAI_NOTIFY_CHANNEL_ID` | 当前任务在通知设置中绑定的渠道 ID，未单独绑定时不存在           |
| `DAIDAI_NOTIFY_PY`         | 内置通知 helper `notify.py` 的绝对文件路径                      |
| `DAIDAI_SCRIPTS_DIR`       | 脚本根目录绝对路径                                              |
| `NO_PROXY` / `no_proxy`    | 回环地址白名单（含 `127.0.0.1` 等），防止回调面板自身时误入代理 |


## 通知调用规范

### 1. 优先复用面板内置 helper

呆呆面板自动将脚本根目录添加至 `PYTHONPATH`，脚本首选直接引用：

```python
from notify import send

send("任务标题", "正文内容")
```

### 2. 路径回落逻辑

当直接导入失败时，仅回落至呆呆面板环境变量指向的路径及当前脚本所在目录，禁止引入青龙面板的历史路径（如 `/ql/...`）：

```python
candidates = [
    os.environ.get("DAIDAI_NOTIFY_PY", "") and os.path.dirname(os.environ["DAIDAI_NOTIFY_PY"]),
    os.environ.get("DAIDAI_SCRIPTS_DIR", ""),
    os.path.dirname(os.path.abspath(__file__)),
]
```

### 3. 异常处理与退出码

与青龙静默忽略未配渠道不同，呆呆面板在无匹配推送渠道时会返回 HTTP 400，导致 `notify.py` 抛出 `RuntimeError`。
脚本发送通知时须做好异常捕获，避免因非致命通知异常导致核心签到逻辑退出码异常（非零状态）。

## 依赖与定时任务管理

1. **依赖安装**：在呆呆面板后台「依赖管理 → Python」中按需添加 Python 包，避免在脚本中执行动态安装。
2. **任务命令**：定时任务执行命令统一使用 `task <子目录>/<脚本名>.py`（例如 `task newapi/newapi.py`）或 `python3 <子目录>/<脚本名>.py`。
3. **日志输出**：日志提示中统称为“呆呆面板”或“面板通知”，不使用已弃用的平台术语。
