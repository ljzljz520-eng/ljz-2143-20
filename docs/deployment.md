# C 端窗口交付验证器：跨机部署说明

本文只写由流水线和设备实探能够证明的结论。字体、X11/Wayland 图形后端、显示缩放、安装目录写入权限均**不得**根据开发者机器、镜像内容或文档默认“齐全”；每台设备必须上传自己的探测结果。

## 1. 组件和数据流

```text
Web 控制台 (deploy/console/index.html)
  ├─ 登记设备声明能力：devices.capabilities
  ├─ 配置发行通道：channels
  └─ 创建升级批次：batches + batch_devices
                 │
控制面服务 (deploy/server.py，Python 标准库 + SQLite)
  ├─ 保存安装清单 manifest、构建指纹、artifact sha256、payload 文件 hash
  ├─ 依据每台设备最新 probe 选择 system/bundled artifact
  └─ 支持 HTTP Range；下载中断后从已有字节续传
                 │
C 端升级代理 (client/agent.py)
  ├─ 干净 HOME 下探测显示、字体、缩放、库、磁盘和写权限
  ├─ 预检 → 断点下载 → artifact hash → 安全解包 → payload hash/权限
  ├─ 安装到 releases/<version>-<releaseId>
  ├─ 启动 payload 并等待首帧标记
  └─ 首帧失败则保留旧 current，并验证旧版本可回退
                 │
SQLite (var/vdv.db)
  ├─ releases.fingerprint：源码/worktree digest、编译器、SOURCE_DATE_EPOCH、绝对路径泄漏
  └─ reports：每台设备的 probe、下载、安装、首帧、回退事件
```

完成条件只有一个：服务端收到 `phase=first_frame_verified,status=success`。`download_verified` 只说明 tar 字节和摘要正确，网页将其显示为“已下载，尚未完成”。

## 2. 自动探测输出字段与文档结论

代理输出 JSON（可用 `client/agent.py probe` 单独运行）。关键结论字段如下：

| 能力 | 字段 | 结论来源 | 不允许的默认值 |
|---|---|---|---|
| 显示器 | `display.available` | `DISPLAY` + `/tmp/.X11-unix/X<n>` + `xdpyinfo`；Wayland 检查 `XDG_RUNTIME_DIR/wayland-*` | 容器装了 Xvfb 就认为可用 |
| 图形后端 | `display.backends.x11.available` / `wayland.available` | socket、命令返回值和输出 | 文档写“支持 X11/Wayland”即通过 |
| 缩放 | `display.scale.{scale,dpi,method}` | `xrandr` 物理尺寸计算；否则 GDK/QT 环境变量；都没有则 `unavailable` | 默认 1.0 或 96 DPI |
| 字体 | `fonts.availableFamilies`、`missing`、`bundled` | `fc-match` 验证系统族；`fc-scan --format %{family[0]}` 验证随包字体 | 只看到字体文件存在即认为可用 |
| 系统库 | `libraries.available/missing` | `/sbin/ldconfig -p` 或 `ldconfig -p` | 开发镜像上有库就认为目标机有 |
| 写权限 | `paths.*.writable`、`freeBytes` | 在 HOME/cache/state/installRoot 中创建临时文件；`statfs` 取容量 | 以 root 身份或 sudo 结果代替 C 端用户 |

本仓库当前无图形环境的实际探测结论（完整机器可读样例见 [probe-sample.json](probe-sample.json)，其时间和容量会随环境变化）是：

```json
{
  "display": {"available": false, "reason": "DISPLAY is not set"},
  "backends": {"x11": false, "wayland": false},
  "scale": {"method": "unavailable", "scale": null, "dpi": null}
}
```

因此 CI 中的 `fixture` 通道声明 `display=false`，只验证交付/回退机制，不把它冒充真实窗口。真实 SDL 构建必须在有 X11 或 Wayland 的设备上取得 `display.available=true`，并由 SDL 进程首帧 marker 返回驱动、宽高。

## 3. 可重复构建

构建器：

```bash
python3 tools/build_releases.py \
  --mode fixture \
  --version 1.0.0 \
  --channel fixture \
  --policies system,bundled \
  --source-date-epoch 1700000000 \
  --out dist/release
```

