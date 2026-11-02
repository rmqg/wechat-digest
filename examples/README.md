# Synthetic examples — fictional test data only

Every participant, message, timestamp, group name, image, XML payload, and database row here is invented. These files are rendering/decryption examples, **not** evidence of real AI-generated reports or real WeChat content. The SQLCipher fixture is format-valid test data; its openly published key and integrity material are strictly test-only and must never be used with real databases.

## Generate or refresh

From the repository root:

```sh
python3 scripts/generate-demo.py
# or choose a separate destination
python3 scripts/generate-demo.py --output /tmp/wechat-synthetic-demo
```
Generation requires Pillow, PyCryptodome (the data-core AES dependency), the public renderer, and an installed CJK font. The CJK font may be configured with `WECHAT_DIGEST_FONT`; the renderer fails clearly if none is available. The generator uses only local fixture data and does not connect to WeChat, the network, a model, or a GUI and does not send messages.

`fixture-key.json` contains the **PUBLIC TEST-ONLY** passphrase, public salt, derived encryption key, and derived HMAC key. `fixture-integrity.json` contains the SHA-256 checksum of the generated encrypted database plus a safe byte offset for a tamper test. These values are intentionally public and provide no protection. Do not substitute them for real keys. The encrypted database has a deterministic public salt so generation is reproducible. Successful authenticated decryption should recover the `synthetic_messages` table; a wrong key or modified ciphertext must fail authentication.



## QQ fictional sample

Generate the deterministic QQ renderer sample without contacting OneBot, QQ, the network, or a model:

```sh
python3 scripts/generate-demo.py --qq --output /tmp/qq-digest-demo
```

The sample uses invented numeric group/self IDs and fictional messages. It is a renderer fixture, not AI output, a real QQ history export, or evidence of a live OneBot integration. QQ runtime reports and assets are generated independently of the WeChat synthetic SQLCipher fixture.

QQ deployment templates are also provided in `qq-service.example.json`, `qq-site.example.json` and `qq-publish.example.json`; the WeChat daily-delivery watchdog has `daily-watchdog.example.json`. They contain synthetic group/self IDs, non-routable hosts and an explicitly invalid site-hash placeholder only; do not treat them as production endpoints or credentials. Copy templates to private directories and fill actual identities, secret paths and independently scoped SSH/site settings outside the repository.

The source snapshots intentionally include a reply and a forward, a missing-image reference, and a local generated PNG. XML and report text are fictional and safe to parse locally. No old runtime files are inputs to this generator.
