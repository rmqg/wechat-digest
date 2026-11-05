# Changelog

## 1.2.3

- Automatically send the QQ site-access/use notice only after both editions for a daily task have confirmed `sent` receipts. Track notice delivery separately so restarts do not duplicate confirmed notices and unknown submissions are not blindly retried.
- Expose independent notification receipts through `status.site_access_notices`; recover interrupted `sending` notices to `unknown`, retry only confirmed pre-submit failures, and never alter image receipts or regenerate/resend reports.
- Add an optional private per-group `site_access_file` configuration for the notice; retain independent group authorization and existing report image receipts.
- Synchronize public runtime/template references to 1.2.3. No real domains, credentials, group identifiers, URLs or private chat data are published.

## 1.2.2

- Fix shared-domain QQ report access in browsers that may reuse cached HTTP Basic credentials: route `/qq/*` to the QQ-specific authenticator before username-based legacy routing, while preserving the existing WeChat root route and per-group authorization boundaries.
- Return a standards-compliant HTML 401 response with a Basic challenge so browsers can present their native authentication prompt. Safari may require that prompt; automatic login from URL user-info is not guaranteed.
- Synchronize public runtime/template references to 1.2.2. No production account, credentials, private URL, group identifier or report artifact is included.

## 1.2.1

- Fix GitHub Actions workflow cache initialization: use `runner.temp` only in a step environment, persist the cache path through `GITHUB_ENV`, and reuse the same offline tokenizer cache in subsequent test steps.
- Synchronize public runtime/template version references to 1.2.1. No production image, dependency, model or runtime was changed.
- This corrective release supersedes v1.2.0 for use. The v1.2.0 tag and published assets remain unchanged.

## 1.2.0

- Rank QQ speakers by accumulated original-text tokens using pinned offline `tiktoken` data; include literal captions, exclude generated media/OCR/AI/web context, and omit tokenizer details from reports.
- Refresh QQ CDN `rkey` only from the verified account's group ticket after a CDN 400, preserving resource identity, original-image handling, cache and prepared-delivery receipts; reject only structured summary-provider 400 responses permanently.
- Add verified literal speaker quotes and a ranked activity/token section to the end of brief reports. Brief and review choose source-grounded quotes independently; empty candidates stay empty, and visible quotes participate in the Han-character count.
- Use the platform-neutral project name **WeChat Digest** and repository `rmqg/wechat-digest`; update clone instructions and repository links.
- Check configured desktop availability before launching report workers; treat absent or ambiguous worker receipts as unknown, never as retryable pre-submit failures.
- Let the live Weixin window start at any fully visible screen origin while preserving calibration reference, exact template size, dialog guards and stable-instance checks; verify the target header before submission.
- Include sanitized source/stage and report-day/edition context in delivery Bark notices, retaining failure context for deduplicated recovery without exposing raw errors or secrets.
- Drop stale active edition errors only after validated success receipts, including terminal replays, while retaining historical logs and forbidding regeneration or duplicate submission.
- Keep historical token counts and confirmed PNG fingerprints unchanged; publish only synthetic examples, not production accounts, conversations, reports or operational history.

## Unreleased

- No unreleased changes.

## 1.1.1

- Persist each QQ task's prepared report paths and PNG SHA-256 fingerprints before sending. Resume only unsent editions from those fixed artifacts; late history sync never regenerates or overwrites a confirmed edition.
- Stop on missing or changed prepared files, refuse unsafe partial-delivery recovery without a manifest, and preserve completion when both editions already have confirmed receipts.
- Give separate requests for the same window separate output namespaces; the generation CLI accepts a validated task ID or creates a fresh UUID.

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
