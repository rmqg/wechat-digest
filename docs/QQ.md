# QQ 接入与运行

本功能使用 OneBot v11 WebSocket API 接入 QQ 群，不读取微信数据、不复用微信 runtime，也不会接入或读取私有 Agent/Memory 数据。QQ 侧数据、任务、回执、报告和模型配置均放在独立目录；不要把 QQ 报告放进微信网站发布目录。

## 前提与边界

- 自行维护已经登录且获准使用的 QQ 账号，以及兼容 OneBot v11 的 NapCat 实例。NapCat/QQ 客户端安装、账号风控和上游 API 兼容性不由本项目保证；遵守相关产品条款。
- OneBot WebSocket 监听仅限本机或受控私网。启用访问令牌，令牌放到权限为 `0600` 的独立文件中；服务配置只记令牌文件路径。不要把 NapCat 管理端口映射到公网。
- Python 3.11+、本仓库依赖、持久化磁盘、模型兼容 API 和中文字体是运行依赖。含图片的 QQ 总结还必须准备 RapidOCR/ONNX 依赖；仅安装 `requirements.txt` 不包含 OCR。QQ 网站使用 Python 标准库 HTTP server，不需要 Web 框架。
- 只配置自己拥有或明确获准处理的单个群。群 ID、机器人 QQ ID、昵称均由部署者填入，不从消息自动猜测目标。
- 当前内容理解支持文本、提及和平台提供的完整图片（视觉/OCR/来源图片）；语音、视频和未展开的合并转发不伪造内容理解。此类消息的元数据仍可保留，但消息数量不等于所有媒体内容均已理解。

## 安装与隔离配置

复制 `examples/qq-service.example.json` 为私有运行配置，并复制模型模板为私有文件。以编辑器填入唯一群 ID/名称、机器人数字 QQ ID、昵称、OneBot WebSocket URL、`enabled_from` 启用时间、令牌文件、模型配置、数据目录与报告目录。每个群的服务配置、令牌、模型密码、运行目录和报告目录必须互相独立，文件权限建议目录 `0700`、秘密文件 `0600`。

```sh
python3 -m pip install -r requirements.txt
install -d -m 700 /srv/qq-digest/runtime /srv/qq-digest/reports
install -m 600 /dev/null /srv/qq-digest/runtime/onebot.token
# 通过秘密管理器或受控编辑器写入 token，不要放入 shell 历史或仓库。
```

含图片的生产部署可使用已验证的 Linux x86-64 / Python 3.12 OCR wheel 锁；安装器只下载官方 PyPI 中名称和 SHA256 均匹配的文件，不改变代理或客户端。其他架构需单独验证自己的 wheel 锁，不能使用 x86-64 二进制冒充兼容。

```sh
python3 scripts/install-ocr.py --target /srv/qq-digest/ocr-deps --wheel-dir /srv/qq-digest/ocr-wheels
# 运行 worker 时将 /srv/qq-digest/ocr-deps 加入 PYTHONPATH；已有 wheel 可用 --offline 安装。
# 或构建包含该 OCR 层的 Linux x86-64 工具镜像：
docker build --build-arg WITH_OCR=1 -t wechat-digest-runtime:1.1.0 .
```

相对路径以 JSON 配置文件所在目录为基准。服务配置至少应明确 `onebot_url`、`onebot_token_file`、`group_id`、`group_name`、`expected_self_id`、`bot_names`、`runtime_dir`、`reports_dir`、`model_config`、`enabled_from` 和 `daily_hour`。`enabled_from` 是 Unix 秒时间戳，防止启用前的历史消息被当作新触发。模板中的群、账号、密码、URL 和 token 路径均为虚构/占位，必须替换；不得将真实 token 或 API key 放入仓库。

推荐先在私有配置中将 OneBot WebSocket URL 指向本机 NapCat 服务；确认 token 权限和群/机器人身份后，以服务账号运行 QQ worker。先前台运行并观察日志/运行状态，不要一开始就启用公网站点或长期自动发送。

QQ worker 使用已鉴权的 OneBot v11 WebSocket API；网站服务才使用 HTTP。NapCat 对应 API 监听保持私有，不暴露管理端口。

## 首次验证：先只跑最近 24 小时

