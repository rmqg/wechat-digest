# 文档与发行校验

## 校验目标

“命令能复制”至少包含三层：文档自身的路径/链接/JSON语法一致；CLI 实际接受写下的参数；在有授权环境中完成真实动作。前两层可自动化且必须在发布前执行；第三层涉及本人微信登录、手机确认、模型费用或发送，不能由一个静态检查器冒充。

## 1. 本地文档与 CLI 检查

```sh
python scripts/check-docs.py
python scripts/check-docs.py --cli
```

基础模式检查仓库 Markdown 的本地链接、标题锚点、围栏闭合和配置示例 JSON。`--cli` 在没有 live 项目配置的临时环境中仅运行解析器的 `--help`，要求在期限内正常返回 usage；不会执行采集、模型、GUI或发送。它不执行文档里任意 shell 代码，也不把 `sudo`/网络动作伪装成校验。

如果报告缺链接，修正文档或补齐真实文件；不要删掉检查来让文档“通过”。如果 CLI 在 `--help` 前就附加进程、发网络请求或要求群 ID，应修复入口的副作用顺序。

## 2. 配置与安装模板

先按部署手册准备测试 `.env`，再执行：

```sh
docker compose config --quiet
sh -n scripts/install-openwrt.sh
sh scripts/install-openwrt.sh --dry-run
```

安装器可在临时文件系统根做应用验证：

```sh
mkdir -p /tmp/wechat-openwrt-root/etc/crontabs
printf '%s\n' '# unrelated test cron entry' > /tmp/wechat-openwrt-root/etc/crontabs/root
sh scripts/install-openwrt.sh --root /tmp/wechat-openwrt-root --apply
sh scripts/install-openwrt.sh --root /tmp/wechat-openwrt-root --apply
```

核对无关 cron 行保留，第二次不重复添加任务；这些动作仅写临时根，不启动服务。真实 `/etc` 的安装只能由运维者明确指定 `--apply`，且先检查实际路径、数据权限和后台服务状态。

## 3. 解密与媒体

按 [DATABASE](DATABASE.md) 生成的公知测试库执行认证解密，查询 `synthetic_messages` 行。使用错误 key 或翻转加密体字节时必须失败且不破坏已有有效输出。WAL 测试覆盖提交/未提交/损坏帧以及最后一次提交导致文件缩小的边界。原库字节哈希应始终不变。

## 4. 真实渲染烟测

```sh
python scripts/generate-demo.py --output /tmp/wechat-digest-demo
```

查看 HTML/PNG，不以“文件非空”代替布局验证。核对中文可读、统计范围、十分钟空桶、引用作者与原文、灰框缩略图及原图链接、简报/回顾差异、正文汉字数排除来源/目录/排行。手机宽度检查真实元素尺寸与页面是否溢出。

## 5. 隐私审计

```sh
python scripts/audit-release.py
```

检查器只输出文件路径、行号和类别，不回显匹配值。额外私有否定清单必须放在仓库外：

```sh
python scripts/audit-release.py --private-patterns /path/outside/repository/private-patterns.json
```

该 JSON 是“类别 → 正则表达式”。用于检查你自己的旧域名、昵称、绝对家目录等，不要为了写一个否定规则把真实私密值再提交进公开检查脚本。加密测试库只有精确的合成 fixture 路径获准；真实数据库、登录态、私钥、抓包和运行目录不可进入 Git 索引。

还必须使用一个成熟的密钥扫描器，例如已验证来源的 Gitleaks，并启用脱敏：

```sh
gitleaks dir . --redact --no-banner
gitleaks git . --redact --no-banner
```

测试向量命中应逐项确认并作最窄的规则例外；不能对整个 `examples/` 或整个测试树关闭密钥检查。检查器不能证明图片没有私人内容，因此仅收录从零生成、可复现的合成资产，拒绝原聊天截图。

## 6. 历史与发布后复验

只推经过审计的分支。若旧历史包含个人资料或无许可源，将其留在私有备份，创建干净公开历史；不要 `git push --all` 或上传旧缓存包。公开后在新的临时目录 clone，重跑测试/文档检查/合成生成，检查 GitHub 可见性、默认分支、许可证识别、文件树和 README 链接。记录实际验证平台及哪些客户端捕获路径尚未做真机授权验证。

