# VMISS 补货监控

青龙脚本：轮询 VMISS 商店分类页的商品库存，型号补货时通过青龙自带 `notify.py` 的 `send(title, content)` 推送通知。

分类页公开可访问，脚本只读取库存，不执行下单操作。

## 依赖

运行环境：Python 3.10 及以上。

在青龙「依赖管理 → Python」中安装：

```text
requests
```

## 青龙任务

```text
*/5 * * * * python3 /ql/data/scripts/qlscripts/vmiss/vmiss_stock.py
```

补货窗口通常很短，建议 5 分钟或更短间隔。实际路径按你的青龙脚本目录调整。

## 环境变量

### 可选

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VMISS_TARGETS` | `US.LA.TRI.Basic` | 监控目标，JSON 数组 |
| `VMISS_NOTIFY` | `restock` | `restock` 仅补货时通知；`always` 每次运行都通知当前库存 |
| `VMISS_STATE_FILE` | 脚本同级 `.data/vmiss_stock.json` | 库存状态文件路径 |
| `VMISS_TIMEOUT` | `20` | 单次请求超时（秒） |
| `VMISS_PROXY` | 空 | `http://主机:端口`，默认直连 |

`VMISS_TARGETS` 每项包含：

- `store`：必填，分类页路径段，即 `https://app.vmiss.com/store/<store>` 中的 `<store>`
- `plans`：选填，型号名数组（页面上的 `package-title`，如 `US.LA.TRI.Basic`）；留空则监控该分类下全部商品

监控洛杉矶 TRI 的 Basic 和 Core：

```json
[{ "store": "us-los-angeles-tri", "plans": ["US.LA.TRI.Basic", "US.LA.TRI.Core"] }]
```

同时监控多个分类：

```json
[
  { "store": "us-los-angeles-tri", "plans": ["US.LA.TRI.Basic"] },
  { "store": "jp-tokyo-tri", "plans": [] }
]
```

型号名区分大小写以外的差异会自动忽略，但拼写必须与页面一致；写错时日志会提示「未找到型号」。

## 通知

使用青龙默认 `notify.py`，渠道沿用面板配置。

- `restock` 模式只在「缺货 → 有货」时通知，同一次补货只推一次；持续有货不会重复推送。
- 分类页拉取失败（网络异常、Cloudflare 拦截）会推送一次异常通知，并保留上次库存；恢复时推送一次恢复通知。
- 找不到 `notify.py` 或调用失败时，通知内容仍会打印到日志。

通知内容包含型号名、库存数量、价格和商品页链接。

## 说明

VMISS 的 Cloudflare 对 `/store/us-los-angeles-tri/*` 下的商品页路径启用了访问拦截，直接请求会返回 403，因此脚本只监控分类页、不尝试自动下单。收到补货通知后需手动下单。

脚本自身日志为普通文本，状态文件默认写在脚本同级 `.data/` 目录。青龙如需持久化，可将 `VMISS_STATE_FILE` 指向 `/ql/data` 下的路径。

## 常见问题

### 拉取失败：Cloudflare 硬拦截

```text
[VMISS] 拉取 /store/us-los-angeles-tri 失败：Cloudflare 硬拦截：当前出口 IP 被拉黑 (cf-ray ...)，需更换出口 IP 或走代理
```

青龙所在 VPS 的出口 IP 被 Cloudflare 判为风险 IP，返回 `Sorry, you have been blocked`。这是 IP 层面的拦截，改 UA、加 Cookie 都无效。可用的处理方式：

- 给脚本配置 `VMISS_PROXY`，走一个未被拦截的出口
- 更换 VPS 或该 VPS 的出口 IP

被拦截时脚本会推送一次异常通知，并保留上次库存，不会误判成缺货。

### 拉取失败：Cloudflare 人机验证

```text
[VMISS] 拉取 /store/us-los-angeles-tri 失败：Cloudflare 人机验证：需真实浏览器才能通过
```

返回的是挑战页（`Just a moment...`），纯 HTTP 请求无法通过。同样通过 `VMISS_PROXY` 换出口，或改用浏览器方案。
