# 数据库、密钥与授权采集

## 重要范围与准备

只对自己拥有或获得明确授权访问的账号进行操作。密钥捕获需要读取目标客户端进程内存，是高权限动作；它不是普通数据库导出。捕获会访问数据库加密材料但不会在捕获阶段读取消息正文（程序状态字段会报告 `message_bodies_read: false`）。后续解密/导入会读取正文，应另行按数据最小化原则处理。

本仓库提供 Linux 本机客户端捕获路径和格式解密代码。它不提供 Windows 或 macOS 客户端的本地密钥提取命令/实现，也不把其他项目的未授权许可实现或文档打包进来。上游曾描述过针对不同操作系统的不同来源/进程调试方法；那些说明不构成本仓库可验证、可执行或受支持的步骤。不要在 Windows/macOS 上套用 Linux `/proc`、GDB、ptrace、ELF 命令。本仓库也不承诺支持任何未经仓库代码与测试确认的 WeChat 版本。

## 账户发现与安全检查

账户目录通常位于桌面容器的 GUI 用户目录下 `xwechat_files/<账号目录>/db_storage`，旧布局也可能位于 `Documents/xwechat_files/...` 或 `文档/xwechat_files/...`。路径中的目录名是账号目录标识，不应当当作显示名或群 ID。用显式参数避免多个账号时误选。不要把目录列表、账号名或密钥输出到公开日志。

前置条件：公共部署配置已正确填写必需的 `group.id` 和 `group.name`；桌面容器已启动、客户端已登录并建立数据库；操作人能够使用 Docker daemon；项目所配置的本地 keyscan 镜像已构建且可用（脚本使用 `--pull never`）；用户能够在本人手机上批准登录。`prepare-collection.py` 通过 Docker 命令调用宿主 daemon，操作者必须有相应 Docker 权限；容器隔离不消除 Docker socket/daemon 权限本身的信任风险。

先只做捕获点检查（不会 attach debugger）：

```sh
python3 deployment/prepare-collection.py --check
```

检查通过时不输出密钥，内部校验桌面账号目录唯一且至少有数据库，并检查客户端二进制与捕获点；容器会退出。若发现多个候选目录，使用目录名明确指定账号；若没有数据库，先正常登录客户端并等待本地建库。若二进制 hook 不匹配或不可读，停止，不要强行跳过。

显式进行授权登录时捕获：

```sh
python3 deployment/prepare-collection.py --account '<确切账号目录名>' --capture --timeout 300
```

命令会等待用户在客户端窗口中完成手机批准的重新登录；不要在非本人账号上操作，也不要把终端/内存内容录屏或共享。成功时只打印类似 `{"keyed_databases":N,"messages_read":false,"source_read_only":true}` 的计数状态，并在 runtime 私有目录创建 `selection.json` 与 keyscan 输出。N 应大于 0。若未捕获到有效且验证通过的消息库密钥，脚本拒绝创建可用选择。超时、被系统 ptrace 策略拒绝、登录流程改变、捕获点变化、账号路径不唯一时都应停止并排查；不要放宽系统安全策略或切换到其他人的会话。

容器使用无网络模式、只读根文件系统、只读账号挂载、资源限制、删除大部分 capabilities，并仅为访问同一桌面容器进程所需增加 `SYS_PTRACE`、`DAC_READ_SEARCH`；其 PID namespace 与桌面共享。它以容器内 root 运行以完成捕获，并非低权限 worker。keyscan 临时输出目录为私有权限；捕获结果仍是可解密账户数据的凭据，应按秘密管理。容器退出不保证宿主机 swap、daemon 日志或备份中不存在任何系统级残留。

## Linux 捕获机制：原始密钥与 passphrase

SQLCipher 页面校验需要 32 字节数据库加密密钥（enc key）；`all_keys.json` 中的 `enc_key` 是该格式的十六进制字节串。Linux 专用的 MIT `wxextract` 源码包含按数据库盐检查运行中进程候选密钥的路径：从目标数据库第一页取得每库 salt，对进程映射/内存中的候选值逐一执行页面 HMAC 校验。Linux 内存读取受到 `/proc` 权限、ptrace 限制、进程状态和容器配置约束；页面校验失败的候选不会成为有效密钥。权限允许访问进程内存不等于对账号内容获得授权。

