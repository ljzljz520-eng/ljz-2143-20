# 客户端升级与服务器配置的兼容窗口

升级期间“新服务器 + 旧客户端”和“旧服务器 + 新客户端”必须能共存，
不能要求整批设备在同一时刻升级。兼容策略由 `wdeliver/manifest.py` 实现，
验收见 `tests/acceptance.py` T5。

## 1. 清单协议版本

| 版本 | 字段（相对上版新增） | 协商来源 |
|---|---|---|
| 1.0 | `build_id, version, package_url, package_size, package_sha256, files` | 1.0 客户端 |
| 1.1 | `release_channel, rollout_batch, release_notes` | 1.1 客户端 |
| 1.2 | `requirements, fallback_on_fail, min_client_version` | 1.2 客户端（当前） |

## 2. 协商规则

客户端请求清单时带头：

```text
GET /api/manifest/device/<id>?channel=stable
X-Manifest-Accept: 1.0,1.1,1.2      # 该客户端能理解的版本
X-Client-Version: 1.0.0             # 语义版本，用于 min_client_version 硬门禁
```

- 服务器从交集中选**双方都支持的最高版本**，返回时带
  `X-Manifest-Served-Version`；1.0 客户端永远收不到 1.1/1.2 字段。
- 无交集 → `409 CLIENT_TOO_OLD`（如接受头是 `0.1`），客户端停止并记录，
  不进行猜测式安装。
- 构建声明了更高 `min_client_version` 时，即使协议视图能降级，
  旧客户端也会在 `parse()` 处收到 CLIENT_TOO_OLD（纵深防御：服务端已先拦）。

## 3. 前向兼容（旧客户端接到未来字段）

解析器契约：

- **未知顶层键、未知 files 键一律忽略**，登记到 `ignored_keys`，
  安装语义不变（继续按本版本字段走下载/校验/安装/首帧）；
- 结构硬错误才拒绝：缺包地址、sha256 非 64 hex、files 为空/非列表等。

测试模式下服务器可注入未来字段做实测：

```bash
# 服务端以 WDELIVER_TEST=1 启动
python3 -m wdeliver.client.updater ... --emulate-client 1.0.0 --inject-future
# state/manifest-*.json 中可见 future_* 字段，升级仍以 1.0 语义成功 ACTIVE
```

## 4. 后向兼容（新客户端接旧服务器）

- 新客户端 `parse()` 对没有 `manifest_version` 的清单按 1.0 处理；
- 对未来协议版本号（如 "1.9"）按已知最高版宽容解析，未知键忽略；
- 缺失的 1.1/1.2 字段用默认值：`fallback_on_fail=True`、无 requirements 门禁。

## 5. 灰度批次的确定性

是否在本批升级由 `sha256(device_id|batch) % 100 < rollout_pct` 决定：
同一设备对同一批次结论稳定，调大批量会稳定纳入新设备，缩小批次不会让已升级设备
“被降级”（设备端不会因清单停止而回滚，回滚只由首帧失败或人工触发）。

## 6. 升级客户端自身的建议窗口

1. 先部署支持新协议的**服务器**（始终能给旧视图）；
2. 按批次升级客户端（控制台可按设备 client_version 筛选放量）；
3. 全部客户端越过新版本后，再在通道里启用新门禁字段；
4. 需要强制时才提高 `min_client_version`，旧客户端会收到明确的 409，
   而不是拿到无法解释的配置。
