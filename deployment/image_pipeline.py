#!/usr/bin/env python3
"""Shared, serialized full-image stage for daily reports and live preprocessing."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

import settings
ROOT = settings.ROOT
WORK = settings.RUNTIME / "digest"


def _private_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.image-stage-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        project = settings.load_project()
        os.chown(temporary, project["worker_uid"], project["worker_gid"])
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _last_object(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError('Image stage returned no structured result')


def _run_tool(image: str, script: str, arguments: list[str], *, network: str, timeout: int):
    project = settings.load_project(required=True)
    name = 'wechat-image-stage-' + uuid.uuid4().hex[:12]
    command = ['docker', 'run', '--rm', '--pull', 'never', '--name', name, '--network', network,
               '--read-only', '--user', f"{project['worker_uid']}:{project['worker_gid']}", '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--memory', '1g', '--cpus', '1', '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777',
               '--env', 'PYTHONPATH=' + ('/ocr-deps:' if script == 'ocr_images.py' and (settings.RUNTIME / 'ocr-deps').is_dir() else '') + '/source/memory:/source/agent_console:/tool:/tool/vendor',
               '--env', 'TZ=' + project['timezone']]
    command += ['--env', 'WECHAT_DIGEST_CONFIG=/runtime/project.json']
    for source, target, readonly in [
        (ROOT / 'deployment', '/tool', True), (ROOT / 'memory', '/source/memory', True),
        (ROOT / 'agent_console', '/source/agent_console', True),
        (settings.RUNTIME / 'project.json', '/runtime/project.json', True),
        (settings.RUNTIME / 'keyscan/all_keys.json', '/keys/all_keys.json', True),
        (settings.RUNTIME / 'selection.json', '/runtime/selection.json', True), (WORK, '/work', False),
        (settings.RUNTIME / 'gemini-model.json', '/models/gemini.json', True),
        (settings.RUNTIME / 'vision-qwen.json', '/models/qwen.json', True)]:
        command += ['--mount', f'type=bind,src={source},dst={target}' + (',readonly' if readonly else '')]
    if script in {'prepare-images.py', 'web_context.py'}:
        selection = json.loads((settings.RUNTIME / 'selection.json').read_text(encoding='utf-8'))
        selected = selection.get('source_db_dir')
        if not isinstance(selected, str) or not selected.startswith('/config/'):
            raise ValueError('selected source database is not a configured profile path')
        relative = Path(selected.removeprefix('/config/'))
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('selected source database path is unsafe')
        source_db = (settings.RUNTIME / 'gui' / relative).resolve(strict=True)
        gui_root = (settings.RUNTIME / 'gui').resolve(strict=True)
        if not source_db.is_relative_to(gui_root) or not source_db.is_dir():
            raise ValueError('selected source database is outside the configured profile')
        command += ['--mount', f'type=bind,src={gui_root},dst=/config,readonly']
    if script == 'ocr_images.py' and (settings.RUNTIME / 'ocr-deps').is_dir():
        command += ['--mount', f'type=bind,src={settings.RUNTIME / "ocr-deps"},dst=/ocr-deps,readonly']
    command += [image, '/tool/' + script, *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except BaseException:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
        raise
    return result


def _container_path(path: Path) -> str:
    return str(Path('/work') / path.relative_to(WORK))


def process_window(start: int, end: int, *, max_images: int = 0, wait_for_lock: bool = True,
                   scope: str = 'daily') -> dict:
    if type(start) is not int or type(end) is not int or start >= end or max_images < 0:
        raise ValueError('Invalid image processing window or batch size')
    if scope not in {'daily', 'realtime', 'on-demand'}:
        raise ValueError('Invalid image processing scope')
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (WORK / 'image-stage.lock').open('a+') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait_for_lock else fcntl.LOCK_NB))
        except BlockingIOError:
            return {'status': 'busy', 'counts': {}, 'pending_start': start, 'next_attempt_at': int(time.time()) + 15}
        return _process_locked(start, end, max_images, scope)


def _process_locked(start: int, end: int, max_images: int, scope: str) -> dict:
    os.umask(0o077)
    began = time.monotonic()
    now = int(time.time())
    image = settings.runtime_image('runtime')
    project = settings.load_project(required=True)
    selection = json.loads((settings.RUNTIME / 'selection.json').read_text())
    jobs = WORK / 'image-jobs'
    jobs.mkdir(mode=0o700, exist_ok=True)
    os.chown(jobs, project["worker_uid"], project["worker_gid"])
    for old in jobs.iterdir():
        age = now - old.stat().st_mtime
        if old.is_dir() and ((old.name.startswith('realtime-') and age > 600)
                             or (old.name.startswith('daily-') and age > 7 * 86400)
                             or (old.name.startswith('on-demand-') and age > 30 * 86400)):
            shutil.rmtree(old)
    job = jobs / f'{scope}-{now}-{uuid.uuid4().hex[:8]}'
    job.mkdir(mode=0o700)
    os.chown(job, project["worker_uid"], project["worker_gid"])
    manifest_path = job / 'image-manifest.json'
    request_path = job / 'original-download-request.json'
    output_path = job / 'image-understanding.json'
    exclusions = []
    notice_hashes = []
    for path in (WORK / 'sent-receipts').glob('*.json'):
        receipt = json.loads(path.read_text())
        if receipt.get('status') == 'sent' and receipt.get('chat_id') == project['group']['id'] and receipt.get('server_id'):
            exclusions.append(str(receipt['server_id']))
    for path in (WORK / 'on-demand-receipts').glob('*.json'):
        receipt = json.loads(path.read_text())
        if receipt.get('status') == 'sent' and receipt.get('chat_id') == project['group']['id'] and receipt.get('server_id'):
            exclusions.append(str(receipt['server_id']))
    for path in (WORK / 'site-access-receipts').glob('*.json'):
        receipt = json.loads(path.read_text())
        if receipt.get('chat_id') != project['group']['id']:
            continue
        if receipt.get('text_sha256'):
            notice_hashes.append(receipt['text_sha256'])
        if receipt.get('status') == 'sent' and receipt.get('server_id'):
            exclusions.append(str(receipt['server_id']))
    notice_hashes_path = job / 'exclude-own-text-sha256.json'
    _private_json(notice_hashes_path, notice_hashes)
    exclude_path = job / 'exclude-server-ids.json'
    _private_json(exclude_path, exclusions)
    arguments = ['--source-db-dir', selection['source_db_dir'], '--runtime-dir', '/work', '--start', str(start),
                 '--end', str(end), '--manifest', _container_path(manifest_path),
                 '--download-request', _container_path(request_path), '--exclude-server-ids', _container_path(exclude_path)]
    timings = {}
    t = time.monotonic()
    prepared = _run_tool(image, 'prepare-images.py', arguments, network='none', timeout=240)
    timings['prepare_seconds'] = round(time.monotonic() - t, 3)
    if prepared.returncode:
        raise RuntimeError('Full-image preparation failed; private source data was not discarded')
    manifest = json.loads(manifest_path.read_text())
    request = json.loads(request_path.read_text())
    ledger_path = WORK / 'original-download-failures.json'
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    downloader_version = hashlib.sha256((ROOT / 'deployment/fetch-originals.py').read_bytes()).hexdigest()
    fingerprints = {target['message_uid']: hashlib.sha256((downloader_version + target.get('descriptor_sha256', '')).encode()).hexdigest()
                    for target in request['targets']}
    for item in manifest:
        if item.get('available') and not item.get('download_required'):
            ledger.pop(item['message_uid'], None)
    due = [target for target in request['targets'] if ledger.get(target['message_uid'], {}).get('fingerprint') != fingerprints[target['message_uid']]
           or float(ledger.get(target['message_uid'], {}).get('retry_at', 0)) <= now]
    if max_images:
        due = due[:max_images]
    if sum(int(target.get('expected_length') or 0) for target in due) >= 1024 ** 3:
        raise RuntimeError('Large-download network prerequisite is required for this image batch')
    spec = importlib.util.spec_from_file_location("original_fetch_stage", ROOT / "deployment/fetch-originals.py")
    original_fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original_fetch)
    t = time.monotonic()
    for target in due:
        single_path = job / 'one-original-request.json'
        _private_json(single_path, {**request, 'targets': [target]})
        uid = target['message_uid']
        try:
            original_fetch.host(single_path)
        except Exception as exc:
            prior = ledger.get(uid, {})
            attempts = int(prior.get('attempts', 0)) + 1 if prior.get('fingerprint') == fingerprints[uid] else 1
            reason = type(exc).__name__
            try:
                message = json.loads(str(exc)).get('error', '')
                if isinstance(message, str) and message.startswith(('CDN ', 'Downloaded ', 'Invalid CDN', 'Missing ', 'Unsupported ', 'Original-image ', 'Image message ', 'Requested message ', 'Not every requested ', 'All configured CDN')):
                    reason = message[:200]
            except (ValueError, AttributeError, TypeError):
                pass
            ledger[uid] = {'attempts': attempts, 'retry_at': int(time.time()) + min(3600, 30 * 2 ** min(attempts - 1, 7)),
                           'category': 'original_download_failed', 'reason': reason, 'fingerprint': fingerprints[uid]}
        else:
            ledger.pop(uid, None)
        _private_json(ledger_path, ledger)
    timings['download_seconds'] = round(time.monotonic() - t, 3)
    if due:
        t = time.monotonic()
        prepared = _run_tool(image, 'prepare-images.py', arguments, network='none', timeout=240)
        timings['reprepare_seconds'] = round(time.monotonic() - t, 3)
        if prepared.returncode:
            raise RuntimeError('Downloaded originals could not be verified')
        manifest = json.loads(manifest_path.read_text())
        request = json.loads(request_path.read_text())
    caption_manifest = []
    for item in manifest:
        if item.get('download_required'):
            caption_manifest.append({**item, 'available': False, 'reason': 'The advertised full image is pending retrieval'})
        else:
            caption_manifest.append(item)
    caption_manifest_path = job / 'caption-manifest.json'
    _private_json(caption_manifest_path, caption_manifest)
    def timed_tool(script, args, network, timeout):
        stamp = time.monotonic()
        result = _run_tool(image, script, args, network=network, timeout=timeout)
        timings[script.removesuffix('.py') + '_seconds'] = round(time.monotonic() - stamp, 3)
        if result.returncode not in (0, 2):
            _private_json(job / (script + '.failure.json'), {'returncode': result.returncode, 'stderr': result.stderr})
            raise RuntimeError(script + ' did not finish successfully')
        return result

    ocr_path, web_path, summary_path = (job / name for name in ('ocr.json', 'web.json', 'web-summary.json'))
    with ThreadPoolExecutor(max_workers=3) as pool:
        caption_future = pool.submit(timed_tool, 'caption-images.py', [
            '--manifest', _container_path(caption_manifest_path), '--output', _container_path(output_path),
            '--cache-output', '/work/vision-cache.json', '--failures', '/work/vision-failures.json',
            '--diagnostics-dir', '/work/vision-diagnostics', '--max-images', str(max_images)], project['report_prefix'] + '_default', 900)
        ocr_future = pool.submit(timed_tool, 'ocr_images.py', [
            '--manifest', _container_path(caption_manifest_path), '--output', _container_path(ocr_path),
            '--cache-dir', '/work/ocr-cache', '--max-images', str(max_images)], 'none', 900)
        web_future = pool.submit(timed_tool, 'web_context.py', [
            '--source-db-dir', selection['source_db_dir'], '--start', str(start), '--end', str(end),
            '--output', _container_path(web_path), '--max-pages', str(4 if scope == 'realtime' else 0)], project['report_prefix'] + '_default', 900)
        ocr_future.result()
        web_future.result()
    timed_tool('web_summary.py', ['--input', _container_path(web_path), '--output', _container_path(summary_path),
               '--max-pages', str(2 if scope == 'realtime' else 0)], project['report_prefix'] + '_default', 900)
    auxiliary = {}
    auxiliary_pending = False
    for kind, path in [('ocr', ocr_path), ('web', summary_path)]:
        for uid, entry in json.loads(path.read_text()).items():
            auxiliary.setdefault(uid, {})[kind] = entry
            if entry.get('status') in ('pending', 'failed'):
                auxiliary_pending = True
    auxiliary_path = job / 'auxiliary-context.json'
    _private_json(auxiliary_path, auxiliary)
    info = _last_object(captioned.stdout)
    if not output_path.is_file():
        raise RuntimeError('Image caption worker did not persist its results')
    understood = json.loads(output_path.read_text())
    pending_items = [item for item in manifest if understood.get(item['message_uid'], {}).get('status') != 'success']
    counts = {'total': len(manifest), **{status: sum(row.get('status') == status for row in understood.values())
                                      for status in ('success', 'failed', 'pending', 'unavailable')}}
    future = [float(info['next_attempt_at'])] if isinstance(info.get('next_attempt_at'), (int, float)) and info['next_attempt_at'] > time.time() else []
    undownloaded = {target['message_uid'] for target in request['targets']}
    for item in pending_items:
        uid = item['message_uid']
        if uid in undownloaded:
            future.append(float(ledger.get(uid, {}).get('retry_at') or time.time() + 1))
        elif understood.get(uid, {}).get('status') == 'pending' and not future:
            future.append(time.time() + 1)
    status = 'complete' if not pending_items and not auxiliary_pending else 'pending'
    result = {'status': status, 'start': start, 'end': end, 'scope': scope, 'counts': counts,
              'manifest_path': str(manifest_path), 'understanding_path': str(output_path),
              'auxiliary_path': str(auxiliary_path), 'auxiliary_pending': auxiliary_pending,
              'notice_hashes_path': str(notice_hashes_path),
              'exclude_path': str(exclude_path), 'pending_start': start if auxiliary_pending else min((item['timestamp'] for item in pending_items), default=None),
              'next_attempt_at': int(min(future)) if future else (int(time.time()) + 60 if pending_items or auxiliary_pending else None),
              'timings': {**timings, 'total_seconds': round(time.monotonic() - began, 3)}}
    _private_json(job / 'result.json', result)
    _private_json(WORK / f'{scope}-images-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', type=int, required=True)
    parser.add_argument('--end', type=int, required=True)
    parser.add_argument('--scope', choices=('daily', 'realtime', 'on-demand'), default='daily')
    parser.add_argument('--max-images', type=int, default=0)
    parser.add_argument('--no-wait', action='store_true')
    args = parser.parse_args()
    try:
        result = process_window(args.start, args.end, max_images=args.max_images, wait_for_lock=not args.no_wait, scope=args.scope)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'complete' else 2


if __name__ == '__main__':
    raise SystemExit(main())