真实 SDL 应用（需要 `libsdl2-dev`、`libsdl2-image-dev`、`patchelf`）：

```bash
python3 tools/build_releases.py \
  --mode sdl \
  --version 1.0.0 \
  --channel stable \
  --policies system,bundled \
  --requires-display \
  --font-family 'Noto Sans CJK SC' \
  --font-file /path/to/NotoSansCJK-Regular.ttc \
  --out dist/release
```

可重复性措施：

1. 固定 `SOURCE_DATE_EPOCH`（默认 `1700000000`）。
2. tar 使用 USTAR、固定 mtime、uid/gid=0、空用户名、排序遍历。
3. C 编译使用 `-ffile-prefix-map=$PWD=.` 和 `-fdebug-prefix-map=$PWD=.`。
4. manifest 记录 git commit、tracked/worktree digest、编译器、文件 sha256、模式和 payload 大小。
5. 构建器扫描 ELF 中的 `/workspace/...` 等构建目录；发现后写入 `build.absolutePathLeak`，验收要求为空数组。
6. 临时构建目录在 `libraries.path` 中重写为 `<payload>/...` 或 `<build-only>/...`，不进入稳定指纹。

重复验证：

```bash
python3 tools/build_releases.py --mode fixture --version 1.0.0 \
  --channel fixture --policies bundled --out /tmp/a
python3 tools/build_releases.py --mode fixture --version 1.0.0 \
  --channel fixture --policies bundled --out /tmp/b
cmp /tmp/a/visual-window_1.0.0_fixture_linux_bundled.tar \
    /tmp/b/visual-window_1.0.0_fixture_linux_bundled.tar
```

## 4. 随包携带依赖与系统组件的选择

服务端不是只按体积选包。它先过滤，再在兼容候选里选最小：

1. 架构匹配。
2. 显示器和图形后端实探通过。
3. 必需字体由系统字体或已通过 `fc-scan` 的随包字体满足。
4. system artifact 要求的库存在；bundled artifact 仍要求内核、显示服务、fontconfig/freetype 等不能合理静态化的系统底座。
5. 安装目录剩余容量大于 `requirements.minDiskBytes`。
6. 候选都通过后，在最新版本内选 `size ASC`。

| 策略 | 体积 | 兼容性 | 适用场景 |
|---|---:|---|---|
| system | 小；验收夹具 system=81,920 bytes，bundled=153,600 bytes | 依赖目标机已安装匹配 soname；旧发行版、精简桌面、嵌入式镜像可能失败 | 受控企业镜像、统一 OS/仓库版本、安全补丁由系统统一管理 |
| bundled | 大；夹具约为 system 的 1.88 倍 | 应用媒体库随包走，目标机缺库时仍可选中 | 多发行版、现场设备差异大、无法要求预装 SDL/图像库 |
| 字体 bundled | 增加 TTF/TTC 体积 | 文件存在后仍要 `fc-scan`，并在首帧/渲染中验证 | 无 CJK 字体或字体版本不一致的现场设备 |
| X11/Wayland、GL/EGL、fontconfig 服务 | 不随包复制 | 必须由系统/会话提供，靠 socket、驱动和首帧验证 | 所有真实窗口 |

验收夹具中的 `libvdv-greeter.so.1` 用来演示同一应用的两种选择：system 包链接系统 soname，bundled 包放入 `payload/lib/` 并用 `$ORIGIN/../lib` 查找。

## 5. 启动服务和控制台

```bash
python3 deploy/server.py \
  --host 127.0.0.1 --port 18080 \
  --db var/vdv.db \
  --artifact-dir var/artifacts
# 浏览器打开 http://127.0.0.1:18080
```

也可直接调用 API：