另一路径取得 32 字节 passphrase，而非可直接传给解密 CLI 的 enc key。`wxextract.passphrase` 对登录阶段的捕获材料按每个数据库 salt 计算：`PBKDF2-HMAC-SHA512(passphrase, salt, 256000, dkLen=32)`，再验证第一页 HMAC。不同数据库有不同 salt，因此同一 passphrase 对每个 DB 分别派生各自 enc key。passphrase 缓存与 enc key 文件都具有同等敏感性；不要把 passphrase 当作可替代 enc key 的 CLI 参数。

仓库当前可见 Linux 捕获入口包括 `deployment/capture_41.py` 的 GDB 硬件断点登录捕获，以及 `deployment/native-capture.py` 配合 `wxextract.passphrase` 的 ptrace 捕获实现。前者先检查 ELF 捕获点/运行二进制是否一致，捕获点不支持时明确不回退软件断点；后者在启动链上捕获后重新扫描数据库并逐库验证。另有 `wxextract.keys` 的内存原始密钥扫描模块。捕获方式在时机、所需启动链、权限和失败模式上不同；`prepare-collection.py` 使用的 CLI 路径以当前部署代码为准，不要自行把入口替换成另一个模块。Linux 4.0/4.1 标签仅标识这些实现的设计路径，不代表所有对应客户端小版本或所有发行版均兼容。未经仓库测试确认的版本、架构、客户端构建、账户存储位置都可能不适用。

## SQLCipher4 页面格式与拒绝策略

本项目实现的格式参数：页长 4096 字节；每页末尾保留 80 字节（16 字节 IV + 64 字节 HMAC-SHA512）；第一页开头 16 字节为该数据库盐；数据部分使用 AES-CBC；MAC 派生使用由 salt 每字节 XOR `0x3A` 得到的 MAC salt，PBKDF2-HMAC-SHA512 2 轮、32 字节。加密页密钥本身由客户端捕获逻辑得到，或由 passphrase 对该数据库 salt 以 256000 轮 PBKDF2-HMAC-SHA512 派生。必须针对每个 DB 的 salt 使用该 DB 的 key。不能把某个消息库密钥拿去解密其他文件。

读取时每页必须验证 HMAC，第一页使用 `SQLite format 3\0` 明文头部恢复逻辑页内容。认证失败、错误盐/密钥、截断页、未知格式应停止并拒绝输出有效数据库；不要忽略验证错误，也不要尝试把密文当成普通 SQLite 打开。HMAC 可检测无正确密钥的损坏/篡改，但不证明数据来源真实、消息未被客户端修改或报告完整。

WAL 是独立的密文页帧日志，客户端可能在 checkpoint 前把最近提交保留在 `-wal` 中。只解密主 DB 可能缺少已提交的最新数据。`patch_wal` 校验 WAL header、帧校验与页认证，仅将完整提交事务的帧应用到解密副本；未提交尾帧不应用。WAL 无效、与 DB 不匹配或身份验证失败应作为故障，不应把半截 WAL 拼入结果。主库、WAL 应来自同一一致性快照；客户端仍在写入时直接逐个复制文件可能产生竞态，优先使用停止写入后的只读快照/文件系统快照；不可用时保留同一时刻的主库和 WAL，捕获后核对页数及 SQLite 完整性。不得修改、checkpoint、删除或重命名客户端原文件。

## 从密文副本解密与验证

解密器支持 `--key` 直接提供 32-byte enc key 十六进制值，或用 `--keys KEYFILE.json --rel DATABASE_REL_PATH` 从密钥映射选择该数据库条目。`--keys` 模式不要把密钥内容粘贴到命令行；输入密钥文件必须是受信 runtime 私有文件。路径示例中的 `/path/to/...`、`DATABASE_REL_PATH` 都是说明性占位符。先建立仅自己可读的目标目录：

```sh
umask 077
mkdir -p runtime/private-decoded
chmod 700 runtime/private-decoded
python3 memory/decrypt_sync.py \
  /path/to/private-snapshot/message/message_0.db \
  runtime/private-decoded/message_0.db \
  --keys runtime/keyscan/all_keys.json \
  --rel message/message_0.db
```

程序输出 `decrypted <页数> authenticated pages; sqlite_valid=True`；页数应大于 0。若相邻 `message_0.db-wal` 存在，CLI 自动在目标副本上校验 WAL 并只应用校验通过的完整提交事务；当前摘要输出不报告应用了多少 WAL 帧。WAL 校验/页认证失败、错误密钥或无对应密钥会停止并拒绝使用输出；最后还会对解密后目标执行只读 SQLite `quick_check`，失败时拒绝成功状态。目标必须是新的私有路径，解密器不可写原密文。若使用 `--key KEY_HEX`，该参数可能暂时出现在本机进程列表和 shell history；多用户主机避免这种方式。`--key` 必须是 enc key，不可传 passphrase。

