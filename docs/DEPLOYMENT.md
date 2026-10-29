# 部署：Linux / iStoreOS 从零配置

本文面向独立的新公开发布实例。只在你有权访问的账号/数据库上操作。Linux 宿主应有与镜像相容的 CPU 架构、Docker Engine/CLI 和 Compose v2 插件、Python 3 及 venv、git、OpenSSH/rsync（需发布时）、足够的持久磁盘和 Docker daemon 管理权限。iStoreOS/OpenWrt 还需 procd/`rc.common`；非 OpenWrt Linux 可自行管理 `daily-job.py`，但不要假设这些 initd 脚本可直接用于 systemd。Docker socket 权限近似 root：不要对公网暴露 daemon，不要把 docker 组权限授予不可信账户。

从公开仓库克隆源码至 `/opt/wechat-digest`。下面使用 `main` 分支；生产部署前应审阅并固定到已核验的提交，不复制旧项目 runtime 或聊天数据：

```sh
sudo install -d -m 0750 -o root -g root /opt/wechat-digest
sudo chown "$(id -u):$(id -g)" /opt/wechat-digest
git clone --branch main https://github.com/rmqg/wechat-digest.git /opt/wechat-digest
cd /opt/wechat-digest
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

先初始化 runtime 和配置模板；该命令拒绝覆盖既有 `project.json`：

```sh
.venv/bin/python deployment/bootstrap.py --init-runtime
chmod 0600 runtime/project.json
```

编辑项目配置中的镜像引用并核对 desktop 容器名；此时不用填写 group 目标。然后创建权限为 0600 的 `.env`，填入强 `DESKTOP_PASSWORD`（不要在命令行回显该值），需要时设置 `PUID`、`PGID`、`DESKTOP_USER`、`DESKTOP_BIND=127.0.0.1`、`DESKTOP_PORT`、`TZ`。Compose 把 desktop 3000 默认限制到本机并将 `runtime/gui` 持久化至 `/config`。


先检查版本的 `Dockerfile`、`docker-compose.yml`、`requirements.txt`、锁定文件和目标平台；运行时基础镜像为 Python 3.12 slim，Dockerfile 装载调试/GUI与字体依赖并构建 runtime 镜像。只构建本地工具镜像，不要在未经审查下启动 desktop 服务：

```sh
docker compose --profile tools build runtime
docker image inspect --format '{{.Id}}' wechat-digest-runtime:1.0.0
```

上一步的项目配置已指定本机镜像；Compose 默认 desktop image 是 `ghcr.io/nickrunning/wechat-selkies:0.0.12-minimal`，须在审阅来源/架构后使用。限制 `.env` 为 `0600`，不要在命令行/history 回显密码。启动桌面：

```sh
docker compose up -d desktop
```
Compose 的登录端口仅绑定宿主 `127.0.0.1:3000`。若从管理工作站远程维护，可用 SSH 本地端口转发（把示例用户名/主机替换为你的 SSH 主机；不开放路由器端口）：

```sh
ssh -N -L 3000:127.0.0.1:3000 operator@example.com
```

在工作站浏览器打开 `http://127.0.0.1:3000`，使用 `.env` 的桌面登录凭证，然后在桌面 UI 内登录微信。完成后不要关闭 persistent GUI volume；确认桌面 UI 显示账号身份及其 GUI account directory 与将选中的目录对应。绝不可仅凭同名昵称或账号排列顺序作选择。




```sh
.venv/bin/python deployment/bootstrap.py --pin-images
```

只读 preflight 可用 `--check`；成功结果应包含 `status=ready`，并确认 pins、desktop image ID 和 runtime 目录存在：

```sh
.venv/bin/python deployment/bootstrap.py --check
```


`runtime/image-pins.json` 保存引用与 `sha256:` 镜像 ID；pin 前要求 runtime/keyscan 相同且 desktop 容器 ID 匹配。任何镜像更新都须先确认新镜像内容，再显式 `--refresh-pins`；切勿静默覆盖同名 tag。

