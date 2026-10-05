# 手工演示脚本（无显示的 CI/开发机）

以下脚本在当前这种“无 X、无 SDL2、字体不全”的机器上即可完整演示，
全程结论来自实测。

```bash
set -e
# 0) 验收（推荐先跑，29 项应全绿）
python3 tests/acceptance.py

# 1) 构建 + 依赖对比
python3 -m wdeliver.builder.build --version 1.0.0
python3 -m wdeliver.builder.build --version 1.1.0
mkdir -p /tmp/x && tar -xzf packages/visual-window-app_1.1.0.wdz -C /tmp/x
python3 -m wdeliver.builder.deps --payload /tmp/x \
  --package packages/visual-window-app_1.1.0.wdz

# 2) 起服务 + 发布
WDELIVER_TEST=1 python3 -m wdeliver.server.app --port 8448 & SRV=$!
sleep 1
python3 -m wdeliver.builder.publish \
  --build-record packages/visual-window-app_1.1.0.build.json
BID=$(python3 -c "import json;print(json.load(open('packages/visual-window-app_1.1.0.build.json'))['build_id'])")
curl -s -X POST localhost:8448/api/channels -H 'Content-Type: application/json' \
  -d "{\"name\":\"stable\",\"build_id\":\"$BID\",\"batch\":\"b1\",\"rollout_pct\":100,\"require\":{}}"

# 3) 正常升级（看探测如实报告 nullfb / scale unknown）
rm -rf /tmp/wd
python3 -m wdeliver.client.updater --server http://127.0.0.1:8448 \
  --device demo --install-dir /tmp/wd/i --data-dir /tmp/wd/d \
  --cache-dir /tmp/wd/c --state-dir /tmp/wd/s update

# 4) 中途断网升级（新设备；首次连接发 40KB 就断）
WD_DOWNLOAD_CUT=40960 python3 -m wdeliver.client.updater \
  --server http://127.0.0.1:8448 --device demo-cut --install-dir /tmp/wd2/i \
  --data-dir /tmp/wd2/d --cache-dir /tmp/wd2/c --state-dir /tmp/wd2/s update

# 5) 控制台
# 浏览器打开 http://127.0.0.1:8448/ ，观察“交付结论”列：
# 只有 ACTIVE 设备出现绿色“升级完成（首帧已验证）”。

kill $SRV
```
