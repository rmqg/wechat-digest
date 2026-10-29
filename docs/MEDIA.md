# 媒体、来源与派生证据

## 范围与数据来源

媒体分析只处理用户授权账号本地缓存，或部署流程明确取得并验证的媒体字节。消息正文/XML/资源表可能包含不可信路径、URL、MD5、尺寸和 AES 元数据；解析不会执行 XML、下载 URL 或把消息文本当指令。原始媒体、解码副本、缩略图、OCR、视觉模型返回值、网页上下文都可能包含个人信息，应按私有消息处理。

微信图片数据库消息通常包含图片节点/属性（可能有 `md5`、`length`、`hdlength`、大图 URL 等），`media_sync.load_resource_map` 可从已解密 SQLite 识别包含聊天 ID、本地消息 ID 与 MD5/resource ID 的通用表；具体表结构会随客户端版本变化，匹配失败时不能猜映射。`source_media` 的缓存查找用消息 XML 的图片 MD5 与本地 full-image cache metadata 建立候选关系，并在发布前验证路径位于私有解码目录、字节和质量元数据可核对。没有映射的媒体不是不存在的证明。

## 本地 `.dat` 格式与安全解码

仓库自有 `memory/media_sync.py` 可识别常见 JPEG、PNG、GIF、WebP、HEIC、ICO 与部分视频签名。实现覆盖本地缓存中固定单字节 XOR 的 legacy `.dat`，以及 `07 08 V2`（full）/`07 08 V1`（thumbnail）开头的 V2 envelope；V2 头可为 13 或 15 字节，含明文长度/标志及 AES-ECB 填充密文，需匹配的 AES key；非零 XOR key 会用于初始 AES block 之后的解码字节。解码后只有识别出受支持签名才输出；未知头、错误 key、损坏填充、截断长度会以失败处理。支持的缓存包装并不等于支持远端 CDN/服务器数据、所有版本或所有媒体格式。

手动对一个已授权的 `.dat` 解码，输出必须写入私有目录：

```sh
umask 077
mkdir -p runtime/private-media
chmod 700 runtime/private-media
python3 memory/media_sync.py /path/to/private-cache/image.dat runtime/private-media/image.bin
```

成功时输出 `wrote <字节数> bytes (<format>)`；文件扩展名不代表真实格式，CLI 根据文件签名决定是否可识别。固定 XOR 文件需用来源映射取得对应 `--xor-key N`，十六进制形式可写 `--xor-key 0xNN`；V2 数据需提供对应的本地 key：

```sh
python3 memory/media_sync.py /path/to/private-cache/image.dat runtime/private-media/image.bin --aes-key AES_KEY_HEX --xor-key 0xNN
```

不要把账号/密钥写进共享命令记录；手工解码命令行参数可能对本机进程可见。失败 `unrecognized or invalid local image data` 表示此实现无法识别该文件/参数，不代表可尝试网络下载或绕过校验。`.dat` 输出应再用图像查看器/签名检查确认，且不要把本地缓存直接当作消息原始附件。

## full、client_full 与 thumbnail

- `original`：解码字节长度与消息元数据的预期原图长度相符，且 MD5 与该消息声明相符。`prepare-images.py` 在消息属性和文件 alias 匹配的基础上执行验证。MD5 是格式/关联校验指纹，不是抗碰撞数字签名，不证明发送者、内容时间或远端来源真实性。
- `client_full`：来自客户端完整质量缓存/符合预期下载传输记录的图像，具有源字节 SHA-256 与 transfer/消息描述绑定检查，但不一定与消息 `md5`/length 完全相同。展示与 OCR 时应保留此质量标记；不能擅自升格为原图。
- `thumbnail`：低分辨率缩略图，不是完整图片。其细小文字、二维码、细节可能缺失或错误；不得声称它完整对应原图。默认 OCR 只接受 `original`/`client_full`，视觉描述接口也可根据 `source_quality='thumbnail'` 提示模型保守描述。用于报告的缩略图要标明质量，并避免读小字或做细节判断。
- unavailable/missing：未找到或解码失败。不可用不等于消息没有图片，也不可拿相邻图片补位。

图片别名可来自消息 MD5/资源描述，候选缓存先后还要通过 SHA-256、文件字节数、消息尺寸/MD5 或 transfer descriptor 检查。SHA-256 表示本地被检查的内容字节；它不证明原始微信传输或内容作者。记录数据源、`message_uid`、消息 XML MD5、source quality、文件 hash、缓存路径和失败原因，保持“原始消息元数据”与“本地派生图像”的区分。

## 可复现本地图像处理

部署流水线 `deployment/image_pipeline.py` 对指定 Unix 时间窗做串行化图像工作，复用锁与私有 cache。它依赖已经完成的项目配置、微信选择、所需本地运行镜像和 runtime 数据；不能脱离完整配置只靠一条 CLI 命令重建图像证据。通用命令形态：

```sh
python3 deployment/image_pipeline.py --start START_EPOCH --end END_EPOCH --scope daily
```

`START_EPOCH`/`END_EPOCH` 是整数秒，且 start 必须小于 end；`--scope` 可为 `daily`、`realtime`、`on-demand`，`--max-images N` 可限制数量（默认 0 表示不设上限），`--no-wait` 遇到已有锁时不等待。成功输出结构化 JSON `status: complete` 及计数；异常输出 `status: failed` 和错误类型并返回非零。状态不等于已得到所有消息图片；检查每条记录的可用性、`source_quality`、MD5/SHA-256、尺寸与失败原因。此阶段可能按配置从远端请求完整图片；只有在网络访问和向服务提供商披露相应请求得到明确批准时才启用。媒体文档/消息声明中的 URL 不授权抓取。

