# 依赖对比（随包 vs 系统）

安装包总体积：**150987 字节**

## 随包携带项

| 项目 | 包内路径 | 体积(字节) |
|---|---|---:|
| nullfb 纯软件帧缓冲 | `hooks/firstframe.py` | 4030 |
| 位图字体 3x5（44 字形） | `assets/fonts/bitmap_3x5.json` | 602 |
| PPM 背景（PNG 转换后） | `assets/background.ppm` | 140786 |
| **随包小计** | | **145418** |

## 系统组件（本机实测）

| 组件 | 结论 | 证据 |
|---|---|---|
| libSDL2 | ❌ 缺失 | not in ldconfig cache |
| libSDL2_image | ❌ 缺失 | not in ldconfig cache |
| fontconfig (fc-list) | ✅ 存在 | /usr/bin/fc-list |
| X server 客户端工具 (xdpyinfo) | ❌ 缺失 | xdpyinfo not found in PATH |
| Xvfb 虚拟显示 | ❌ 缺失 | Xvfb not found in PATH |
| 系统 sans-serif 字体 | ✅ 存在 | {"sans": "DejaVu Sans", "mono": "DejaVu Sans Mono", "cjk": null} |

## 选择依据

1. 随包携带（包内实测体积见上）：nullfb 后端 + 位图字体 + PPM 背景，保证无显示/无字体/无 SDL2_image 的机器仍能完成首帧验证；
2. 系统组件（不占包体）：libSDL2 / X server / 系统字体 —— 真实窗口渲染依赖它们，安装前由 probe 实测；缺失则通道策略要求 sdl 时预检失败，允许 nullfb 时降级并在 receipt 如实标注；
3. 背景选用 PPM 而非 PNG：构建期转换，免除对 libSDL2_image 的硬依赖，包体由原图大小决定（见 asset_conversion 字段），代价是无透明/高压缩；
4. 不随包 libSDL2：单个 .so 约 MB 级且与 glibc/ABI 强耦合，随包体积大且跨发行版兼容性差；改为声明系统组件 + 实测门禁 + 回退后端。

> ⚠️ 本机缺失：libSDL2、libSDL2_image、X server 客户端工具 (xdpyinfo)、Xvfb 虚拟显示。这些项由探测在目标机上重新实测，不在文档中默认齐全。
