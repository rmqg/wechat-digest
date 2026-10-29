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
