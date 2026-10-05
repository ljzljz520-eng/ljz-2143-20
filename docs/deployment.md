# 跨机部署说明 —— C 端窗口交付验证器（WDeliver）

> 本文档的每一条“能力”结论都对应 `wdeliver.client.probe` 的真实探测输出，
> **没有任何一项默认齐全**。字体、图形后端、显示缩放、写入权限都由目标机实测给出。

## 1. 系统组成

```text
Web 控制台 (wdeliver/server/console.html)
   │  登记设备能力 / 配置发行通道与升级批次 / 查看交付结论
   ▼
服务端   (wdeliver/server/app.py + db.py, 标准库 http.server + sqlite3)
   │  /api/manifest/device/<id>  按协议版本协商的安装清单（兼容窗口）
   │  /packages/<build>.wdz      资源包，支持 Range 续传
   ▼
数据库   SQLite：builds(构建指纹) / devices(探测结果+阶段+首帧receipt)
   │           / channels(通道、批次、放量、能力门禁) / device_events
   ▼
客户端   (wdeliver/client: probe → preflight → download → install
   │       → 暂存首帧 → 原子激活 → 激活后首帧 → 失败回退)
   ▼
载荷     deploy/payload：现有 C/SDL2 窗口程序 + hooks/firstframe.py
         + 随包位图字体 + PPM 背景（nullfb 无显示首帧后端）
构建器   wdeliver/builder：确定性打包、绝对路径红线、双指纹、依赖对比
```

## 2. 快速开始（可重复）

```bash
# (1) 可重复构建两个版本（产物字节级可复现，见 docs/build.md）
python3 -m wdeliver.builder.build --version 1.0.0
python3 -m wdeliver.builder.build --version 1.1.0

# (2) 启动服务端（生产去掉 --test；--test 仅开放断网注入接口）
WDELIVER_TEST=1 python3 -m wdeliver.server.app --port 8448

# (3) 发布构建（上传包 + 登记指纹）
python3 -m wdeliver.builder.publish \
  --build-record packages/visual-window-app_1.1.0.build.json

# (4) 控制台配置通道/批次/门禁
#     浏览器打开 http://127.0.0.1:8448/
#     ① 登记设备  ② 选择 build、通道名、批次、放量比例、字体/图形门禁

# (5) 客户端在目标机执行（所有目录显式指定，HOME 不被业务文件污染）
python3 -m wdeliver.client.updater \
  --server http://<server>:8448 --device dev-lab-01 \
  --install-dir /opt/visual-window-app \
  --data-dir /var/lib/wdeliver --cache-dir /var/cache/wdeliver \
  --state-dir /var/lib/wdeliver/state update
```

一键跑全部验收：`python3 tests/acceptance.py`（29 项，覆盖第 6 节全部场景）。

## 3. 探测输出 ↔ 文档结论对照表

控制台“实测能力摘要”与 `state/probe.json` 的字段完全一致：

| 文档结论 | 探测字段 | 取值（全部来自实测） |
|---|---|---|
| 干净用户目录 | `clean_home.status` | `ok`=HOME 可读且无残留；`missing`=HOME 不存在；`warn`=发现上次交付状态 |
| 安装路径可写 | `install_writable.status` | `ok`=mkstemp+写入+读回+删除成功；`missing`=PermissionError |
| 本地写入权限 | `local_writes.status` | data/cache 两个目录分别真实 round-trip 后取最差值 |
| 默认字体齐全 | `fonts.status` / `fonts.found.sans` | fontconfig 实测 sans/mono/cjk；缺 sans=`missing`，缺 cjk=`warn` |
| 图形后端可用 | `gfx_backend.backend` | 实测顺序 `x11`(xdpyinfo 真连) → `wayland`(socket) → `framebuffer`(/dev/fb0) → `nullfb`(随包) |
| 显示缩放 | `display_scaling.scale` | X11 时读 `xrdb -query` 的 Xft.dpi 计算 scale；**无显示时为 `null`/`unknown`，不默认 1.0** |
| 升级源连通 | `network.status` | 对 `/health` 发起真实 HEAD 请求 |

本文档不写“目标机默认有字体/有显示/DPI=96”。例如当前构建机实测：

```text
fonts:        warn   sans=DejaVu Sans, cjk 缺失
gfx_backend:  warn   nullfb（无 X/Wayland/fb 设备）
display:      unknown scale=null（无法测量，不假设）
libSDL2:      缺失   ldconfig -p 中不存在
```

在 Docker 目标（见仓库 `docker/`）上则由 Xvfb + libSDL2 实测为 `x11/sdl`。
同一份文档，结论随机器变化——这就是“探测给出结论”的含义。

### 通道策略如何使用探测结论

控制台保存通道时可设置门禁，写入清单 `requirements`（协议 1.2）：

- `gfx_backend: "x11"` —— 必须探测到真实 X11，否则预检阻断；
- `gfx_backend: "sdl"` —— x11/wayland/framebuffer 任一即可；
- 不限制 —— 允许降级到随包 `nullfb`，首帧 receipt 会如实标注
  `backend=nullfb` 与 `font_source=bundled:...`；
