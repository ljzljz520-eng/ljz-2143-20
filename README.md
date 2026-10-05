# Visual Window App + WDeliver 跨机窗口交付验证器

本仓库包含两部分：

1. **Visual Window App** —— 真实 C/SDL2 桌面窗口程序（下方原有说明，Docker 内
   Xvfb + noVNC 可见）。
2. **WDeliver 交付验证器（`wdeliver/`）** —— 把“跨机部署说明”落地为可验收的
   C 端窗口交付系统：

   - Web 控制台：登记设备能力、配置发行通道与升级批次（`wdeliver/server/console.html`）
   - 服务端 + SQLite：资源与安装清单（协议协商）、构建指纹、每台设备探测结果
   - 客户端：真实探测（字体/图形后端/显示缩放/写入权限，**无默认假设**）
     → 预检 → 断点续传下载 → 校验 → 原子安装 → **首帧验证** → 失败回退
   - 构建器：可重复打包、开发者绝对路径红线、随包/系统依赖体积对比
   - 兼容窗口：客户端与服务器清单格式按 1.0/1.1/1.2 协商，旧客户端忽略未知字段
   - **网页只在首帧 receipt 到达后才显示“升级完成”，下载完成不算完成**

快速入口：

```bash
python3 tests/acceptance.py     # 29 项验收：干净目录/缺字体/只读路径/中途断网/旧客户端新字段/回退…
```

文档：

- [跨机部署说明（探测输出对照）](docs/deployment.md)
- [可重复构建步骤](docs/build.md)
- [回退步骤](docs/rollback.md)
- [兼容窗口（客户端↔服务器清单格式）](docs/protocol-compat.md)
- [无显示机器手工演示脚本](docs/wdeliver-demo.md)
- [依赖体积/兼容性对比（自动生成）](docs/deps-comparison.md)

---

# Visual Window App (C + SDL2 + noVNC)

这是一个真实的 **C 语言桌面 GUI 程序**（基于 SDL2），程序在 Linux 容器中启动窗口并将图片渲染为背景图。
通过 **Xvfb + x11vnc + noVNC**，你可以直接在浏览器看到并操作这个窗口。

## 特性

- C11 + SDL2 图形窗口（非 Web 页面）
- SDL2_image 加载背景图（`assets/background.png`）
- 清晰事件循环（刷新、关闭事件）
- 严格编译参数：`-Wall -Wextra -Werror`
- 一条命令启动整套可视化链路

## 技术架构

```text
Browser (http://localhost:6080)
        |
      noVNC (WebSocket)
        |
      x11vnc (VNC)
        |
      Xvfb (:99 virtual display) + fluxbox
        |
   C SDL2 GUI App (real window rendering)
```

## 目录结构

```text
visual-window-app/
├── docker-compose.yml
├── README.md
├── assets/
│   └── background.png
├── docker/
│   ├── Dockerfile
│   └── entrypoint.sh
├── src/
│   ├── main.c
│   ├── window.c
│   ├── window.h
│   ├── renderer.c
│   └── renderer.h
└── Makefile
```

## 一键启动

```bash
docker compose up --build
```

启动完成后，在浏览器打开：

```text
http://localhost:6080
```

## noVNC 说明

noVNC 仅用于远程显示容器中的真实图形窗口。
本项目本质仍是标准 C GUI 程序，不是 Web 应用，也不是 Canvas 模拟。

## 背景图

当前仓库默认提供了 `assets/background.png` 作为占位背景图。
如果你要替换成指定图片，直接覆盖同路径文件即可，无需改代码。

