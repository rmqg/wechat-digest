# 运行维护

配置字段约束见 `CONFIGURATION.md`；初始安装与账号采集见 `DEPLOYMENT.md`；错误诊断见 `TROUBLESHOOTING.md`。



## 每日自动任务与手动触发

`deployment/daily-job.py` 每分钟调用一次；到达 `project.json` 配置的 `timezone` 和 `daily_hour` 后处理该配置窗口。OpenWrt 安装器 `scripts/install-openwrt.sh` 将以下分钟级 cron 行幂等追加到 root crontab：

```cron
* * * * * cd /opt/wechat-digest && /usr/bin/python3 deployment/daily-job.py >> runtime/digest/daily-job.log 2>&1
```

该行假设项目位于 `/opt/wechat-digest`、Python 位于 `/usr/bin/python3`。每分钟 tick 使用互斥锁，已有 tick 时新 tick 输出 `running` 并退出。日志可能含敏感诊断信息，需限制权限并轮替。若设置 `WECHAT_DIGEST_STORAGE_MOUNT`，须将其传入 cron 与按需服务的实际环境；shell 临时 `export` 不会自动持久化为开机任务环境。

初次安装（先 dry-run，再应用）：

```sh
cd /opt/wechat-digest
sudo scripts/install-openwrt.sh --dry-run
sudo scripts/install-openwrt.sh --apply
```

`--root ABSOLUTE_STAGING_FS_ROOT` 指系统文件树 staging 根（默认 `/`），不是项目源码目录；用于测试安装布局时可设 staging 根。安装器默认 dry-run，只有 `--apply` 才复制三个 initd 服务并幂等添加上述 cron 行；拒绝符号链接目标，不会启动服务。确认输出的服务文件、crontab 路径和目标 root 正确后再 apply。不要在 staging root 内写入真实生产目录数据。

手动生成自定义时段摘要，`--hours` 必须为正数，默认 24 小时，默认结束时刻为当前时间：

```sh
cd /opt/wechat-digest
/usr/bin/python3 deployment/run-digest.py --hours 24
```

每日 bundle 可用 `--prepare-only` 只生成/校验、不发送；若需手动预演，先停正式 cron，运行一次：

```sh
cd /opt/wechat-digest
/usr/bin/python3 deployment/daily-job.py --prepare-only
```

它用 Asia/Shanghai 当天窗口生成两版，确认后恢复 cron。每日正式流程 brief 成功后才尝试 review，两 edition 的状态、PNG hash 与收据独立。请勿直接改写 scheduler JSON 或收据来强制重发。

固定结束时刻可增加 `--end EPOCH`。`--list-groups` 可列出当前数据源群组而不请求模型：

```sh
/usr/bin/python3 deployment/run-digest.py --list-groups
```

将输出与 `project.json` 中明确配置的群组 ID/name 人工比对，不按列表顺序选择。成功时 stdout 最后一行是 JSON 结果，包含 brief/review 文件；非零退出或 `images_pending` 不是完整成功，应先处理图片/错误。按需触发由 `wechat-summary-commands` 服务接收；不要手工伪造请求 ID，`--request-id` 仅用于服务生成的 64 位小写 SHA-256 ID，并要求明确 start/end 且 start < end。

## brief/review、重复发送与状态恢复

每日生成有 brief 与 review 两个 edition。调度器用 `runtime/digest/delivery-state.json`、文件锁及 `sent-receipts/` 收据持久化状态。运行时同一时刻只允许一个 tick 持有锁；输出 `running` 表示另一个 tick 正在执行，不应再并行启动。

发送过程被中断、状态为 `unknown`、发现缺失收据，或已标记发送但收据不在时，系统禁止自动重试，以免重复发送。人工恢复：停止调度，保存状态/收据/日志供本地排查；通过微信目标群的实际消息与服务端收据确认该版是否送达。若无法确定，保持 unknown，不要删除状态或收据来重发。只有确认未送达且理解状态机语义后，按运维流程恢复；确认已送达则保留其收据。brief 和 review 分别跟踪，切勿因一版成功就假定另一版也成功。

## 服务启停

OpenWrt/iStoreOS 模板服务名固定为 `wechat-image-origin`（私有 assets origin）、`wechat-summary-commands`（按需命令扫描）和 `wechat-images`（实时图片工作）。先执行安装器 dry-run 并 `--apply` 安装脚本；只有配置、desktop、pins 和 runtime 均已准备后再启用：

```sh
/etc/init.d/wechat-image-origin enable && /etc/init.d/wechat-image-origin start
/etc/init.d/wechat-summary-commands enable && /etc/init.d/wechat-summary-commands start
/etc/init.d/wechat-images enable && /etc/init.d/wechat-images start
```