真实 key 从 private keyfile 读取，不得粘贴到文档、issue、截图或公开命令记录。解密依赖 `pycryptodome` distribution（代码导入 `Crypto.Cipher.AES`）。

导入器接受解密后的 SQLite 文件，且按需要显式指定源表名：

```sh
python3 memory/memory_ingest.py export \
  runtime/private-decoded/message_0.db runtime/private-decoded/messages.jsonl
python3 memory/memory_ingest.py index \
  runtime/private-decoded/message_0.db runtime/private-decoded/messages-index.sqlite
```

可用 `--table MSG` 显式指定表名。导出为 JSONL；索引为本地私有 SQLite，字段包括 `message_uid`、`timestamp`、`sender_id`、`type`、`content`、`source_row`。具体源库表名、列名和消息结构依客户端版本变化；导入器只按通用规则发现表列，不意味着所有 schema 均支持。检查行数、表/列发现情况和少量经授权的来源记录，再继续；空输出或字段无法识别时停止。`message_parse` 对已识别的消息做本地文本提取：可抽取应用消息的标题/描述/URL/record 内容；引用消息解析 `<refermsg>` 中展示名和内容；普通文本、压缩内容、系统/媒体消息按类型输出标签。它不执行 XML/链接、不访问 URL/网络，也不验证消息陈述或链接可信度。无法识别的结构应作为原始/未知内容留存，不得声称已完整解析。压缩消息内容只支持实现包含的 zlib 与 zstd 解压；zstd 需要 Python `zstandard` 或系统 `libzstd`，未安装时相应数据无法解压。不要公开内容样本；输出应保存在私有目录，避免索引与 JSONL 进入公共站点或模型输入。

### 本地合成 SQLCipher fixture：安全自检

仓库 `examples/synthetic/` 中全部数据均为虚构；这不是微信备份或真实聊天记录。离线生成到独立目录（需要本机 Python、SQLite shared library、PyCryptodome）：

```sh
python3 scripts/generate-demo.py --output /tmp/wechat-synthetic-demo
```

生成器输出合成 brief/review JSON、来源快照、HTML/PNG、示例 XML/图片、`synthetic-sqlcipher4.db` 和 `fixture-key.json`、`fixture-integrity.json`。fixture 表 `synthetic_messages(message_id, participant, body)` 有两行合成记录。公开 `fixture-key.json` 中的 passphrase/key/salt 只能用于测试，绝不能用于真实数据库或生产配置。完整合成数据列表及其边界见 [examples/README.md](../examples/README.md)。

fixture 解密示例会把已公开的测试专用 derived key 直接写入命令，必须只对刚生成的 synthetic 文件运行：

```sh
python3 memory/decrypt_sync.py \
  /tmp/wechat-synthetic-demo/synthetic-sqlcipher4.db \
  /tmp/wechat-synthetic-demo/decrypted.sqlite \
  --key aad9272dd880fec8c2e134a09d3548a204ccb8a72ca7f765fe26bd0b95eca69a
```
只读查询解密副本行数，预期显示 `(2,)`：

```sh
python3 -c "import sqlite3; c=sqlite3.connect('file:/tmp/wechat-synthetic-demo/decrypted.sqlite?mode=ro&immutable=1', uri=True); print(c.execute('SELECT count(*) FROM synthetic_messages').fetchone()); c.close()"
```

可选负向验证仅用合成副本：将一个仅含零值的错误 key 传给 fresh target，预期非零退出/认证失败；不要把 `if` 包装后的 shell 状态误认为解密成功：

```sh
python3 memory/decrypt_sync.py \
  /tmp/wechat-synthetic-demo/synthetic-sqlcipher4.db \
  /tmp/wechat-synthetic-demo/wrong-key.sqlite \
  --key 0000000000000000000000000000000000000000000000000000000000000000
```

测试篡改时先复制 fixture，不要修改源文件。`fixture-integrity.json` 中 offset 128 是生成器为负向测试选取的密文 byte offset；复制件解密应被 HMAC 拒绝：

```sh
cp /tmp/wechat-synthetic-demo/synthetic-sqlcipher4.db /tmp/wechat-synthetic-demo/tampered.db
printf '\001' | dd of=/tmp/wechat-synthetic-demo/tampered.db bs=1 seek=128 conv=notrunc status=none
python3 memory/decrypt_sync.py \
  /tmp/wechat-synthetic-demo/tampered.db \
  /tmp/wechat-synthetic-demo/tampered.sqlite \
  --key aad9272dd880fec8c2e134a09d3548a204ccb8a72ca7f765fe26bd0b95eca69a
```

