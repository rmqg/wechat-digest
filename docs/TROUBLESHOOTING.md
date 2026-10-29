# 故障排查

先停止可能造成重复影响的动作；记录状态字段/错误阶段和时间，但不要复制 token、密钥、群聊消息、真实群 ID 或完整私有配置到工单。以下建议不包含网络全局重启、自动重试或强制清理状态。

安装/容器启动：先分清主机依赖、Compose/service 和 runtime 数据问题。`docker compose ... build runtime` 失败时检查 Docker daemon、源码根构建上下文、requirements 与目标架构；勿用 `privileged` 或挂宿主根目录绕过。desktop 启动失败时确认 `.env` 存在并设置 `DESKTOP_PASSWORD`、端口未占用、`runtime/gui` 可写及 desktop image 与配置匹配。Python 模块缺失先核对 runtime 镜像/代码和实际需要的依赖：`runtime/python-deps` 不是图片 worker 的强制挂载，OCR 依赖只有存在 `runtime/ocr-deps/` 时单独挂载；不要在容器内临时无锁 pip install。

Bootstrap 配置失败：`--init-runtime` 拒绝覆盖已存在 project.json；先手工备份后编辑，不要删除 runtime 整体重做。`--pin-images` 对 runtime/keyscan 相同 ID 和运行中 desktop ID 有强制校验；如果不一致，停止 desktop、核对 `.env` 和 `project.json` tag、再按计划重建/启动正确镜像，最后才显式 pin。只有有意升级才 `--refresh-pins`。

日报 cron 不执行：确认 installer 已 `--apply`，检查 `/etc/crontabs/root` 有安装器分钟任务、项目路径为 `/opt/wechat-digest`、Python 为 `/usr/bin/python3`，且 root 可访问 Docker daemon。查看权限受控的 `runtime/digest/daily-job.log`；`waiting` 表示当前配置的 `timezone`/`daily_hour` 门槛尚未到，`running` 表示互斥锁被另一 tick 持有。不要并行调用 `--force`。

dry-run sender 报输入错误：报告 PNG 必须在 `runtime/reports/` 且有同 basename 的 `.html` 和 `.json`；dry-run 不依赖校准/GUI。真实 sender 需要 calibration、GUI、key、selection 和 runtime image pin；如果预提交前失败可修复后重测 dry-run。已经点击发送或提交状态不明时，不重复调用 `--send`。