### 显式选择账号并采集密钥

在 desktop GUI 中手动登录目标账号，先确认客户端当前账号，并核实 `runtime/gui/` 下对应的精确数据库目录名。此采集方式是 Linux desktop/容器流程，不应宣称 Windows 或 macOS 支持。先进行默认检查（无 `--check` 参数；省略 `--capture` 即仅检查）：

```sh
.venv/bin/python deployment/prepare-collection.py
```

检查通过后，在操作者明确选定账号目录的前提下显式运行采集：

```sh
.venv/bin/python deployment/prepare-collection.py --account 'ACCOUNT_DIRECTORY' --capture --timeout 300
```

`ACCOUNT_DIRECTORY` 必须替换为人工确认的精确目录名；只有候选唯一、身份无歧义时才省略 `--account`。超时单位秒，默认 300。Capture 运行隔离无网络 keyscan 容器内的 `capture_41.py`，只读挂载 GUI 账号目录、与 desktop 共享所需进程命名空间，并等待操作者在客户端手动重新登录时附加硬件断点。此流程会实际影响客户端调试状态，不是无副作用探测。成功需得到 HMAC 验证 key，写入 `runtime/keyscan/all_keys.json` 与 `runtime/selection.json`；按秘密保管。账号不符、模糊、断点失败、超时、无验证密钥都应停止，不循环尝试账户。

采集容器内 `capture_41.py` 从镜像提供的 `/opt/wcdb-key-tool` 导入 key scan 工具（Dockerfile 将 `tools/wcdb-key-tool/` 打包到该路径）；不要替换为未经许可的第三方实现。采集目录只读挂载，key output 以 HMAC 验证后写入私有 runtime。不要记录实际密钥、账号路径或数据库内容。

随后只读列出可用群（无需先设目标 group 或模型）：

```sh
.venv/bin/python deployment/run-digest.py --list-groups
```

操作者对照输出、已登录数据源和授权范围确认目标后，再填准确的 `group.id`、`group.name`。发送才需要 `bot.username`；按需提及扫描才需要 `bot.names`。摘要/模型阶段再准备 `runtime/gemini-model.json`。目标不确定时不可继续。

## 图片准备与 OCR

图片处理及原图下载使用 `runtime/image-pins.json` 中 pin 的 runtime image，并传入当前项目配置；它们按 `group.id/name` 生成 image manifest 与 original-fetch request，web context 仅读取配置群组的消息。source DB 路径保持 `/config/...`。原图下载容器将整个 `runtime/gui` 只读挂载至 `/config`，以便保留 `xwechat_files/...`、`Documents/xwechat_files/...` 两类受支持目录结构，并读取旁侧 MMKV/CDN/媒体资源；不得只 bind 某一级 DB 子目录或用仅复制 `.db` 的目录替换整个 GUI storage。

图片 worker 分别只读挂载部署代码、memory 模块、GUI 账号数据及项目配置；可写任务/缓存位于 runtime work 下。无需为了图片 worker 创建 `runtime/python-deps/` 才能启动，它不是强制挂载。OCR 依赖只在 `runtime/ocr-deps/` 存在时作为独立挂载提供；使用 OCR 时依赖 wheel 必须与架构匹配。模型 JSON 只在需要的视觉/摘要模块挂载，密钥不得复制入任务目录。

OCR 使用 `rapidocr-onnxruntime` 本地运行，依赖 wheel 清单在 `deployment/ocr-requirements.txt` 和 `deployment/ocr-wheel-lock.json`，需要与部署架构匹配的 `runtime/ocr-deps/`；不属于在线模型配置。CLI 独立调用要求 manifest/output：

```sh
.venv/bin/python deployment/ocr_images.py --manifest PATH_TO_IMAGE_MANIFEST --output PATH_TO_OCR_JSON --cache-dir runtime/digest/ocr-cache
```

