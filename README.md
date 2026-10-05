# C 端窗口交付验证器

仓库包含一个真实 C/SDL2 窗口程序，以及把跨机部署落到可验收流程的交付验证器：

- Web 控制台登记设备声明能力、发行通道和升级批次。
- Python 标准库控制面提供资源、安装清单、构建指纹和每设备 probe/事件查询。
- SQLite 保存 releases、fingerprint、batches、reports。
- C 端代理实探字体、X11/Wayland 后端、显示缩放、库、磁盘和本地写权限。
- 支持预检、可续传下载、安全安装、首帧验证、失败回退。
- 下载完成只显示“已下载”；只有首帧验证成功才显示“升级完成”。
- 提供 protocol v1/v2 兼容窗口，旧客户端不会收到无法解释的顶层新字段。

## 快速验收（无需图形环境）

```bash
./tests/acceptance.sh
```

该命令构建确定性 C 测试夹具并覆盖：干净 HOME、缺字体、无法写安装路径、下载中断续传、旧客户端接新字段、下载后未首帧不得完成、首帧失败回退，以及可重复构建/无开发者绝对路径。

## 启动控制台

```bash
python3 deploy/server.py --host 127.0.0.1 --port 18080
# 打开 http://127.0.0.1:18080
```

发布示例产物：

```bash
python3 tools/build_releases.py --mode fixture --version 1.0.0 \
  --channel fixture --policies system,bundled --out dist/release

curl -sS -X POST http://127.0.0.1:18080/api/admin/releases/publish \
  -H 'content-type: application/json' \
  -d '{"path":"dist/release/visual-window_1.0.0_fixture_linux_bundled.tar"}'
```

设备升级：

```bash
python3 client/agent.py upgrade \
  --server http://127.0.0.1:18080 \
  --device-id bench-01 \
  --home ./var/home-bench01 \
  --install-root ./var/install-bench01
```

只运行探测：

```bash
python3 client/agent.py probe \
  --home ./var/home-bench01 \
  --install-root ./var/install-bench01
```

## 真实 SDL 窗口

本地或 Docker 中需要 SDL2、SDL2_image、X11/Wayland/Xvfb 等组件。真实首帧由 `src/main.c` 在第一次 `SDL_RenderPresent` 后写 marker：

```bash
python3 tools/build_releases.py --mode sdl --version 1.0.0 \
  --channel stable --policies system,bundled --requires-display \
  --out dist/release
```

真实设备必须由 probe 证明 `display.available=true`、图形后端和缩放结论可用；无显示器 CI 的 fixture 通道不能冒充真实窗口首帧。

## 文档

完整的探测字段、system/bundled 体积与兼容性依据、可重复构建、兼容窗口、回退和验收步骤见：

- [docs/deployment.md](docs/deployment.md)

## 原有 noVNC 可视化

```bash
docker compose up --build
# http://localhost:6080
```

noVNC 只是远程显示容器内真实 C/SDL2 窗口的链路；交付验证器不把 noVNC 或镜像中已安装的组件当作目标设备能力。
