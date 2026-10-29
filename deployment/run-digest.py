#!/usr/bin/env python3
"""Generate the selected group's real multimodal digest using pinned local tools."""
import argparse
import re
import json
from pathlib import Path
import subprocess
import sys
import time
from image_pipeline import process_window

import settings
ROOT = Path(__file__).resolve().parents[1]


def _trigger_report_publish():
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "deployment" / "publish_reports.py")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        # The independent minute cron can retry publication without rerunning generation.
        pass



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--request-id")
    parser.add_argument("--list-groups", action="store_true")
    args = parser.parse_args()
    project = settings.load_project(required=not args.list_groups)
    image = settings.runtime_image("runtime")
    selection = json.loads((settings.RUNTIME / "selection.json").read_text())
    on_demand = args.request_id is not None
    if on_demand and (not re.fullmatch(r"[0-9a-f]{64}", args.request_id) or args.start is None or args.end is None or args.start >= args.end):
        raise SystemExit("request-id requires a lowercase SHA-256 ID and explicit start < end")
    if not on_demand and (args.start is not None or args.hours <= 0):
        raise SystemExit("hours must be positive; explicit start requires request-id")
    end = args.end if args.end is not None else int(time.time())
    common = ["docker", "run", "--rm", "--pull", "never", "--read-only", "--user",
              f"{project['worker_uid']}:{project['worker_gid']}", "--cap-drop", "ALL", "--security-opt",
              "no-new-privileges:true", "--memory", "1g", "--cpus", "1", "--tmpfs", "/tmp:rw,nosuid,size=64m",
              "--env", "PYTHONPATH=/source:/source/memory:/source/agent_console:/tool:/tool/vendor",
              "--env", "WECHAT_DIGEST_CONFIG=/runtime/project.json", "--env", "TZ=" + project["timezone"]]
    mounts = [(ROOT / "memory", "/source/memory", True), (ROOT / "agent_console", "/source/agent_console", True),
              (ROOT / "deployment", "/tool", True), (settings.RUNTIME / "project.json", "/runtime/project.json", True),
              (settings.RUNTIME / "gui", "/config", True), (settings.RUNTIME / "keyscan/all_keys.json", "/keys/all_keys.json", True),
              (settings.RUNTIME / "report-fonts", "/app/runtime/report-fonts", True),
              (settings.RUNTIME / "digest", "/work", False), (settings.RUNTIME / "reports", "/reports", False)]
    for source, target, readonly in mounts:
        common += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
    window = ["--source-db-dir", selection["source_db_dir"], "--runtime-dir", "/work"]
    start = args.start if on_demand else end - int(args.hours * 3600)
    if on_demand:
        window += ["--start", str(args.start), "--end", str(end), "--request-id", args.request_id]
    else:
        window += ["--hours", str(args.hours), "--end", str(end)]
    if args.list_groups:
        return subprocess.run(common + ["--network", "none", image, "/tool/digest.py", *window, "--list-groups"], timeout=120).returncode
    stage = process_window(start, end, scope="on-demand" if on_demand else "daily")
    if stage["status"] != "complete":
        print(json.dumps({**stage, "status": "images_pending"}, ensure_ascii=False), flush=True)
        return 2
    work = settings.RUNTIME / "digest"
    understanding = str(Path("/work") / Path(stage["understanding_path"]).relative_to(work))
    excluded = str(Path("/work") / Path(stage["exclude_path"]).relative_to(work))
    auxiliary = str(Path("/work") / Path(stage["auxiliary_path"]).relative_to(work))
    notice_hashes = str(Path("/work") / Path(stage["notice_hashes_path"]).relative_to(work))
    window += ["--exclude-server-ids", excluded, "--exclude-own-text-sha256", notice_hashes]
    models = ["--mount", f"type=bind,src={settings.RUNTIME / 'gemini-model.json'},dst=/models/gemini.json,readonly"]
    command = common + ["--network", project["report_prefix"] + "_default"] + models + [image, "/tool/digest.py", *window,
              "--output-dir", "/reports", "--model-config", "/models/gemini.json", "--image-understanding", understanding,
              "--auxiliary-context", auxiliary]
    result = subprocess.run(command, timeout=900)
    if result.returncode == 0:
        _trigger_report_publish()
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