上面两个 PATH 要换成实际存在且由当前任务生成的文件；不要对合成样例以外的私人素材伪造示例输出。`--max-images N` 可限制任务数量，默认 0 表示不限。每条输入必须标为 available 且质量为 `original`/`client_full`，资源 SHA256 须匹配；缩略图会被拒绝。输出按消息 key 记录 ok/failed/pending，命令非零或有 failed/pending 时需排查，不能作为已完成证据。

## 报告生成、prepare-only 与发送

先列群组（无模型调用）并确认源账号和 `project.json` 的目标配置相符：

```sh
.venv/bin/python deployment/run-digest.py --list-groups
```

实际自定义摘要默认生成最近 24 小时，可传正数 `--hours H`，默认结束时间为当前 epoch；也可加 `--end EPOCH` 固定窗口结束：

```sh
.venv/bin/python deployment/run-digest.py --hours 24
```

成功应有 JSON 结果包含 brief/review 对应的 HTML/PNG/JSON 路径，且文件实际存在于 `runtime/reports/`。审阅内容、目标窗口、模型请求费用、报告可见信息后再进入发送流程。按需模式的 `--request-id` 仅供 trigger service，以小写 64 hex ID 并配对 `--start`、`--end`，且 start < end；不要直接从命令行发起任意用户请求。

日报只准备不发送：暂停正式 cron 后运行一次：

```sh
.venv/bin/python deployment/daily-job.py --prepare-only
```

此模式生成并校验 daily bundle，不调用 sender。验证后恢复原 cron。

`send-report.py` 默认 dry-run，只检查 PNG、同名 HTML/JSON、路径和目标，不访问 GUI：

```sh
.venv/bin/python deployment/send-report.py runtime/reports/REPORT.png --chat-id '10000000000@chatroom'
```

示例 ID 是明确虚构的；只有实际发送才替换为经人工核验的目标 ID。PNG 必须是报告目录中的生成报告图像，且有相应 HTML/JSON。默认预期输出 `status=prepared`、`sent=false`，不得据此声称消息已发。真实发送唯一授权参数为 `--send`；发送前由有权操作者在操作点复核目标群、桌面登录账号、UI calibration、待发送内容及 hash 并明确批准。请勿在未确认时发送。发送结果若为 `unknown`、超时或无可解析 receipt，停止并核对群内实际消息/本地状态，禁止盲目重发。



## 升级与验证回滚

升级前先停止 cron 和三个长驻服务，保存当前源码版本号、Compose 环境文件（加密保管）、`runtime/project.json`、模型/vision 凭证、`image-pins.json`、key与selection、GUI运行目录、校准资产、trigger/access-notice、递送状态及收据、图片/发布所需保留状态。备份加密并限制访问；确认本机旧 Docker image ID 仍存在，避免以同名 tag 覆盖后丢失旧镜像。

将新版本部署到独立目录或保留可精确恢复的版本控制 commit；按该版 Dockerfile/Compose 为目标架构构建新 image，审阅配置迁移说明。先更新配置中的 image refs，启动匹配 desktop，再执行明确升级 pin 流程 `bootstrap.py --refresh-pins`；绝不因 tag 名没变就跳过 ID 比对。先做非发送检查：bootstrap check、prepare-collection 默认检查（不带 `--capture`）、list-groups、image-origin `--check-config`、daily `--prepare-only`。审阅生成文件与旧 receipt/state，再恢复调度。

回滚：停 cron/服务，恢复上一版源码、环境文件与该版本兼容的 runtime 备份；恢复以前的 image reference/pins，并确认本机仍有记录的旧 ID，且 desktop container image ID 匹配。不要刷新 pins 伪装成旧版本；如旧 ID 不存在，需重建准确旧版 image 并先校验 ID。运行不发送的 preflight 并检查递送状态和收据，确认没有不明发送后再恢复定时任务。任何未知发送保持 unknown；不可删除 state/receipt 使回滚“成功”。