期望前述有效 fixture 解密通过，行数为 2；错误 key 或修改密文应非零退出并拒绝认证。生成器的 `fixture-integrity.json` 给出文件 SHA-256 与安全字节偏移。不要将负向测试结果外推为对所有客户端数据库的兼容证明。合成测试只验证本仓库实现的公开格式/样例输入。



## 原始库只读、快照、权限与密钥轮换

- 捕获 mount 应只读；所有解密和 WAL 更新写入隔离的目标目录。使用只读 URI 检查源 SQLite，不要由分析器打开原库进行可能产生写入的数据库操作。
- 稳定快照优于运行中逐文件复制。若必须从运行客户端获取文件，只能复制同一采集时点的 DB 与 WAL，并在私有副本上处理；SHM 是客户端共享内存辅助文件，不应当作为数据库副本的替代或对外发布。
- `runtime/` 是敏感区域。目录建议 `0700`、密钥及导出文件 `0600`，仅授予必要执行用户。默认备份不应包含密钥、解密数据库、报告和模型配置；若业务需要备份，必须单独加密并限制访问、期限和恢复人员。
- 更换/重建客户端数据、盐发生改变或 DB 被轮转后，旧 enc key 不能默认继续有效。重新对新文件按每个 salt 验证并生成密钥映射；保留必要旧密钥用于旧快照的期限应最短。密钥轮换意味着停止使用/销毁旧凭据，并重新评估旧备份；删除 JSON 文件不代表备份或 SSD 上不可恢复。
- 错误密钥、不同账号密钥、页 HMAC 错误和 WAL 不匹配都必须拒绝；不要因“能打开”而跳过认证。SQLite `integrity_check` 通过仅表明数据库结构检查通过，不是来源真实性证明。

## MIT 工具来源与边界

`deployment/vendor/wxextract/` 中 key/passphrase 模块来自 MIT 项目，`deployment/wcdb_key_tool.py` 及其许可证文件来自 MIT 项目。分发时应同时保留相应 LICENSE/NOTICE；这些第三方许可不意味着微信数据格式为公开稳定 API，也不构成对任一客户端版本的兼容保证。本手册只说明本仓库自有调用边界和公开格式参数，不复刻未获许可的上游项目实现或操作手册。
解密与媒体命令在实际执行环境需要 `pycryptodome` Python distribution（代码导入 `Crypto.Cipher`）；先按发布所附依赖清单安装，未安装时会在首次解密时失败。不要用来历不明的同名包替代。示例中 `/path/to/...`、`KEY_HEX` 均为说明性占位符，不能直接运行。

### 单独的 Windows/macOS MIT 工具

`tools/wcdb-key-tool/` 另含第三方 MIT 项目的 Windows、macOS 源码；它们不是 `memory/decrypt_sync.py` 的替代，也没有接入本项目 Linux runtime/容器采集流程。请阅读该目录对应 LICENSE 和每个脚本的顶部要求、限制及安全提示。脚本声明的行为来自其自身实现，不是本项目对这些平台做的验证结果：

- Windows 源码描述 4.0.x 旧路径的进程内存 raw-key 扫描、4.1+ 的运行时 `Config.Cipher` 扫描/HMAC 验证；`capture-experimental` 是研究备用路径，不是默认主路径。需足够进程读取权限；调试工具 `cdb.exe` 仅实验捕获需要。脚本标题/自述不构成对任何具体 Windows/WeChat build 兼容保证。
- macOS 源码描述旧路径的内存扫描与 4.1.10+ 的 LLDB 断点捕获 passphrase 后按数据库 salt 派生。它要求本机 LLDB、root/task-for-pid 权限，并在前提说明里要求对 WeChat app 做 ad-hoc 重签名、重启；这会改变应用签名状态，更新后可能需要重做。高权限调试或修改签名可能影响客户端安全性/更新/运行行为。此流程不能当作无副作用或本项目支持的部署步骤。

两个平台源码都可能在进程内存、调试器交互、OS 安全策略、客户端 binary/layout、账号路径和本地构建差异下失败；错误 key 不应被保存为有效结果，解密仍必须逐页 HMAC 验证。若无经过组织批准的独立安全审查、平台备份与回滚计划，就不要执行这些平台的调试/重签名路径。它们不支持本仓库对 Windows/macOS 提供 end-to-end 摘要、媒体、部署或发送保证。Linux 手册命令不得跨平台照搬。