1. 在 NapCat 的 OneBot v11 WebSocket 配置中启用 API 与鉴权，明确只开放给服务端。核实 API 能认证、读取目标群历史，并确认自身 QQ ID 与配置一致。
2. 配置启用时间、独立目录和凭据，但不要提前运行 `qq_service.py --serve`：该模式会执行真实自动投递，不是 dry-run。
3. 使用 `QQClient.history(group_id, start, end)` 读取并保存最近 24 小时的私有快照，再执行 `qq_digest.py --group-id GROUP_ID --sources-json SNAPSHOT --start START --end END --model-config MODEL_CONFIG --runtime-dir PRIVATE_WORK --output-dir PRIVATE_REPORTS`。该 CLI 生成 HTML/PNG，不发送；API 可能计费，不是离线模式。
4. 核对群身份、时间、非空记录和完整双版后，再以 `qq_service.py --config PRIVATE_CONFIG --serve` 明确启用自动触发与投递。若需直接验证发送，调用 `QQClient.send_image` 并保留每版持久化状态/回执；结果未知不得重试，不得删状态强制重发。
5. 确认服务关闭后可从持久目录恢复任务、状态与报告。避免同时运行多个 worker 实例处理同一 `runtime_dir`。

离线示例使用 `python3 scripts/generate-demo.py --qq --output /tmp/qq-digest-demo` 生成虚构 QQ 数据和 HTML/PNG，不联网、不调用模型、不发送消息，不代表 AI 实际总结结果。

## 触发规则、时间窗和防重

- 日报固定 UTC+8、每天 05:00 计划生成；`daily_hour` 可配置。日报为前一天该时刻至当天该时刻的 24 小时窗口；部署启用前的日程不会立即补发。
- 真实 OneBot `at` 指向本机器人，或文本同时含 `@` 与任一配置的 bot 名字，均进入意图识别；不要求位置、空格或先匹配“总结”关键词。机器人自己的消息不触发也不纳入下一轮总结；普通提及、引用或转述是否属于直接总结请求由后续判定，不直接当成执行授权。
- 默认自定窗口最近 24 小时。请求可指定时间长度，最大 7 天（168 小时）；超限或无效请求被拒绝，不会静默缩短成 7 天。超过窗口上限需缩小窗口后重新请求。
- 触发请求使用持久化任务及逐版发送回执防重。确认送达后绝不自动重发；提交后无法确认的结果按未知状态停止，先核实 QQ 端实际消息再人工处理。取消任务只取消尚未发送的工作；发送中/结果未知不能撤销已提交的消息。
- 报告为空时不得生成/投递空白摘要；拒绝发送不是成功回执。永久不可重试的请求会终止并需修正输入，暂时性网络/API故障按有限退避恢复。

## Web 报告服务与共用入口

QQ 报告站点是 Python 标准库实现，可选监听本机 loopback 或 Unix socket。生产推荐每群一个私有 socket，并由 Docker Caddy 通过仅私有挂载的 `/config` 路径访问；不要将 socket 所在目录、报告目录或 token 挂进公网容器之外的共享数据区。每个 QQ 群使用独立站点用户名/密码及独立报告根目录，密码不能跨群或与 MSC 共用，不能写入仓库、URL、访问日志或工单。

独立站点配置模板为 [`examples/qq-site.example.json`](../examples/qq-site.example.json)，其中的群 ID/账户为虚构值，密码 verifier 是必须替换的占位符。为实际群生成密码 verifier 时使用站点程序的 `--hash-password` 输入流程；不要把明文密码写进配置、URL、命令参数或 shell history。每个群创建独立强密码和 scrypt verifier，站点配置设 `0600`。将 `reports_dir` 指到本群专属静态报告目录；`socket_path` 必须位于 Caddy 私有挂载可访问的目录，避免共享 `/config/private` 给无关容器。运行前执行 `python3 deployment/qq_report_site.py --config /private/path/qq-site.json --check-config` 检查配置，再按服务管理器运行 `--serve`。

同一群的明文密码需分别生成两种验证器：`qq_report_site.py --hash-password` 生成站点用的 scrypt；Caddy 的 `caddy hash-password` 从标准输入读取密码并生成 bcrypt。二者不能互换。不要使用会把明文放进进程参数的 `--plaintext`，也不要改写现有 MSC 的用户名或哈希。更新后先执行 Caddy `validate` 再平滑 `reload`；只重载本次新建的 QQ 站点进程，不重启共享代理。

浏览器可能缓存现有 MSC 的 HTTP Basic 身份；验证新账号时使用私密窗口或独立浏览器配置。根入口将按已验证用户名跳转到对应群；切换账号不是给一个账户增加其他群权限。

共用域名和 HTTPS 入口时，可在既有 Caddy `basic_auth` 认证后，按认证用户名 `http.auth.user.id` 做群隔离路由，每个账户只反代到自己群的 Unix socket；不要基于未经认证的路径参数挑选群。示意（仅展示 QQ 分流，不是完整站点配置）：

