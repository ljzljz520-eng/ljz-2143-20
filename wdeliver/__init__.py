"""WDeliver —— C 端窗口交付验证器。

模块边界：
- ``wdeliver.manifest``  发行清单的版本协商 / 多版本视图 / 向前兼容解析
- ``wdeliver.builder``   可重复构建：打包、去开发者绝对路径、指纹、依赖对比
- ``wdeliver.server``    Web 控制台 + 资源/清单服务 + 探测结果入库
- ``wdeliver.client``    预检 → 下载 → 安装 → 首帧验证 → 失败回退
"""

CLIENT_VERSION = "1.2.0"
BUILD_TOOL = "wdeliver-build"
