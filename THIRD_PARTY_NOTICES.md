# Third-party notices and release boundary

The MIT license at the repository root covers original project contributions. It does not replace another author's copyright or license. Keep the notices below when redistributing the relevant files.

## Bundled source

| Paths | Original source / pinned revision | License | Notice retained |
|---|---|---|---|
| `tools/wcdb-key-tool/wcdb_key_tool{,_macos,_windows}.py` | [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool), `79f1b5b92e12c66aa281b4a60a3c478b5f547dfa` | MIT, copyright 2025 CloudDreamAI / TANGandXUE | `third_party/wcdb-key-tool/LICENSE` |
| `deployment/vendor/wxextract/keys.py`, `passphrase.py`, package initializer | [boujuan/extract-wechat-messages-linux](https://github.com/boujuan/extract-wechat-messages-linux), `74fa2ce1004411fae353ddd35c6426d60fab8a62` | MIT, copyright 2026 Juan Boullosa | `deployment/vendor/wxextract/LICENSE`, `NOTICE.md` |
| Portions of `memory/media_sync.py` (V1/V2 hybrid DAT decoding) | `wxextract/images.py` from the same [boujuan/extract-wechat-messages-linux revision](https://github.com/boujuan/extract-wechat-messages-linux/blob/74fa2ce1004411fae353ddd35c6426d60fab8a62/src/wxextract/images.py) | MIT, copyright 2026 Juan Boullosa | Module attribution and `deployment/vendor/wxextract/LICENSE` |
| `assets/fonts/ResourceHanRoundedCN-*.ttf`, `*.woff2` | [Resource Han Rounded v0.990](https://github.com/CyanoHao/Resource-Han-Rounded/releases/tag/v0.990) | SIL Open Font License 1.1 | `assets/fonts/OFL-License.txt` |

The local vendored wxextract key/passphrase files were verified byte-for-byte against the pinned source. The database/message modules in `memory/` independently follow SQLCipher/WCDB formats; the media module additionally adapts the explicitly licensed hybrid DAT decoder listed above. They are not copies of the unlicensed local reference project. The independent HTML/Pillow renderer likewise does not redistribute that project's renderer.

The font license, copyright statements and reserved-font-name restrictions apply to the font files. Fonts are not relicensed under MIT. Rendering documents with the fonts does not make the documents subject to OFL, but redistributing the font software itself requires its OFL notices.

## External dependencies, not relicensed or mirrored

- [nickrunning/wechat-selkies](https://github.com/nickrunning/wechat-selkies): MIT source project; the Compose file references its external image. The image contains independently licensed components and the proprietary WeChat client. This repository does not claim MIT rights over the WeChat client or republish its binaries.
- [WeChat](https://www.wechat.com/) / [WCDB](https://github.com/Tencent/wcdb) are Tencent projects and trademarks. Use of the official client is subject to its own terms. This project is not affiliated with Tencent.
- Python, Pillow (HPND), pillow-heif (BSD), PyCryptodome (BSD/public-domain components), python-zstandard (BSD), OpenSSL, SQLite, GNU GDB/binutils and operating-system packages retain their own licenses. Installing/building a worker image does not relicense those packages. Consult their package metadata and original sources for notices and applicable source obligations when redistributing an image.
- Optional OCR dependencies under `deployment/ocr-requirements.txt` retain their upstream licenses; no OCR model license is replaced by the project's MIT license.

## Explicitly excluded local reference materials

The earlier private working tree included code and documentation from [xiaoguiwucan/linux-wechat-agent](https://github.com/xiaoguiwucan/linux-wechat-agent). No explicit redistribution license was verified for its general console, memory/AI applications or copied `tools/wechat-decrypt` tree. Those implementations, screenshots, documentation, runtime samples and private Git history are **not included** in this public release. A public repository or Docker image is not by itself permission to relicense its contents.

The public release provides its own reporting/data foundations plus the separately attributed MIT database-key tools. No operational capability is represented by an unimplemented placeholder. Platform-specific memory capture still requires the operator's own account, an appropriate client version, privileges and explicit manual authorization; upstream compatibility statements are not blanket guarantees.

## Synthetic examples

`examples/synthetic/` and its generator are original fictional data. The published test passphrase, raw encryption key and HMAC material are deliberately public test vectors, never credentials for real data. Do not replace them with personal exports and then commit the directory.
