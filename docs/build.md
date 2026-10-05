# 可重复构建步骤

所有命令在仓库根目录执行，仅需 Python 3.11+ 标准库（PNG 解码/压缩均为标准库）。

## 1. 构建

```bash
python3 -m wdeliver.builder.build --version 1.0.0
# 产物：
#   packages/visual-window-app_1.0.0.wdz         安装包（确定性 tar.gz）
#   packages/visual-window-app_1.0.0.build.json  构建记录（双指纹 + 文件清单）
```

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--version` | 1.0.0 | 版本号；写入载荷 BUILD.json，参与 build_id |
| `--epoch` | 1700000000 | SOURCE_DATE_EPOCH；固定后构建与时间无关 |
| `--bg-width/height` | 320×180 | PNG→PPM 目标尺寸 |
| `--payload` | deploy/payload | 载荷源（只读：构建在临时副本中进行，不污染源树） |
| `--out-dir` | packages | 产物目录 |

## 2. 可重复性保证（逐项）

1. **tar 成员顺序**：按相对路径排序；
2. **mtime**：所有成员 mtime 固定为 epoch；
3. **uid/gid/uname/gname**：归零/清空；
4. **mode**：统一 0644（hooks 下 Python 入口 0755）；
5. **gzip 头**：`mtime=0`，compression=9，文件名空；
6. **PNG→PPM 转换无随机量**：最近邻缩放，确定性输出；
7. **build_id**：对 `(path, size, sha256(content))` 序列做 sha256，
   版本号经 BUILD.json 参与哈希（不同版本必得不同 build_id）。

验证：

```bash
python3 -m wdeliver.builder.build --version 1.0.0
cp packages/visual-window-app_1.0.0.wdz /tmp/run1.wdz
python3 -m wdeliver.builder.build --version 1.0.0
cmp /tmp/run1.wdz packages/visual-window-app_1.0.0.wdz && echo "字节级一致"
```

（验收脚本 T8 每次都会执行该比较。）

## 3. 开发者绝对路径红线

构建器打包前扫描所有**已声明**文本文件（.py/.c/.h/.json/.toml/.md/.txt/.sh）：

- 命中 `/home/<user>`、`/Users/<user>` 一律拒绝（退出码 2）；
- 命中构建机真实 CWD/HOME 同样拒绝；
- shebang 仅放行可移植形式（`#!/usr/bin/env ...`、`/bin/sh` 等）；
- 未在 `payload.toml` 声明的文本文件会触发安全网报错（防止漏扫）。

验证包内确实干净：

```bash
mkdir -p /tmp/x && tar -xzf packages/visual-window-app_1.0.0.wdz -C /tmp/x
grep -rInE "/(home|Users)/[A-Za-z0-9._-]+" /tmp/x \
  --include='*.py' --include='*.c' --include='*.h' --include='*.json' \
  --include='*.toml' && echo "存在泄漏" || echo "无开发者绝对路径"
```

## 4. 指纹

| 指纹 | 算法 | 含义 | 用途 |
|---|---|---|---|
| `build_id` | 内容树 sha256 | 与时间无关的构建身份 | 通道绑定、数据库主键、幂等升级 |
| `package_sha256` | 归档字节 sha256 | 下载物完整性 | 下载完成后的最终背书 |
| `files[].sha256` | 每个成员 sha256 | 解包后逐文件复核 | 防止归档层通过但文件损坏/替换 |

数据库 `builds` 表留存全部指纹；控制台“构建指纹”卡片可查。

## 5. 发布到服务器

```bash
python3 -m wdeliver.builder.publish \
  --server http://127.0.0.1:8448 \
  --build-record packages/visual-window-app_1.0.0.build.json
# 内部：PUT /api/packages/<file>（上传字节）→ POST /api/builds（登记指纹）
```

## 6. 依赖对比报告

```bash
mkdir -p /tmp/x && tar -xzf packages/visual-window-app_1.0.0.wdz -C /tmp/x
python3 -m wdeliver.builder.deps \
  --payload /tmp/x --package packages/visual-window-app_1.0.0.wdz
# 产物：packages/deps-report.json 与 docs/deps-comparison.md
```

系统组件（libSDL2 / fontconfig / Xvfb / sans 字体）是否存在全部由当前机器实测，
换机器结果会变，文档不缓存“齐全”结论。
