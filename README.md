# WeChat Digest

面向 **自己设备、自己账号或明确获授权数据** 的微信本地数据与总结工具链：密钥提取、认证解密、消息/媒体解析、双版总结、安全投递和带原文来源的网页。

项目原创代码使用 [MIT](LICENSE)。随附的三平台密钥工具保留 MIT 版权声明，字体使用 SIL OFL；完整清单见 [第三方声明](THIRD_PARTY_NOTICES.md)。本项目不隶属于腾讯，不提供微信客户端二进制、账号、登录态或真实聊天数据，也不保证任何微信版本永远可用或账号绝无风险。

## 先选你的使用路径

| 目标 | 从哪里开始 | 是否需要微信/网络/模型 |
|---|---|---|
| 先看报告、引用卡片和完整图片效果 | [合成样本](examples/README.md) | 不需要微信、模型或网络 |
| 对自己的库做只读解密/导出 | [数据库手册](docs/DATABASE.md) | 需要自己的库与已验证密钥；提取密钥另需授权进程访问 |
| 在 Linux/iStoreOS 上持续生成总结 | [部署手册](docs/DEPLOYMENT.md) | 需要本人登录客户端、Docker、模型配置 |
| 定时/群内 @ 生成并发送 | [运行维护](docs/OPERATIONS.md) | 先完成前述配置；发送是显式副作用 |
| 远端网站展示、NAS 保留原图 | [配置参考](docs/CONFIGURATION.md)与[运行维护](docs/OPERATIONS.md) | 需要自己的 SSH/网站认证和图片回源入口 |
| 修改源码或验证发行包 | [开发与验证](docs/DEVELOPMENT.md) | 合成样本与大部分测试可离线运行 |

## 能力与明确边界

- **数据库源码完整可见**：独立 SQLCipher/WCDB v4 认证解密实现；Linux/macOS/Windows 的 MIT 密钥工具；Linux 原生内存/登录时 passphrase 捕获源码。密钥候选必须经 HMAC 验证，不以“找到一段十六进制”冒充成功。
- **原数据只读**：解密输出与原库分离，认证失败不覆盖已有有效结果；处理有效已提交 WAL，输出后检查 SQLite。数据库密钥、passphrase 和解密副本都按秘密管理。
- **消息与媒体**：多消息分片、联系人/会话、压缩文本、引用/转发内容、DAT AES/XOR；图片准备、可选 OCR/视觉理解、网页辅助证据。媒体指纹和 SHA 校验绑定真实消息，不用缩略图冒充原图。
- **两版独立总结**：简报和回顾各一次完整结构化请求、分别缓存；不是先写回顾再压缩。保留时间顺序、话题边界、原话及来源，不用失败后的假结果填空。
- **HTML 与 PNG**：两者均有中文字体、十分钟折线与时间刻度、正文汉字数、短日期峰值；HTML 另有真实头像、可展开来源卡片、引用灰框及可打开原图的缩略图。PNG 是独立排版的静态摘要，不包含 HTML 的交互来源卡片。
- **定时和自定**：每日窗口与群内 @ 请求分开处理，默认最近 24 小时，显式时长按请求解释。两版分别记录发送回执；未知送达状态不盲目重试。
- **安全网页**：静态清单只发布必要文件，原始源快照/密钥/模型配置不上传；图片同域访问，可由远端反代回 NAS loopback 图源，保留原网站账号认证。

**不把未验证功能写成支持保证**：公开数据读取格式与客户端运行时捕获是两件事；Linux 客户端版本、ptrace/GDB 权限、macOS 调试保护及 Windows 权限必须分别确认。企业微信与所有未来客户端版本不在本项目通用兼容性承诺内。详见 [兼容性与捕获边界](docs/DATABASE.md)。

本公开工程不包含此前私有参考树中没有明确再分发许可的通用 Agent 控制台/长期记忆 Web 应用。其运行必需的数据基础与报告渲染已独立实现，而不是把无许可源码改一个 MIT 文件再上传。数据库提取、解密到本项目总结/发布链路的源码均在本仓库中；第三方镜像与客户端仍遵守各自许可。

## 1. 离线看完整样本

要求 Python 3.11+；使用虚拟环境，不修改系统 Python：

```sh
git clone https://github.com/rmqg/wechat-digest.git
cd wechat-digest
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/generate-demo.py --output /tmp/wechat-digest-demo
```

生成目录包含虚构参与者、消息、回复/转发 XML、自制插图、简报/回顾 HTML/PNG 和认证加密 SQLite 测试库。直接打开生成的 `index.html` 或 PNG 查看。所有样本均从零构造，不是对真实聊天简单替换姓名。

`fixture-key.json` 的密钥是 **PUBLIC TEST-ONLY**，任何人都能解开样本；不能拿它保护实际数据。该演示验证渲染和解密，不是一次真实 AI 总结效果或微信进程捕获的证明。实际解密与篡改检查命令见 [样本说明](examples/README.md)和[数据库手册](docs/DATABASE.md)。

## 2. 新机器部署顺序

下面是入口顺序，不替代包含权限、预期结果和故障分支的[完整部署手册](docs/DEPLOYMENT.md)。**默认不会启用发送、附加调试器或替你登录微信。**