## 7. 首次公开发行的实际验证范围

- macOS arm64：执行离线回归、27 个 CLI 帮助入口、本地文档链接/JSON 检查、安装器语法和 dry-run。
- Linux arm64 Docker：实际构建工具镜像；在 UID 1000、断网、只读根、移除 capabilities 的容器中认证解密公知样本，查询到两行 `synthetic_messages`，核实原文件 SHA256 不变并实际渲染 PNG。
- Chromium：打开生成的 HTML，展开来源，核实合成图加载、回复灰框、UTC 时间和峰值；桌面与 390px 窄屏无横向溢出。另直接查看完整中文 PNG 与计数/时间刻度。
- 发布前使用外置私有否定清单和脱敏 Gitleaks 检查公开文件；公开历史从本发行目录重新创建，不带此前私有运行树。

这些证据不等同于重新登录任何真实微信、调用付费模型或真实投递。此次公开包未重新做 Windows/macOS 密钥捕获真机测试，也未把 Linux arm64 工具镜像烟测写成微信桌面镜像支持 ARM 的保证。客户端版本与权限边界仍以数据库和部署手册为准。

## 8. 1.1.0 QQ 与隔离验收

- 本地公开工程全套回归覆盖 QQ 协议/历史游标、空窗口、七天边界、逐版回执、并发实例锁、只读状态、HTTP/Unix socket 权限以及微信既有流程；平台专用的三项 Linux GDB 隔离捕获测试仍需显式环境，不能将其跳过说成已做真机捕获。
- 已在一个获授权 QQ 群完成真实过去24小时生成与两版图片投递：278 条可读消息、52 张图片，OneBot 回执按群号/发送者/get_msg 验证。真实记录、群身份、密码、报告截图和回执不作为公开样例。
- 新网站真实 HTTPS 验证了新账号只见对应 QQ 群、旧账号仍见原网站、未登录拒绝、双向跨群路径拒绝；桌面及390px真实浏览器检查了正文、来源图和导航。账号密码只保存在站点私有配置中，不复用、不进入发布包。
- `scripts/generate-demo.py --qq` 提供完全虚构的离线展示烟测。CI 另构建带校验OCR层的 Linux x86-64/Python3.12 工具镜像，并执行实际数字OCR识别；以该提交的 GitHub Actions 结果为准，不把纯文本CLI帮助当成OCR验证。

## 9. 1.1.1 固定产物恢复验收

- 使用两个独立 Python 进程、真实 SQLite 持久目录及实际渲染的双版 PNG 实跑恢复：第一进程在简报确认后模拟回顾提交前故障；第二进程添加迟到记录后，只续发原回顾，不调用生成器。两版 PNG 与源快照 SHA-256 不变。
- 上述烟测使用明确隔离的本地传输记录器，不连接 OneBot、不调用付费模型、不向真实群再发测试消息；它验证跨进程产物恢复，不冒充真实 QQ 投递证据。
- 回归覆盖已确认双版不依赖产物缓存、损坏产物停止、旧版未知提交没有固定产物时停止，以及同窗口不同任务的独立文件名。真实群首次投递及权限隔离证据仍见上一节。
- 本地最终回归：296 项通过、93 个子测试通过；3 项需显式 Linux GDB 环境的测试跳过。文档检查覆盖 33 个 CLI 帮助入口且无错误；发布审计与脱敏完整历史凭据扫描无发现。

## 10. 1.2.0 token ranking, quotes and delivery validation

