# Changelog

## 1.1.0

- Add configurable QQ OneBot v11/NapCat history collection, image/OCR/web context, independently generated brief/review reports, real message-receipt verification and a deterministic offline fictional QQ demo.
- Keep per-group messages, tasks, reports and receipt databases independent; collect while model work is running, honor a seven-day request bound, prioritize fresh requests, and stop cancelled, permanent-error or ambiguous-send jobs safely.
- Recognize native QQ mentions or text containing both `@` and a configured bot name; interpret intent separately, ignore bot-authored notifications and avoid replaying historical triggers on activation.
- Add a standard-library report website with per-group scrypt credentials, root-to-account-group routing, cross-group/resource denial, safe static paths, optional private Unix sockets, and restricted single-group SSH publishing with minimal metadata.
- Reuse a responsive web reading layer, expose verified source images within their own group, preserve delivered PNG fingerprints, and provide group-home/brief-review navigation without mixing website credentials.
- Prevent empty generation and cached-empty delivery; keep confirmed daily state stable during receipt rechecks and eliminate false overdue/recovery Bark alarms without hiding real failures.
- Require a single locked QQ service instance; status reads do not reset in-flight state, and recovery marks interrupted sends unknown only under the serving process lock.
- Support QQ `title_prefix` in the independent renderer and complete JSON inside an unclosed Markdown fence while still rejecting truncation, trailing prose and incomplete streams.
- Add OCR-summary and provider-fallback support, pin `websockets==16.0`, and provide an explicitly selected, checksum-verified Linux x86-64/Python 3.12 OCR image layer with an actual OCR CI smoke check.
- Retain original third-party notices; publish only synthetic examples, not production accounts, passwords, raw conversations, browser sessions or private operational history.

## Unreleased

- Use the platform-neutral project name **WeChat Digest** and repository `rmqg/wechat-digest`; update clone instructions and repository links.

## 1.0.0

Initial public MIT release of the authorized personal-WeChat digest pipeline.

- Full database-key source for Linux/macOS/Windows under retained MIT notices, plus native Linux 4.x key/passphrase recovery source.
- Independent authenticated SQLCipher/WCDB data foundation, committed-WAL handling, message export/indexing, local DAT media decoding, and independent HTML/Pillow reporting.
- Portable explicit project configuration, immutable local image pins, account selection and fail-closed delivery targets.
- Separate complete brief/review summaries with source attribution, media/OCR/web evidence, per-edition caches, durable delivery receipts and unknown-state safeguards.
- Static website publication, same-origin authenticated image proxying, loopback-only image origin, and safe source/quote display.
- Wholly synthetic sample conversations, images and an authenticated encrypted SQLite fixture with public test-only keys.
- Verified V1/V2 hybrid DAT envelopes with AES prefixes, clear middles and XOR tails; strict raster completeness validation and HEIF decoding support.
- Consistent configured report/CLI timezones, HTML styles isolated from visible content, and PNG count/time axes. Synthetic source cards link to generated full-size images.
- Detailed deployment, configuration, database, media, architecture, operations, troubleshooting, security and development documentation.

This public history does not import private operational notes, personal data or the earlier unlicensed reference application's Git history. Third-party permissions are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