1. 在固定源码目录（示例 `/opt/wechat-digest`）安装 Python/Docker；准备持久磁盘。
2. 初始化私有运行目录：

   ```sh
   python3 deployment/bootstrap.py --init-runtime
   cp .env.example .env
   chmod 600 .env runtime/project.json
   ```

3. 用编辑器设置 `.env` 中的强桌面密码、UID/GID和镜像引用；不要把口令直接粘进 shell 命令或历史。让 `runtime/project.json` 的 worker UID/GID 与实际文件访问者一致。先配置镜像和容器名；群 ID 可在解密、列群后再明确填写。
4. 检查并构建工具镜像，启动独立客户端：

   ```sh
   docker compose config --quiet
   docker compose --profile tools build runtime
   docker compose up -d desktop
   docker compose ps
   ```

   默认 GUI 只监听 `127.0.0.1:3000`。远程访问先按部署手册做受控隧道/认证；不要把无认证桌面映射到公网。大镜像拉取需遵守你自己的出口与代理规则。
5. 自己在桌面登录微信并手机确认，明确核实账号。显式锁定已有本机镜像：

   ```sh
   python3 deployment/bootstrap.py --pin-images
   python3 deployment/prepare-collection.py --check
   ```

6. 按[数据库手册](docs/DATABASE.md)选择精确账号目录，理解进程捕获/重新登录影响后才显式加 `--capture`。捕获与只读检查不是同一操作。密钥写入私有 runtime 后列群：

   ```sh
   python3 deployment/run-digest.py --list-groups
   ```

   用输出核实自己的 `group.id` 与 `group.name`，再设置发送账号及触发昵称。不可自动选首个群或使用示例目标发送。
7. 创建自己的模型配置（文件名沿用 `gemini-model.json`，接口并不限定厂商）：

   ```sh
   cp examples/gemini-model.example.json runtime/gemini-model.json
   chmod 600 runtime/gemini-model.json
   ```

   用编辑器填写 OpenAI-compatible `base_url`、`model`、`api_key`，并按配置手册设置 worker 可读的文件属主。不要在问题单贴原配置。
8. 先执行无发送的生成和校验，再按文档显式授权发送：

   ```sh
   python3 deployment/run-digest.py --hours 24
   python3 deployment/daily-job.py --prepare-only
   ```

   生成可调用模型并产生 API 费用；`--prepare-only` 不是无模型模式。无模型演示应使用第 1 节的合成样本。
9. 确认目标、图片、回执和异常状态处理后，再安装调度。OpenWrt 安装器默认只打印计划：

   ```sh
   sh scripts/install-openwrt.sh --dry-run
   # 阅读输出、完成配置后，才在目标宿主机执行：
   sh scripts/install-openwrt.sh --apply
   ```

   服务启用/启动是另一显式步骤。不要为了重发而删除 terminal/unknown 回执。详见[运行维护](docs/OPERATIONS.md)。

## 目录地图

```text
deployment/                 摘要、图片/OCR、调度、发送、网站、图源及安全运行器
memory/                     独立解密、WAL、消息与媒体数据基础和 CLI
agent_console/daily_report.py  独立 HTML/PNG 渲染器（不是通用控制台）
tools/wcdb-key-tool/         保留 MIT 许可的三平台密钥工具完整源码
assets/fonts/               保留 OFL 的 Resource Han Rounded 字体
examples/                   无秘密配置模板、明确标注的合成样本
scripts/                    合成样本、文档/发行检查、安装入口
docs/                       分主题的详细手册
third_party/                第三方许可与来源记录
```

运行目录是 `runtime/`，**不在 Git 或 Docker build context 内**。其中包括登录会话、原始/解密数据、密钥、模型配置、媒体缓存、报告、回执和锁；不要以为 `.gitignore` 可以代替文件权限、加密备份或日志脱敏。

## 完整手册

- [部署与升级](docs/DEPLOYMENT.md)：从空机到镜像 pin、账号采集与运行。
- [全部配置](docs/CONFIGURATION.md)：每份 JSON 的字段、默认值、读取者、权限及失败条件。
- [数据库与密钥](docs/DATABASE.md)：平台差异、原理、源码、捕获、认证解密、WAL、轮换、导出。
- [媒体链](docs/MEDIA.md)：DAT、原图/缩略图、CDN票据、OCR/视觉和来源绑定。
- [架构](docs/ARCHITECTURE.md)：进程、数据流、信任边界与状态机。
- [运行维护](docs/OPERATIONS.md)：定时、自定、回执、防重、启停、存储与回滚。
- [排障](docs/TROUBLESHOOTING.md)：按可观察状态定位，不盲目改网络或删除状态。
- [安全](docs/SECURITY.md)：授权、密钥、容器权限、网站/图片认证与公开范围。
- [开发和校验](docs/DEVELOPMENT.md)、[文档校验](docs/VALIDATION.md)、[贡献规范](CONTRIBUTING.md)。
- [许可与第三方](THIRD_PARTY_NOTICES.md)、[版本记录](CHANGELOG.md)。

## 报告问题前

提供系统/架构、Python/Docker和客户端版本、入口命令（移除凭据）、脱敏状态码、错误类别及最小合成复现。不要上传真实库、passphrase、key JSON、CDN票据、登录二维码/会话、源快照、成员列表或带头像的私人截图。安全问题按 [SECURITY.md](SECURITY.md) 私下处理。
