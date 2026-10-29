#!/usr/bin/env python3
"""Compare existing Gemini and Maibot vision endpoints on the same local pictures."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
from vision import describe_image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="/work/image-manifest.json", type=Path)
    parser.add_argument("--gemini", default="/models/gemini.json", type=Path)
    parser.add_argument("--qwen", default="/models/qwen.json", type=Path)
    parser.add_argument("--output", default="/work/vision-comparison.json", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    items = json.loads(args.manifest.read_text())
    configs = {"gemini": json.loads(args.gemini.read_text()), "qwen": json.loads(args.qwen.read_text())}

    def run(job):
        name, item = job
        try:
            return {"provider": name, "message_uid": item["message_uid"], "sha256": item["sha256"],
                    "source_quality": item["source_quality"], "result": describe_image(item["image_path"], configs[name])}
        except Exception as exc:
            return {"provider": name, "message_uid": item["message_uid"], "error": str(exc)}

    jobs = [(name, item) for item in items if item.get("available") for name in configs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, jobs))
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(json.dumps({"attempted": len(jobs), "successful": sum("result" in row for row in results), "comparison": str(args.output)}, ensure_ascii=False))
    if jobs and not any("result" in row for row in results):
        raise SystemExit("No configured vision endpoint succeeded; no descriptions fabricated")


if __name__ == "__main__":
    main()