```bash
curl -sS -X POST http://127.0.0.1:18080/api/admin/devices \
  -H 'content-type: application/json' \
  -d '{"id":"bench-01","name":"bench","arch":"aarch64","capabilities":{"site":"qa"}}'

curl -sS -X POST http://127.0.0.1:18080/api/admin/channels \
  -H 'content-type: application/json' \
  -d '{"appId":"visual-window","name":"fixture","active":true,"minClientVersion":1}'

curl -sS -X POST http://127.0.0.1:18080/api/admin/releases/publish \
  -H 'content-type: application/json' \
  -d '{"path":"dist/release/visual-window_1.0.0_fixture_linux_bundled.tar"}'

curl -sS -X POST http://127.0.0.1:18080/api/admin/batches \
  -H 'content-type: application/json' \
  -d '{"name":"batch-a","appId":"visual-window","channel":"fixture","deviceIds":["bench-01"]}'
```

控制台声明能力只用于资产/批次管理和审计；服务端选包时的实际结论以最新 probe 为准。

## 6. 预检、安装、首帧和回退

单设备升级：

```bash
python3 client/agent.py upgrade \
  --server http://127.0.0.1:18080 \
  --device-id bench-01 \
  --home /var/lib/visual-window/clean-home \
  --install-root /opt/visual-window \
  --protocol 2
```

阶段顺序：

1. `probe`：上传实际能力。
2. `preflight_passed/preflight_failed`：失败时不下载。
3. `downloading/download_interrupted`：Range 续传。
4. `download_verified`：校验 artifact sha256；不是升级完成。
5. `installing/installed`：安全解包并逐文件校验 sha256/mode。
6. `first_frame_verified/first_frame_failed`：payload 写出首帧 JSON marker。
7. 首帧成功后原子替换 `current`；失败则执行旧版本回退首帧验证。

目录布局：

```text
<install-root>/
├── current -> releases/<version>-<releaseId>/payload
├── staging/
└── releases/
    └── <version>-<releaseId>/
        ├── manifest.json
        └── payload/
```

真实 SDL 程序读取：

* `VDV_FIRST_FRAME_FILE`：第一次 `SDL_RenderPresent` 后写 JSON；
* `VDV_SMOKE_FRAMES=1`：首帧验证模式，在首帧后退出；
* marker 包含 renderer driver、窗口宽高和 `ok=true`。

手动回退：

```bash
python3 client/agent.py rollback \
  --server http://127.0.0.1:18080 \
  --device-id bench-01 \
  --home /var/lib/visual-window/clean-home \
  --install-root /opt/visual-window
```

回退不是只改链接：代理会读取旧版本 manifest 并再次运行首帧 marker，成功才报 `rollback_complete`。

## 7. 协议兼容窗口

* 当前服务端支持 `protocol=1,2`。
* v1 返回固定顶层字段：`action/releaseId/version/channel/policy/downloadUrl/sha256/artifactSha256/size/manifest`。
* v2 新信息放在 `extensions`，例如 requirements、firstFrame、selection 和 buildFingerprint。
* v1 客户端不会收到顶层 `extensions`；嵌套 manifest 中的未来字段按“忽略未知字段”处理。
* 通道可设置 `minClientVersion`；过旧客户端收到 `blocked/client_too_old`，不能收到无法解释的强制升级。
* 所有新字段必须可选，删除或忽略字段不得改变 v1 字段语义。

## 8. 一键验收

```bash
./tests/acceptance.sh
```

覆盖：

1. 干净 HOME + 干净安装目录，首帧成功。
2. 缺少默认/必需字体，probe 和服务端阻断。
3. 安装路径无法创建（路径被普通文件占用，OS 返回 `EEXIST`），预检失败且不下载。
4. 首次下载中途断开，服务端 206 Range 后续传并完成首帧。
5. protocol v1 客户端读取含 v2 构建产物的兼容配置。
6. 下载完成但未安装/未首帧时，控制台不能显示升级完成。
7. 新版本首帧失败后，旧 `current` 保留并通过旧版本首帧回退。
8. 两次构建字节一致、artifact 体积可比较、绝对路径泄漏为空。

## 9. 生产化前还必须替换的边界

当前实现便于离线 CI 验收：HTTP 服务没有鉴权/租户隔离，签名只做 sha256，TLS 和包签名需接入企业 PKI；SQLite 适合小规模或边缘节点，大规模应替换为 Postgres 并保留同样的报告模型；真实图形端还应增加截图/像素差异和崩溃证据归档。