```caddyfile
@qq_demo vars {http.auth.user.id} qq_demo_account
handle @qq_demo {
    reverse_proxy unix//config/private/qq-demo.sock
}
```

实际 Caddy 站点还必须使用运维者已配置的 TLS、`basic_auth` 账户密码哈希、socket 私有挂载和旧 MSC `import`/路由；不要用此示例替换已有认证配置。每群独立账号应在同一认证入口配置，确保未匹配的请求仍走现有 MSC 路由并保留其原认证。QQ 与 MSC 只共用反代入口，绝不共用账号凭据、报告目录、静态资源、SQLite、运行目录或历史数据。

如启用 `deployment/qq_publish.py`，只为指定群设置独立受限 SSH 发布目标；该发布器仅同步该群已核验的简报/回顾和所需哈希图片 assets，最小化必要元数据，不上传 sources 快照、OneBot/model 配置、token 或其他群文件。远端目录仍应通过上述群账户与 socket 隔离提供。

受限 SSH 发布配置模板见 [`examples/qq-publish.example.json`](../examples/qq-publish.example.json)。填写 `group_id`、单群 `reports_dir`、独立 `state_dir`、远端 `host`/`user`/`port`、仅此群可用的 `identity_file` 和固定 `known_hosts`。密钥、known_hosts、配置文件不可提交；私钥文件权限设为 `0600`，SSH host key 必须由管理员经独立可信途径核验并预装，禁止关闭 `StrictHostKeyChecking`。远端账户应被限制在该群的静态网站目录，仅允许更新文件，不可访问 MSC 或其他群目录。

发布前验证配置与目标：

```sh
python3 deployment/qq_publish.py --config /private/path/qq-publish.json
```

发布器只收集同一 `qq:<group_id>` 的完整简报/回顾配对（两版窗口一致且消息数大于零），传输 HTML/PNG、精简 JSON 元数据和文件名为内容 SHA-256 的 assets；不传 sources 快照、完整消息、token、模型配置或其他群文件。它用已固定 known_hosts 的独立 SSH identity 和 rsync 上传；本地状态 hash 相同则不重复上传。返回 `no_complete_reports` 表示没有符合条件的双版报告，不是发布成功。此入口具有外部写入效果，须由部署者在启用前核准确切单群远端目录和 SSH 身份。

发布器配置中的文件路径按进程当前工作目录解释；建议使用绝对路径或在固定私有工作目录启动，并以 `0600` 保存配置。SSH identity 文件、known_hosts 和状态目录都应仅当前群的发布账号可访问。

## 安全、备份与故障处置

- OneBot token、模型 API key、站点密码和 QQ 历史均是秘密；运行和备份目录限制访问，备份加密。禁止在公开 issue 附 token、群成员/消息、报告、数据库、截图或带凭据 URL。
- 不记录完整授权头、消息原文或模型凭据。反代和应用日志应禁用认证信息与 query secrets；导出/发布前核对只包含预期报告。
- API 认证失败：检查 NapCat OneBot WebSocket URL、服务可达性、token 文件权限/内容和机器人在线状态；不要关闭鉴权暴露端口。
- 历史分页不完整或 OneBot 断连：保持任务失败/可重试状态，先恢复 API 和数据完整性，再观察 worker；不得拿部分历史冒充完整窗口。
- 发送状态未知：核对目标群里是否已有该版报告及 OneBot 服务端回执；不要删数据库、回执或任务以强制重发。
- 模型错误：检查模型配置/额度与 API 错误类别。模型请求涉及群聊天隐私并可能计费；仅向已获准的供应商发送所需数据。
- 网站不可用：检查服务进程、socket 属主/权限、Caddy 到 socket 的路由与认证；不应临时开放无认证公网端口。
- 配置修改后逐项审阅目标群、账号、自身 ID、数据目录、报告目录及每群独立认证，再重启服务。

## 发布验证清单

发行前（不在此说明中代替父代理的统一验证）：检查 JSON 模板无秘密和真实群/账号；审阅 QQ 数据根目录与微信及 MSC 的分离；确认网站不依赖第三方 Web 框架；检查 NapCat/OneBot v11 前提、Caddy socket 权限和认证；在授权测试群先做 24 小时无发送试跑并人工检查报告；再测试一次真实提及、默认/自定 7 天边界、取消、空报告拒绝、永久失败及已送达防重；确认失败和未知状态均不会造成重复投递；离线生成 QQ 虚构样本并打开 HTML/PNG；完成后才按项目的发布流程发布。测试/实际发送可能暴露消息并产生模型费用，必须由部署者明确启用。