检查状态用 `/etc/init.d/SERVICE status`（将 SERVICE 替换为以上确切名称）；重启用 `restart`。停服务用 `stop`；取消开机自启用 `disable`，两者须分别执行。image-origin 需有效 origin JSON，summary-commands 需完整 trigger JSON、desk session/key/selection/model，wechat-images 需所用模型/原图与OCR依赖可用。安装器只复制 service 与 cron 配置，不会自动启动/启用。升级、故障恢复前停服务及日报 cron；确认安全状态后再 enable/start。

## 图片与实时任务运行

图片 worker 与原图下载使用 `runtime/image-pins.json` 的 runtime pin 和 `runtime/project.json`；每次任务按 `group.id/name` 生成内容，避免混入其他群组。确认 `selection.json` 由本机已确认账号生成，并且整个 `runtime/gui/` 可只读挂载到容器 `/config`。原图请求依赖 `/config/...` 下源 DB 及同一 GUI storage 的 MMKV、CDN 配置和媒体兄弟文件，不可裁切到单独 database 子目录。

日报和 `wechat-images` 实时服务均按项目 `timezone`/`daily_hour` 计算各自窗口。cron 每分钟唤起 `daily-job.py`，到达配置时刻才生成并递送窗口；实时图片服务根据同一配置使用自身窗口。配置更改后检查生成窗口边界，不要假设已有任务的状态/收据会迁移。

图片处理结果位于 `runtime/digest/image-jobs/`，包括 manifest、原图请求/下载状态、vision/OCR 输出与失败账本。确认任务已经终态并留存所需审计证据后，才清理可重建 cache。OCR 依赖的 `runtime/ocr-deps/` 是可选挂载，只在目录存在时挂载；如果要求 OCR 但依赖未准备，检查运行结果的 failed 状态并安装锁文件指定的目标架构 wheel。`runtime/python-deps/` 不是图片 worker 的强制挂载。

## 发布器、静态站点与图片服务

静态发布配置 `runtime/report-site.json` 必须含 `host`、受限 `user`、`port`、`identity_file`、`known_hosts`、相对 `remote_path`。为该 user 单独建 SSH key，并在远端限制写入单个网站目录；通过严格 host-key pinning 检查身份。发布入口：

```sh
cd /opt/wechat-digest
/usr/bin/python3 deployment/publish_reports.py
```

`published`/`unchanged` 为正常结果，`busy` 表示锁占用；无配置文件时发布停用。只发布验证过的 `index.html`、reports HTML 和资产白名单，不要添加 rsync `--delete` 或绕过清单。

图片源的 `runtime/image-origin.json` 为私密配置，`assets_dir`、随机 `token` 必需，`port` 默认8099且 `bind` 仅允许127.0.0.1。先跑 `deployment/image-origin.py --config runtime/image-origin.json --check-config`，再启动 `wechat-image-origin`。服务只允许 loopback peer + Bearer token 的 GET/HEAD hash asset 请求。

对外静态站点应要求认证并让报告 HTML 与图片同域，Caddy snippet 经受控 `image-origin-auth.caddy` 认证后将 hash 图片反代到 origin。Cloudflare Tunnel route 的本地服务目标为 `http://127.0.0.1:8099`；origin 与 cloudflared 必须共享兼容 network namespace；不要公网 bind 8099。排查 1033 看 connector readiness，而不是面板/Analytics 单一 metrics。TCP 7844 与 UDP transport 分开判断；排障中不要全局重启网络。直接组65534方案只在 operator 已验证原先存在的 bypass rule 时才适用，不是通用要求。


## 缓存、存储与字体

`runtime/digest/` 包含采集/图片处理中间物、锁、递送状态和收据。不要清空整个目录：这会损坏 no-resend 证据。只清理已确认不再被任务引用的缓存子目录，清理前停调度并备份状态/收据。`runtime/reports/` 是发布输入及报告档案；制定保留期前确认发布站点依赖哪些文件，并确保无软链接或越界路径。清理前按业务策略导出需要保留的报告。

若宿主已配置真实持久数据卷，且要启用挂载保护，在 daily cron 与 on-demand 服务的运行环境设置 `WECHAT_DIGEST_STORAGE_MOUNT` 为那个 mount point。未设置/空值表示关闭该 guard；设置后必须 `is_mount()` 为真，普通目录不够。daily 发现未挂载时输出 storage-stage failure 并退出非零，on-demand 拒绝处理。挂载先由主机存储管理配置完成；例如单次人工诊断可 `export WECHAT_DIGEST_STORAGE_MOUNT=/mnt/digest-data` 后运行检查入口，但要让 cron/服务重启后仍生效，须用目标固件支持的持久环境配置方式。切勿因目录存在就假设卷已挂载。

报告字体放在 `runtime/report-fonts/` 并需合法授权。使用可读字体文件并确保摘要容器挂载可访问；缺字体时先修复字体挂载，不要通过压缩字号或静默替换来掩盖问题。

