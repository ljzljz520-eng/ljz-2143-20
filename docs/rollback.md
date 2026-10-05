# 回退步骤（可重复）

## 1. 自动回退（交付链路内置）

激活采用 `install/current -> versions/<version>` 符号链接的原子 rename。
首帧验证分两阶段：

1. **staged**：在 `versions/<new>/` 内先跑首帧钩子。失败则删除该目录、
   `current` 原封不动，设备继续运行旧版本（状态 FAILED）。
2. **active**：切换 current 后再跑一次首帧钩子（模拟真实启动路径）。
   失败则：
   - 把 current 原子切回旧版本相对目标（如 `versions/1.0.0`）；
   - 对旧版本再跑一次首帧钩子，复验旧版可用；
   - 上报 `ROLLEDBACK`，receipt 为旧版本首帧证据；新版目录保留供排查。

复现：

```bash
# 设备先在 roll 通道装 1.0.0（控制台把 roll 通道绑到 1.0.0）
python3 -m wdeliver.client.updater ... --channel roll update
# 控制台把 roll 通道切到 1.1.0，然后：
WD_FORCE_FIRSTFRAME_FAIL=active \
  python3 -m wdeliver.client.updater ... --channel roll update
# 退出码 16；服务端 phase=ROLLEDBACK；readlink install/current = versions/1.0.0
```

也可只让暂存阶段失败（新版不会被激活）：`WD_FORCE_FIRSTFRAME_FAIL=staged`。

## 2. 手工回退（运维步骤）

```bash
# (1) 查看当前与可回退版本
readlink /opt/visual-window-app/current        # 如 versions/1.1.0
ls -1 /opt/visual-window-app/versions/         # 1.0.0  1.1.0

# (2) 原子切回（ln -s + rename，中间无空窗）
ln -s versions/1.0.0 /opt/visual-window-app/current.tmp
mv -Tf /opt/visual-window-app/current.tmp /opt/visual-window-app/current

# (3) 复验旧版本首帧（必须成功才算回退完成）
python3 /opt/visual-window-app/current/hooks/firstframe.py \
  --backend nullfb \
  --root /opt/visual-window-app/current \
  --out-dir /var/lib/wdeliver/rollback-check \
  --version 1.0.0 --build-id previous --stage active
# 退出码 0 且 receipt.json 含 frame_sha256 → 回退成功
# 有真实显示的目标机把 --backend 换成 sdl（WD_SDL_BINARY 指向编译产物）
```

## 3. 下载中断的“回退/重试”

下载从不触碰 `current`：半截文件只存在于 `<data>/downloads/<build_id>.wdz.part`。
网络恢复后重跑同一命令，客户端带 `Range: bytes=N-` 续传（服务端返回 206），
跨进程同样有效。重试次数耗尽（默认 8，`WD_MAX_RESUMES` 可调）才报 FAILED，
此时旧版本仍在运行。

## 4. 服务端侧回滚发布

控制台把通道改回旧 build_id（或勾选“暂停通道”）即可：
设备下次协商清单时收到旧版本/无升级；批次标签 (`batch`) 建议一并更新，
以便事件轨迹区分两次放量。设备已 ACTIVE 的新版本不会被强制降级，
需在设备端按第 1/2 节触发回退。

## 5. 状态与审计

- 本地：`<state>/journal.jsonl`（阶段、下载续传次数、首帧结果），
        `<state>/state.json`（current/previous/backend），
        `<state>/firstframe-*/receipt.json`（两阶段首帧证据）。
- 服务端：`device_events` 表 / `GET /api/devices/<id>/events`。