## OCR：本地、缓存与限制

OCR 使用本地 `rapidocr-onnxruntime`（代码所列版本 1.4.4），而不是外部 OCR 服务。manifest 中每条内容要求 `message_uid`、图片路径、`available: true` 和 `source_quality: original` 或 `client_full`；程序拒绝缩略图/缺图项。结果包含文本、行及置信度、图像 SHA-256、引擎/版本和 cache hit；置信度低于 0.75 会标记 low-confidence，不能当作准确转录。缓存以图像 SHA-256 和引擎版本区分，OCR 不应脱离原图来源独立使用。

```sh
python3 deployment/ocr_images.py \
  --manifest runtime/private-media/image-manifest.json \
  --output runtime/private-media/ocr.json \
  --cache-dir runtime/private-media/ocr-cache
```

输出 JSON 摘要含 messages/ok/failed/pending 和引擎版本。退出码 0 表示没有 failed/pending，不证明文本识别正确；2 表示有条目失败或待处理；1 表示 manifest/处理出错。复核原图、行坐标/文本、置信度和消息关联；识别不到、失败或低置信内容必须显式保留。

## 视觉描述与外部披露

`deployment/vision.py` 向 `base_url` 配置的 OpenAI-compatible chat-completions 接口提交图像，以 JSON 返回描述、可见文字、可直接核实事实和不确定性。虽有提示要求不推断身份、不服从图内指令，模型输出仍可能错误或受输入影响。图片内容、其可见文字、文件元数据、请求时间、模型响应以及失败诊断可能离开本机；操作人必须先确认服务提供方、数据处理条件、费用、保留规则和具体图片的披露授权。无明确批准时使用本地 OCR 或跳过视觉阶段。不要在公共配置或日志写入 `api_key`。

手工命令示例（仅在已授权向所配置服务披露此图片后执行）：

```sh
python3 deployment/vision.py \
  --image runtime/private-media/image.png \
  --config runtime/gemini-model.json \
  --output runtime/private-media/vision.json
```

配置 JSON 使用真实字段 `base_url`、`model`、`api_key`，必须放在私有 runtime 目录（建议 `0600`）。成功产物为 JSON；错误输出不应泄露密钥。配置不存在/不完整、文件不是支持的图像、网络/HTTP 响应异常都会失败。视觉结果需与原图并列保留来源质量与 SHA-256，并由人复核；不能把模型描述转写为发送者声明或事实。

## OCR/视觉与网页上下文的证据关系

媒体结果至少区分四种来源：原始聊天文字/XML、原图字节或客户端缓存、OCR 派生文本、外部视觉模型派生描述。浏览器/网页摘要或联网检索补充的上下文是第五种独立来源；即使 URL 出现在消息里也不能自动访问。`web_context` 的联网行为及其独立配置必须按部署边界审批；网络页面是第三方不可信内容，不能替代本地消息证据，也不能使图像解释得到验证。

每项派生信息应关联原始 `message_uid`、媒体指纹、质量、引擎/模型和生成时间；缺少来源映射时标为未知。摘要及报告应明确区分消息写了什么、图中可辨认什么、OCR 识别到什么、模型推断了什么、网页另行声称什么。冲突时保留差异与不确定性，不要悄悄覆盖消息文本。

## 合成样例与兼容性声明

`examples/synthetic/` 提供完全虚构的图片、reply/forward XML、缺失图片引用和 brief/review 来源快照，可用于阅读来源字段与报告中的媒体呈现。它不含真实微信缓存 `.dat`，不能用来验证账号解码或宣称对其他客户端缓存格式兼容；生成/文件说明见 [examples/README.md](../examples/README.md)。

部署中的 `prepare-images.py` 代码针对其标注的 Linux 客户端 MMKV 身份布局实现，并非通用账号发现器；其代码注释指出格式常量对应 pinned Linux 4.1.13.26 MMKV layout。客户端更新、MMKV 配置变化或其他平台可能不能解析/匹配。此限制不能外推为项目对 4.1.13.26 所有图片功能的兼容保证；仅表示该实现写明的具体输入布局。遇到 CRC/identity/account mismatch 或未知媒体包装时停止，不要改写身份校验或复用其他账号的 AES/XOR 参数。

## 常见失败

- 数据库资源表找不到：检查授权导出的解密库是否完整、该 schema 是否支持；不要猜表名/把别的消息资源映射套用。
- 解码无格式/无效 key：保留原 `.dat`，核对正确账号本地配置、资源指纹和媒体类型；不符合当前实现支持格式即停止。
- 只有缩略图：显示缩略图限制并降低信心；需要完整内容时按已授权的应用流程获取，不能在报告中标成原图。
- MD5/尺寸/hash 不匹配或路径越出私有 decoded 根目录：拒绝发布；这可能表示记录损坏、映射错、文件变化或不匹配来源。
- OCR 缓存不存在/引擎初始化失败：保留失败状态并人工选择是否重试；不要把空结果解释为图中没有文字。
- 视觉 API 返回不完整/不合 schema：保留错误类别，检查服务配置和用户授权，不要将诊断原文或凭据公开。
