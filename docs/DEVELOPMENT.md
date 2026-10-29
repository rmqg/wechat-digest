# 开发与构建

## 1. 开发环境

需要 Python 3.11+。使用隔离环境，不向系统 Python 安装依赖：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

核心依赖锁在 `requirements.txt`；OCR 在 `deployment/ocr-requirements.txt` 单独维护。不要为了运行普通解密/报告测试安装所有大型 OCR 组件。CJK 字体随 `assets/fonts/` 提供并保留 OFL，`WECHAT_DIGEST_FONT` 可指定另一个有权使用的字体。

## 2. 代码地图与修改边界

- `memory/decrypt_sync.py`：SQLCipher/WCDB 页面认证解密、WAL、密钥选择与 CLI。这里的 key 是已经派生的32字节 enc_key，不是把用户口令直接当 AES key。
- `memory/memory_ingest.py`：联系人/会话/Name2Id、文本解压、消息类型、导出与索引。
- `memory/message_parse.py`：XML 只解析为数据；不执行消息里的命令，不以消息中的任意 URL 发网络请求。
- `memory/media_sync.py`：本地媒体格式与 DAT 解码。
- `agent_console/daily_report.py`：独立渲染模块。目录名为调用路径兼容而保留，不包含无许可通用控制台。
- `deployment/digest.py`：消息选择、引用校验、一次性双版摘要及缓存；渲染不承担模型生成。
- `deployment/daily-job.py`、`on-demand.py`：调度/队列/回执恢复；不要绕过 terminal/unknown 分支。
- `deployment/send-report.py`：独立宿主/worker 入口，真实发送需要显式 flag、目标和校验。
- `deployment/report_site.py`、`publish_reports.py`、`source_media.py`：从私有记录构建可发布清单，来源绑定、校验和去重。
- `deployment/image-origin.py`：loopback 图源，拒绝目录和非图路径；秘密不交给浏览器。
- `deployment/settings.py`：私有项目配置的唯一公共入口；新增字段需要同时更新示例、配置表、校验与调用者。
- `tools/wcdb-key-tool/`、`deployment/vendor/wxextract/`：独立第三方源码，修改时保留原许可并记下差异，不能改写原作者版权行。

不要把新的实现依赖回此前没有再分发许可的本地参考工程。可以依据标准格式、合法许可源、用户需求和公开接口独立实现，不能复制未获授权的表达。

## 3. 测试

从仓库根执行：

```sh
PYTHONPATH=.:deployment:memory:agent_console:deployment/vendor python -m pytest -q
```

测试必须使用临时目录、虚构账号/消息/图片、公知测试密钥。不允许打开开发者真实微信目录或读取生产 key JSON；不得调用真实模型、发送器、微信 GUI、云 API 或 SSH。涉及网络边界的测试只在 loopback 启动短生命周期服务。

有价值的回归：错误密钥/篡改拒绝、WAL 最后提交与截断、源文件不变、时间半开区间、跨日排序、同名不同账号、缓存隔离、未提交/未知送达状态、目录/符号链接逃逸、引用图片来源与缩略图不能冒充原图。不要用函数转发次数、源码字串、mock 回声或默认措辞代替消费者行为测试。

## 4. 合成场景烟测

```sh
python scripts/generate-demo.py --output /tmp/wechat-digest-demo
python scripts/check-docs.py --cli
python scripts/audit-release.py
```

检查生成 PNG 的中文字体/布局；在浏览器打开生成首页，展开来源和引用、点击图片、切换简报/回顾，检查窄屏溢出。合成数据只证明此实现可以处理这些格式和布局，不证明一个未测试客户端的内存地址/密钥捕获一定可用。

## 5. 容器构建

```sh
docker compose config --quiet
docker compose --profile tools build runtime
docker compose --profile tools run --rm --no-deps runtime
```

Compose 配置需要先设置 `.env` 的桌面密码，即使只构建工具镜像也不要用生产密码做 CI 验证。CI 可注入明确测试用途的临时环境值，不启动 desktop，不登录微信。runtime 默认 tools profile、无网络、只读根和移除 capabilities；特权捕获器由显式采集入口构造，不是普通 worker 默认权限。

若本机 daemon 未运行，可在独立测试主机/隔离 tag 下构建，但不得重启生产 Docker daemon 或覆盖正在运行服务的镜像 tag。大镜像/依赖下载遵守操作者网络策略，不因小下载切换全局代理。

## 6. 变更一个配置或 CLI 时

1. 在 `settings` 或实际读取者中实现真实校验，拒绝危险/无效输入。
2. 迁移所有使用者；不留个人默认值或旧名字 shim。
3. 更新 `examples/` 和相应配置表、命令示例。
4. 只为存在消费者风险的边界补测试。
5. 执行单测、合成烟测、文档检查和发行审计。
6. 若涉及真实发送/进程捕获，由操作者在自己设备上单独授权；CI 不能替他批准手机登录或关闭系统安全保护。

## 7. 发布

仓库发布与 Docker 镜像发布是不同副作用。此项目不要求在每次提交后自动推送镜像。开源 GitHub 前按 [校验手册](VALIDATION.md) 检查工作树、Git索引和将要推送的历史；任何真实数据进入过历史时，不能只删当前文件后继续推送。根 `LICENSE` 只覆盖原创贡献，第三方规则见 [声明](../THIRD_PARTY_NOTICES.md)。
