# 配置参考

安装、运行及排障手册：`DEPLOYMENT.md`、`OPERATIONS.md` 和 `TROUBLESHOOTING.md`。

所有路径相对于仓库根（除非设置环境变量 `WECHAT_DIGEST_ROOT`）。live 操作不允许缺省真实目标：群组 ID 与名称必须由管理员明确配置，不会以虚构群组兜底。`examples/project.example.json` 是含替换值的示例，不可直接用于发送。


## `runtime/project.json`

bootstrap 初始化/pinning/capture 和 `--list-groups` 不依赖 `group.id/name`，但 live 摘要、媒体准备及 web context 必须以人工确认的 `group.id`/`group.name` 为准；不可使用空值猜目标。Sender 还要求 `bot.username` 非空且非占位符。`bot.names` 默认 `[]`，若提供必须是非空字符串数组；trigger service 自身要求至少一个 bot name。图片 manifest、原图 fetch request 与数据库消息上下文都按配置群组过滤，避免使用其他群组记录。

字段逐项：`report_prefix` 在示例为 `wechat-digest`；`desktop_container` 示例 `wechat-digest-desktop`，均由运行器直接读取，请保留。`public_url`、`image_origin_host` 默认 null；`daily_hour=5`、`timezone="Asia/Shanghai"`、`worker_uid=1000`、`worker_gid=1000`。日报与实时图片窗口均使用配置的 timezone 和 daily_hour；若环境需要保护数据盘，可选设置 `WECHAT_DIGEST_STORAGE_MOUNT` 环境变量，详见下方“存储挂载保护”。

报告日期、金句时间、峰值标签，以及 `--start`/`--end` 的无时区 ISO 时间也按项目 `timezone` 解释；显式带偏移的 ISO 时间保留其绝对时间。已生成报告可带自己的 `timezone` 元数据供离线渲染使用；合成样本明确采用 UTC，不依赖运行机器的本地时区。

`images.runtime`、`images.keyscan` 默认 `wechat-digest-runtime:1.0.0`；`images.desktop` 默认 `ghcr.io/nickrunning/wechat-selkies:0.0.12-minimal`。值是本机 image references，需与已安装并运行的镜像对应。runtime/keyscan 必须解析为相同 image ID；desktop 的 ID 要与配置容器的当前 image ID 相同。bootstrap 记录不可变 ID 到 `runtime/image-pins.json`，每次执行校验 config 引用与 pin 一致；不要把标签本身当成内容摘要。示例模板 `examples/project.example.json` 填入这些键，真实群账号标识只存本机，不可发布。
`runtime/digest/delivery-state.json` 与 on-demand job/receipt、`runtime/digest/sent-receipts/*.json`、`on-demand-receipts/*.json` 是程序拥有的 durable state，不是待手改的设置；记录 edition、window、状态、PNG SHA256/服务端 receipt，用于禁止重复发送。备份/恢复必须原子一致，缺 receipt、发送中断或结果不确定时状态机将进入 `unknown` 并拒绝自动重发。不要删/重置来清理故障。

下面只列出仓库代码实际读取的配置字段；runtime 状态、缓存、收据、报告 JSON 是程序生成数据，不是手工配置。无明确默认值者必须由运维者创建；不要靠放宽校验修复缺项。除非字段明确标为可选，缺少必需项、类型不对或不在允许范围时都应先修配置。所有含 token、密码、API key、个人账号/群标识或登录会话的文件均应以 `0600` 或更严格权限存储，禁止提交。

### 可选 `WECHAT_DIGEST_STORAGE_MOUNT` 宿主环境变量

默认不设置（或为空）时不做数据卷挂载检查。设置为一个路径后，daily job 与按需服务均要求 `Path(value).is_mount()` 为真：目标必须是宿主机当前真实挂载点，而非仅存在的普通目录；若检查失败，daily job 在递送前输出 `status=failed`、`stage=storage` 并退出非零，按需服务也拒绝继续处理。挂载应先由宿主既有、受信任的存储管理流程建立，再设置该变量并启动服务；不要将 runtime 空目录当作已挂载数据盘。此变量不参与 Docker 容器内路径解析，仅供宿主 scheduler/服务 guard 使用。


### `runtime/gemini-model.json` 与 `runtime/vision-qwen.json`

`runtime/gemini-model.json` 是摘要必需配置，必须是 object，`base_url`、`model`、`api_key` 都是非空字符串；客户端会以 base_url 追加 `/chat/completions` 调用 OpenAI-compatible API。它也是视觉主模型配置。`runtime/vision-qwen.json` 是可选备用视觉端点；缺文件时不配置 fallback，如提供则请求时三个键均必须为非空字符串。模型 endpoint 和 key 都是秘密，文件设为 0600、勿放 URL query、日志或仓库。

### `runtime/summary-trigger.json`

仅启用按需摘要命令服务时创建，必需 object 字段：`chat_id` 必须是你所授权目标群 ID；`bot_username` 非空账号 ID；`bot_names` 非空的非空字符串数组；`enabled_from` 为非负 Unix 秒整数（布尔值不是合法整数）。Scanner 以配置用户名排除机器人消息，并只接受配置名称的 @ 提及加总结类文字。不得把真实账号/群 ID 写入公开样例。模型时长解释由独立调用的 `gemini-model.json` 提供；默认请求时长 86400 秒，触发时刻为窗口结束，解析出的 duration 必须是正数。不能通过缩短用户指定时长来规避处理。