| 症状 | 检查与处置 |
|---|---|
| `prepare-collection.py` 的默认检查失败 | 该脚本没有 `--check` 参数；不写 `--capture` 才是检查模式。确认 `runtime/gui/` 存在、desktop 容器正在运行且与 `images.desktop` pin 的 image ID 匹配、`project.json` 有 `images.runtime/keyscan/desktop`，Docker daemon 可访问。修复检查报告后再运行一次默认检查；只有确认账号身份与目录并获得授权后才加 `--capture`。 |
| capture 卡住/超时/找不到密钥 | Capture 会进入桌面客户端进程并等待操作者手动重新登录；确认桌面当前确为已授权目标账号，采集时按提示完成登录动作。检查 keyscan/runtime 镜像 ID 相同、桌面进程可达以及本地数据库目录只读可见。超时/无 HMAC 验证 key 时停止并重新检查，不能尝试别的账号或泄漏密钥。 |
| `run-digest.py` 找不到本地镜像 | 确认 `project.json` 的 `images.runtime` 指向本机镜像，且镜像架构匹配宿主；运行器使用 `--pull never`，不会自动拉取。升级后核对引用解析到的镜像 ID，不要仅凭 tag 名称判断版本。 |
| 容器权限错误或无法写入 | 检查 `worker_uid`/`worker_gid` 与 `runtime/digest`、`runtime/reports` 的目录属主/权限；保持源数据库/key文件只读，避免改用 root、特权容器或放宽整个 runtime 权限。 |
| 模型请求失败 | 确认 `runtime/gemini-model.json` 的端点可由配置的容器网络访问、模型名和 key 有效；检查凭证文件权限。错误报告须脱敏；不要打印 JSON 或测试真实用户数据。 |
| 图片任务停留 `images_pending` | 查看任务 manifest、原图获取请求/失败记录、视觉与 OCR 状态，确认 `runtime/image-pins.json` 仍匹配已配置 image ref，任务配置了正确 `group.id/name`。原图下载需完整只读绑定 `runtime/gui` 到容器 `/config`；核对 selected path 是 `/config/` 下的受支持布局，并保持同目录 MMKV/CDN/媒体兄弟文件可读。检查磁盘和 cache；不要删进行中的 job 或手工标为成功。 |
| `daily-job.py` 返回 `waiting` | 当前配置的 `timezone` 和 `daily_hour` 门槛尚未到；检查项目配置与宿主定时任务。分钟 tick 到门槛后处理窗口。`--force` 会绕过时间门槛，仅用于受控验证；`--prepare-only` 不发送。 |
| `unknown` / interrupted while sending | 视为发送结果不确定；系统禁止自动重试。停止调度，检查本地收据与目标会话中的对应 edition；不确定就维持 unknown。不要删除 `delivery-state.json`、收据或锁文件来强制发送。 |
| brief 已发、review 未发 | 两个 edition 独立状态。确认 brief 收据保留，单独诊断 review；不要将已发 brief 作为重发 review 的依据或删除 brief 证据。 |
| 报告恢复失败/完整性错误 | 确认 report bundle 文件在 `runtime/reports/` 内且 PNG 哈希与状态一致；检查磁盘、备份及权限。不要手工改 hash/edition/window 以通过校验。 |
| 站点发布失败 | 检查 `runtime/report-site.json` 必填字段、私钥与 known_hosts 文件、远端受限账户目录和网络可达性；确保 `remote_path` 是允许的相对路径。不得将私钥或 SSH 配置粘贴到日志。 |
| 图片源返回 403 | 服务仅接受 loopback 来源与正确 Bearer token；确认请求从预期本机代理/Tunnel 来，且 token 注入正确。不要公开 token。 |
| 图片源返回 404 | URL 必须为 `/assets/<64位十六进制哈希>.<允许扩展名>`，对应资产文件须存在于 `assets_dir`。避免软链接；确认静态 HTML、发布资产与本地源使用一致文件版本。 |
| Cloudflare Tunnel 报 1033 | 1033 表示 Tunnel 没有可用连接/connector readiness；先查看本地 tunnel connector 是否在线、目标服务是否监听 `127.0.0.1:8099`、配置路由是否指向正确服务。不要依赖 Cloudflare Analytics/指标显示就绪：监控指标可能滞后或误导。 |
| Tunnel 看似在线但 origin 不通 | 检查 `deployment/image-origin.py --check-config` 能否成功、进程状态/日志与 loopback 8099 监听。容器的 loopback 与宿主 loopback 不同；核实 tunnel 客户端和 origin 的 network namespace。不要把 origin 改为 `0.0.0.0` 来掩盖 namespace 配置问题。 |
| TCP tunnel 无法建连 | 某些网络路径会阻断 Cloudflare Tunnel 的 TCP 7844；区分 TCP 与 UDP 传输方式，按客户端实际 transport 检查相应出口规则。不要假设开放 TCP 即代表 UDP 可用，也不要仅凭面板 metrics 判断数据面成功。 |
| Caddy 图片域名/认证失败 | 检查 Caddy 占位符是否换成实际域名、站点是否与报告 HTML 同源、访问认证是否覆盖图片路径；检查静态站点的认证会话与图片代理策略。不要把绕过认证的公开图片源当作诊断方案。 |
| 服务启动后退出 | 使用系统 init 服务状态/日志检查脚本路径、Python、JSON 配置和文件权限。image-origin initd 启动前会执行配置检查；修复检查错误再启动。确保根目录环境变量与部署位置一致。 |
| 磁盘空间不足或报告缺字形 | 检查 runtime 所在挂载是否真实挂载、剩余空间/inode及字体目录是否可读；按《运行维护》区分缓存、递送状态/收据和报告档案，勿清空整个 digest 状态目录。 |
| `stage=storage` / 按需服务拒绝执行 | 仅当 `WECHAT_DIGEST_STORAGE_MOUNT` 设置为非空值时启用保护；检查变量实际传给 cron/服务的值，以及该路径是否当前确为 mount point（`Path.is_mount()`），普通目录不算。先由宿主存储管理流程挂载目标卷，再恢复任务；若不需要挂载保护，按策略取消变量。不要仅创建目录后重试，也不要把环境变量放进公开日志。 |

若已确认环境里存在针对原始用户/组 `65534` 的直连绕过规则，检查该规则是否仍按预期生效；只在已验证的该类旧环境中考虑直连组设置。此规则不是本项目统一前提，禁止为解决一般 Tunnel/权限故障而添加全局网络例外。

任何故障如涉及真实群聊、未知递送结果或无法验证的账号选择，应停止自动化并由操作者人工确认。保留最小脱敏诊断信息与必要状态副本，修复后按受控流程恢复，不做全局网络重启或盲目重试。
