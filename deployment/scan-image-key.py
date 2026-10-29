#!/usr/bin/env python3
"""Read-only V2 image-key lookup; never attaches, pauses or changes a process."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import sys
from Crypto.Cipher import AES
from PIL import Image
sys.path.insert(0, "/source/memory")
import media_sync


def check_key(key, samples):
    first = samples[0]
    block = AES.new(key, AES.MODE_ECB).decrypt(first[15:31])
    kind = media_sync.detect_image_format(block)
    if kind not in {"jpg", "png", "gif", "webp", "hevc"}:
        return None
    xor_values = {0x88}
    if kind == "jpg":
        xor_values.add(first[-1] ^ 0xd9)
    if kind == "png":
        xor_values.add(first[-1] ^ 0x82)
    for xor in xor_values:
        valid = 0
        for sample in samples:
            decoded, fmt = media_sync.decrypt_v2(sample, key, xor)
            if not decoded:
                continue
            if fmt == "hevc":
                valid += 1
                continue
            try:
                with Image.open(io.BytesIO(decoded)) as image:
                    image.load()
                valid += 1
            except Exception:
                pass
        if valid == len(samples):
            return xor
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True, help="Private JSON list of local DAT paths")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    paths = [Path(p) for p in json.loads(Path(args.samples).read_text())]
    samples = [path.read_bytes() for path in paths]
    if not samples or any(len(s) < 31 for s in samples):
        raise SystemExit("No complete V2 image samples")
    pids = []
    for folder in Path("/proc").iterdir():
        if not folder.name.isdigit():
            continue
        try:
            if os.readlink(folder / "exe") == "/opt/wechat/wechat":
                pids.append(int(folder.name))
        except OSError:
            pass
    found = set()
    pattern = re.compile(rb"[0-9a-f]{16,64}")
    for pid in pids:
        regions = []
        for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
            fields = line.split()
            if "r" not in fields[1] or (len(fields) >= 6 and fields[5].startswith(("/usr/lib/", "/lib/", "/usr/share/"))):
                continue
            start, end = (int(v, 16) for v in fields[0].split("-"))
            if 0 < end - start < 512 * 1024 * 1024:
                regions.append((start, end))
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
            for start, end in regions:
                position = start
                carry = b""
                while position < end:
                    try:
                        memory.seek(position)
                        block = memory.read(min(8 * 1024 * 1024, end - position))
                    except OSError:
                        break
                    if not block:
                        break
                    data = carry + block
                    for match in pattern.finditer(data):
                        value = match.group(0)
                        for offset in range(0, len(value) - 15, 16):
                            candidate = value[offset:offset + 16]
                            if candidate in found:
                                continue
                            found.add(candidate)
                            xor = check_key(candidate, samples)
                            if xor is not None:
                                output = Path(args.output)
                                output.write_text(json.dumps({"image_aes_key": candidate.decode(), "image_xor_key": xor}), encoding="utf-8")
                                os.chmod(output, 0o600)
                                print(json.dumps({"validated_image_samples": len(samples), "process_paused": False, "key_printed": False}), flush=True)
                                return
                    carry = data[-64:]
                    position += len(block)
    raise SystemExit(f"No image key validated against {len(samples)} local samples; no client state changed")


if __name__ == "__main__":
    main()