- `font_sans: true` —— 必须实测到系统 sans 字体；
  缺字体机器若通道允许 nullfb，仍可交付，首帧用**随包位图字体**绘制。

## 4. 依赖选择：随包携带 vs 系统组件

完整实测表由构建器生成：`python3 -m wdeliver.builder.deps`
→ `docs/deps-comparison.md`（数据随构建更新，含每一项包内字节数）。

选择依据摘要：

1. **随包**（压缩后约 145KB / 总包约 150KB）：nullfb 帧缓冲、3×5 位图字体、
   PPM 背景。保证“无显示、无字体、无 libSDL2_image”的机器仍能完成首帧验证。
2. **用系统组件**（零包体、强 ABI 耦合）：libSDL2、X server/Xvfb、
   系统字体。真实窗口渲染依赖它们；安装前由探测实测，缺失即按通道策略阻断或降级。
3. **背景 PPM 而非 PNG**：构建期把 1.7MB 的 PNG 转成 ~170KB PPM
   （`asset_conversion` 记录前后体积），免除对 `libSDL2_image` 的硬依赖；
   代价是无透明通道。C 程序两条加载路径都保留（PPM 原生 / PNG 走 SDL2_image）。
4. **不随包 libSDL2**：MB 级体积且与 glibc/发行版 ABI 强绑定，跨机兼容性差。
   采用“声明系统组件 + 实测门禁 + 回退后端”而不是打包 .so。

## 5. 交付状态机与“完成”的定义

```text
REGISTER → PROBED → PREFLIGHT → DOWNLOADING → VERIFIED → INSTALLING
        → FIRSTFRAME(staged, 在 versions/<v>/ 内验证，current 不动)
        → 原子切换 current 符号链接
        → FIRSTFRAME(active, 激活后复验)
            ├─ 通过 → ACTIVE（携带首帧 receipt）
            └─ 失败 → ROLLEDBACK（切回旧版本并对旧版复验首帧）
```

**网页只有在 `phase=ACTIVE` 且 receipt 含 `frame_sha256` 时才显示“升级完成”。**
DOWNLOADING 即使字节数已满、VERIFIED 即使哈希已通过、INSTALLING 即使解包结束，
都只显示“尚未完成”。这是验收项，不是文案选择（见 `tests/acceptance.py` T6）。

## 6. 验收场景 ↔ 实现位置

| 验收场景 | 测试 | 行为 |
|---|---|---|
| 干净用户目录 | T1 | 探测/缓存隔离到 state 目录与 XDG_*；ACTIVE + nullfb 首帧 receipt |
| 缺少默认字体（`FONTCONFIG_FILE` 指向空目录配置） | T2 | 要求 sans 的通道预检阻断、零落盘；允许 nullfb 时交付并标注随包字体 |
| 无法写安装路径（安装目录 0500） | T3 | `install_writable=missing`，预检失败，不进入下载 |
| 网络断在升级中途（`?cut_after=40960`） | T4 | 自动 `Range` 续传（resumes≥1），最终 sha256 一致 |
| 旧客户端接到新字段 | T5 | 1.0 客户端协商到 1.0 视图；未知字段进 `ignored_keys` 不影响安装；无交集 → 409 |
| 下载完成≠升级完成 | T6 | 控制台完成判定绑定 ACTIVE+receipt.frame_sha256 |
| 首帧失败回退（先装 1.0.0 再升 1.1.0） | T7 | `WD_FORCE_FIRSTFRAME_FAIL=active` → ROLLEDBACK，current 指回 1.0.0 并复验 |
| 可重复构建 | T8 | 固定 SOURCE_DATE_EPOCH/uid/gzip mtime，两次字节一致 |
| 开发者绝对路径红线 | T9 | 构建扫描命中 `/home/...` 直接拒绝（退出码 2） |

## 7. 安全与完整性

- 包：tar.gz 内路径穿越检查（拒绝 `..`/绝对路径成员）；
- 包 sha256（归档层）+ 每个文件 sha256（解包后复核）双校验，任一不过不激活；
- 激活是单次 `rename(2)` 的符号链接切换，崩溃也只会停留在旧版；
- 故障注入接口只在 `--test`/`WDELIVER_TEST=1` 时存在。

## 8. 真实 SDL 窗口路径（Docker 目标机）

载荷 `deploy/payload/src/main.c` 支持一次性首帧模式：
`WD_ONESHOT=1 WD_FRAME_FILE=f.ppm WD_RECEIPT_FILE=r.json` 时，程序创建真实窗口、
渲染一帧、`SDL_RenderReadPixels` 读回写 PPM 后退出。客户端探测到 x11/wayland/fb
且通道未限制降级时，首帧钩子走 `--backend sdl`，由真实窗口像素生成 receipt。
开发/CI 无显示环境走 `nullfb`，receipt 明确区分二者，不互相冒充。