### `runtime/selection.json` 和 `runtime/image-pins.json`

`selection.json` 由成功的 bootstrap 采集流程生成，至少包含 `source_db_dir`（容器路径，以 `/config/` 开头，对应 `runtime/gui/` 下已验证账号 DB）与 `wechat_base_dir`；不要手工改写或选择陌生账号。模式为仅服务用户可读。

`image-pins.json` 由 `deployment/bootstrap.py --pin-images` 创建，schema：`{"schema_version":1,"pinned_at":UNIX_SECONDS,"images":{"runtime":{"reference":"本地引用","id":"sha256:..."},"keyscan":{"reference":"本地引用","id":"sha256:..."},"desktop":{"reference":"本地引用","id":"sha256:..."}}}`。bootstrap 对照配置引用、本机解析出的 SHA256 Docker image ID 及 desktop 容器实际 image ID；不手工编辑。只有计划内镜像升级才运行 `--refresh-pins`。runtime/keyscan 的 pinned ID 必须一致。该文件不是 secret，但影响是否执行代码，保持运维者可写。


### `runtime/ui-calibration.json` 与 `runtime/ui-calibration/`

手工发送需要已校准 GUI 参数及图像模板目录。JSON 必须包含 `version: 1`、`display: ":99"`、`window` 固定几何 `{"x":181,"y":117,"width":917,"height":667}`，以及发送实现读取的坐标：`search_box`、`search_result`、`header_region`、`message_input`、`send_button`（数组坐标/区域类型须符合代码）。还需 `header_template` 指向 calibration 资产目录中的文件；可选项有 `window_title_substring` 默认 Weixin、`allowed_visible_window_titles` 默认空数组、`header_pixel_threshold` 默认 18、`header_max_bad_fraction` 默认 0.001、`send_settle_seconds` 默认 1.5、`confirmation_timeout_seconds` 默认 25。坐标与模板必须由本机实际 GUI 校准并人工复核，禁止使用别人的截图/坐标或公开包含真实 UI 的资产。错误校准会造成发错目标，先用无发送模式验证。

### `runtime/report-site.json`

可选静态站点发布器配置；缺文件时发布器不进行远端发布。配置为 JSON object，以下字段全必需：`host`（合法 DNS/IP/IPv6 host 字符串）、`user`（受限远端 SSH 用户）、`port`（整数 1–65535）、`identity_file`（存在的私钥常规文件路径）、`known_hosts`（存在的 host-key 文件路径）、`remote_path`（安全相对路径；不可绝对路径、含 `..`、双斜线或非法字符）。相对 key 路径以 config 文件目录为基准。无默认值；连接强制 BatchMode、严格 host-key checking、只使用指定身份/known_hosts。远端用户须限制写权限到单一站点目录。

### `runtime/image-origin.json`

图片源必需 object 字段：`assets_dir` 非空目录路径（相对路径相对配置文件目录/项目根解析）；`token` 非空秘密字符串。可选 `port` 默认 8099，必须整数 1–65535；`bind` 默认 `127.0.0.1`，只允许该 loopback 地址。`--check-config` 还确认 assets_dir 是目录。接口仅允许 loopback peer 加 Bearer token，GET/HEAD hash asset path。文件严格保密。

### `runtime/report-site-access.json`

可选站点访问凭证通知配置；缺文件时通知关闭。object 中 `chat_id`、`url` 必须与发布器配置的目标值完全一致；`username` 与 `password` 必须为非空字符串，`enabled_from` 是非负 Unix 秒整数。凭证可能形成含用户名/密码的访问 URL，应仅存于 `0600` 私有文件，绝不日志打印或提交；若不需要消息通知，不要创建该文件。

### `runtime/ui-calibration.json` 的发送先决条件与执行模式

`deployment/send-report.py REPORT.png --chat-id TARGET_ID` 默认只校验 PNG 位于 `runtime/reports/` 且配套 HTML/JSON 存在，输出 `status: prepared`，不执行 GUI 操作。真实发送必须显式 `--send`；发送前人工确认群、账号、校准 UI 和 PNG hash。发送若进入未知状态则绝不可重发。桌面配置/校准与发送运行时依赖缺失会拒绝执行。

### OCR 依赖文件

OCR 不读取用户 JSON 配置。依赖由 `deployment/ocr-requirements.txt` 与 `ocr-wheel-lock.json` 锁定并挂载 `runtime/ocr-deps/`；需使用与目标架构匹配的锁定 wheels。OCR CLI 接受 `--manifest`、`--output`（必需）、`--cache-dir`（默认 `/work/ocr-cache`）、`--max-images`（默认 0，不限；非负）。只识别可用且 `source_quality` 为 `original` 或 `client_full`、hash 匹配的图片；thumbnail 不纳入。缓存 JSON schema 为 `schema=1`、image SHA256、engine=`rapidocr-onnxruntime`、engine_version=`1.4.4`、text、lines、confidence、low_confidence；输出按 message uid 映射状态，不是人工配置。