- Public local checks: 316 tests passed, 12 skipped, and 97 subtests passed. The 12 skips are 3 Linux GDB capture integrations requiring `WECHAT_CAPTURE_INTEGRATION=1` and 9 native X11 window cases unavailable on macOS; all 9 X11 cases separately passed in an isolated NAS runtime with Xvfb, nonroot execution, network disabled, read-only filesystem and no real GUI/account mounts. The public suite also passed as reported by the parent maintainer; this document records that evidence, not an independent rerun.
- Four affected private regression paths passed (82 tests, 20 subtests). These are private verification results and do not imply private source/data is included in this release.
- `scripts/check-docs.py --cli` reported zero errors across 33 CLI help entries; `docker compose config --quiet` succeeded. Synthetic WeChat and QQ CLI generation produced HTML and PNG output. The OCR-enabled public Docker image built successfully using an existing build network without changing host networking; isolated runtime checks confirmed the default 1.2.0 image label, offline cache use and OCR recognition of synthetic digits. The final brief-source overlay image used Python 3.12.16, distinct from the fixed production image.
- A separate fixed production image/Python 3.12.3 cold-start check used pinned `tiktoken==0.14.0` data and a read-only cache as a nonroot process with network disabled: `你好，世界！` counted as 4 tokens and the literal image caption `[图片]` as 3, while image statistics remained unchanged. Production dependencies, model, WeChat and NapCat images were not upgraded; 15 other production containers retained their baseline image and start time.
- In an authorized QQ group, the daily 24-hour digest covered 479 messages, 19 members and 108 original images; all 108 images were understood. Both editions were sent once, with independent `get_msg` verification of recipient group, sender and single-image payload. An independent literal-text recount matched all 19 speakers; the top count was 565. The HTML and PNG displayed no tokenizer explanation. Public documentation omits group/account/message identifiers, credentials, private URLs and report artifacts.
- After service restart and at least two collector/scheduler cycles, the task, receipts, prepared manifest and PNG/HTML/JSON fingerprints and modification times remained unchanged; no regeneration or resend occurred. Isolated SQLite schedule checks verified pre-boundary/at-boundary behavior, duplicate ticks, store reopen and exact 86400-second windows with one task and two receipts. The fixed WeChat wrapper `--list-groups` returned 0 without model access.
- Brief reports now independently choose source-verified quote candidates; review prompt semantics remain unchanged. Original quote text is validated and restored verbatim, empty candidate sets do not invent quotes, and brief HTML/PNG append literal quotes and available activity/token ranking. Visible quotes are included in Han-character counts while ranking labels are not. A production-image preview using verified data showed two literal quotes, 19 members and the top count 565 without clipping; already-sent images were left untouched. Synthetic demos also show exact-source quotes rather than fabricated composite quotations.
- The authorized private site entry and notification usage example were verified in the intended group, including HTTPS response and content/account checks. This public record intentionally excludes the URL, user-info, account, password, group ID and message ID.
- The successful production task state was `complete`. A separate observation script incorrectly expected `done`; that caused a reporting delay only and was not a production processing or delivery failure.
- Final source privacy audit with the external private-pattern list reported zero findings. Redacted Gitleaks 8.30.1 directory and full-history scans each reported zero findings. No private patterns, matching values, runtime caches or production artifacts are included in the public tree.
- The two v1.2.0 push workflow runs ended in failure with no job/check-run records; GitHub did not provide logs (404). Root cause was invalid context use: GitHub documents `runner` as unavailable in `jobs.<job_id>.env`, although it is available in step environments. This workflow-scope defect is corrected in v1.2.1; the v1.2.0 tag/assets remain unchanged and v1.2.1 supersedes that release for use.

## 11. 1.2.1 GitHub Actions workflow correction

- The public workflow now initializes the tokenizer cache in a step environment, exports its path through `GITHUB_ENV`, and uses it from later steps without job-level `runner` context. Official `actionlint` v1.7.12 completed with exit 0; its independently downloaded binary matched the official release metadata SHA-256.
- The extracted prewarm step completed with exit 0; a separate fresh Python process inherited the exported cache path and deterministically counted the sample as 4 tokens. Public version-template contract checks also matched 1.2.1.
- GitHub Actions run `41845207440` for source commit `f936074f57a573ea0f3bd48057ea4d107eb90e4e` completed successfully: Python 3.11 and 3.12 matrix jobs passed, including offline tests, documentation, redacted boundary audit, synthetic rendering, Compose/installer checks, and the Python 3.12 isolated runtime/OCR check. Release-tag CI is checked separately after publication; this is not inferred from local checks.

