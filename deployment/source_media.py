from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

import settings
_SAFE_SHA = re.compile(r"^[0-9a-f]{64}$")


def cache_path(reports_dir: Path) -> Path:
    return Path(reports_dir).parent / "digest" / "prepared-image-cache.json"


def decoded_root(reports_dir: Path) -> Path:
    return Path(reports_dir).parent / "digest" / "images" / "decoded"


def _mapped_image_path(raw: str, reports_dir: Path) -> Path:
    root = decoded_root(reports_dir).resolve()
    path = Path(raw)
    if path.is_absolute() and str(path).startswith("/work/"):
        path = root.parents[1] / path.relative_to("/work")
    elif not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=True)
    if resolved.parent != root or not resolved.is_file():
        raise ValueError("cached source image path escapes decoded media root")
    return resolved


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _web_image_extension(path: Path) -> str | None:
    # These are already byte-verified images from the preparation pipeline.
    with path.open('rb') as source:
        header = source.read(12)
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if header.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return '.webp'
    return None


def _local_png_convert(src: Path, destination: Path) -> None:
    from PIL import Image
    with Image.open(src) as image:
        if image.mode in ('1', 'L', 'LA', 'P', 'RGB', 'RGBA', 'I', 'I;16'):
            image.save(destination, format='PNG')
        else:
            with image.convert('RGB') as rgb:
                rgb.save(destination, format='PNG')


def _convert_to_png(src: Path, destination: Path, project_root: Path) -> None:
    try:
        _local_png_convert(src, destination)
        return
    except ImportError:
        pass
    # Use the bootstrapped local image, whose Python dependencies are baked in.
    image = settings.runtime_image('runtime', project_root)
    project = settings.load_project(root=project_root)
    os.chown(src, project['worker_uid'], project['worker_gid'])
    os.chown(destination, project['worker_uid'], project['worker_gid'])
    name = 'wechat-source-png-' + destination.name.lstrip('.')
    command = ['docker', 'run', '--rm', '--pull', 'never', '--name', name, '--network', 'none',
               '--read-only', '--user', f"{project['worker_uid']}:{project['worker_gid']}", '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true', '--memory', '1g', '--cpus', '1',
               '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m', '--env', 'PYTHONPATH=/tool',
               '--env', 'WECHAT_DIGEST_CONFIG=/runtime/project.json']
    for path, target, readonly in [(src, '/input', True), (destination, '/output.png', False),
                                   (project_root / 'deployment', '/tool', True),
                                   (project_root / 'runtime/project.json', '/runtime/project.json', True)]:
        command += ['--mount', f'type=bind,src={path},dst={target}' + (',readonly' if readonly else '')]
    command += [image, '-c', "from pathlib import Path; import source_media; source_media._local_png_convert(Path('/input'), Path('/output.png'))"]
    try:
        subprocess.run(command, capture_output=True, check=True, timeout=120)
    except BaseException:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
        raise


def _copy_verified(src: Path, expected_sha: str, assets_dir: Path, project_root: Path) -> str:
    if not isinstance(expected_sha, str) or not _SAFE_SHA.fullmatch(expected_sha):
        raise ValueError("cached source image has invalid SHA-256")
    source_digest = hashlib.sha256()
    fd, temp_name = tempfile.mkstemp(prefix=".source-image-", dir=assets_dir)
    try:
        with os.fdopen(fd, "wb") as out:
            with src.open("rb") as inp:
                for block in iter(lambda: inp.read(1024 * 1024), b""):
                    source_digest.update(block)
                    out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if source_digest.hexdigest() != expected_sha:
            raise ValueError("cached source image SHA-256 mismatch")
        ext = _web_image_extension(Path(temp_name))
        final_sha = expected_sha
        if ext is None:
            fd, converted = tempfile.mkstemp(prefix=".source-image-png-", dir=assets_dir)
            os.close(fd)
            try:
                _convert_to_png(Path(temp_name), Path(converted), project_root)
                final_sha = _hash_file(Path(converted))
                ext = ".png"
                os.unlink(temp_name)
                temp_name = converted
            except BaseException:
                Path(converted).unlink(missing_ok=True)
                raise
        name = final_sha + ext
        target = assets_dir / name
        if target.exists():
            if target.is_symlink() or not target.is_file() or _hash_file(target) != final_sha:
                raise ValueError("existing source image asset does not match content hash")
            os.unlink(temp_name)
        else:
            os.replace(temp_name, target)
        return name
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise



def load_cache(reports_dir: Path) -> dict:
    try:
        value = json.loads(cache_path(reports_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError('prepared image cache must be an object')
    return value


def _dependency_path(raw: str, reports_dir: Path) -> Path:
    root = decoded_root(reports_dir).resolve()
    path = Path(raw)
    if path.is_absolute() and str(path).startswith("/work/"):
        path = root.parents[1] / path.relative_to("/work")
    elif not path.is_absolute():
        path = root / path
    return path
def cached_images_by_md5(cache: dict, md5_values: list[str]) -> list[str]:
    """Return locally available full-image UIDs matching message XML fingerprints."""
    wanted = {value.lower() for value in md5_values if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{32}", value)}
    matches = []
    ranked = []
    for uid, entry in cache.items():
        item = entry.get('item') if isinstance(entry, dict) else None
        if not isinstance(item, dict) or item.get('message_uid') != uid:
            continue
        if item.get('available') is not True or item.get('source_quality') not in ('original', 'client_full'):
            continue
        if item.get('download_required') is True or not isinstance(item.get('image_path'), str) or not item.get('image_path'):
            continue
        fingerprints = {str(item.get(key) or '').lower() for key in ('md5', 'original_md5', 'client_full_md5')}
        if not wanted.intersection(fingerprints):
            continue
        quality_rank = 0 if item.get('source_quality') == 'original' else 1
        ranked.append((quality_rank, uid))
    for _, uid in sorted(ranked):
        matches.append(uid)
    return matches

def _xml_root(raw: object) -> ET.Element | None:
    if not isinstance(raw, str):
        return None
    start = raw.find('<')
    if start < 0:
        return None
    try:
        return ET.fromstring(raw[start:])
    except ET.ParseError:
        return None



def _record_image_nodes(root: ET.Element | None):
    if root is None:
        return
    for item in root.findall('.//recorditem'):
        nested = _xml_root(item.text)
        if nested is None:
            nested = item.find('recordinfo')
        if nested is not None:
            for node in nested.findall('.//dataitem'):
                if node.get('datatype') == '2':
                    yield node


def _record_image_md5s(root: ET.Element | None) -> list[str]:
    found = []
    for node in _record_image_nodes(root):
        value = (node.findtext('fullmd5') or node.get('fullmd5') or '').strip()
        if re.fullmatch(r'[0-9a-fA-F]{32}', value):
            found.append(value)
    return found


def _has_record_image(root: ET.Element | None) -> bool:
    return next(_record_image_nodes(root), None) is not None


def source_image_uids(messages: list[dict], cache: dict) -> dict[str, list[str]]:
    """Map cited messages to image candidates; publication validates cached originals."""
    by_server = {str(message.get('server_id')): message for message in messages
                 if message.get('server_id') not in (None, '')}
    by_uid = {message.get('message_uid'): message for message in messages
              if isinstance(message.get('message_uid'), str)}

    def find(message: dict, visiting: set[str]) -> list[str]:
        uid = message.get('message_uid')
        if not isinstance(uid, str) or uid in visiting:
            return []
        visiting = {*visiting, uid}
        root = _xml_root(message.get('raw_content'))
        kind = str(message.get('type') or '').lower()
        if kind in {'image', 'img', '图片'}:
            return [uid]
        md5s = []
        if root is not None:
            appmsg = root.find('.//appmsg')
            if appmsg is not None and appmsg.findtext('type') == '19':
                md5s.extend(_record_image_md5s(appmsg))
            if appmsg is not None and appmsg.findtext('type') == '57':
                refer = appmsg.find('refermsg')
                server_id = refer.findtext('svrid') if refer is not None else None
                original = by_server.get(str(server_id)) if server_id else None
                if original is not None:
                    return find(original, visiting)
        found = []
        for md5 in md5s:
            found.extend(cached_images_by_md5(cache, [md5]))
        return list(dict.fromkeys(found))

    return {uid: find(message, set()) for uid, message in by_uid.items()}

def source_image_requests(messages: list[dict]) -> dict[str, bool]:
    """Mark messages whose own or recursively quoted content requests one or more images."""
    by_uid = {message.get('message_uid'): message for message in messages
              if isinstance(message.get('message_uid'), str)}
    by_server = {str(message.get('server_id')): message for message in messages
                 if message.get('server_id') not in (None, '')}

    def requested(message: dict, visiting: set[str]) -> bool:
        uid = message.get('message_uid')
        if not isinstance(uid, str) or uid in visiting:
            return False
        visiting = {*visiting, uid}
        root = _xml_root(message.get('raw_content'))
        kind = str(message.get('type') or '').lower()
        if kind in {'image', 'img', '图片'}:
            return True
        if root is None:
            return False
        appmsg = root.find('.//appmsg')
        if appmsg is None:
            return False
        app_type = appmsg.findtext('type')
        placeholder = re.search(r'[\[【]\s*图片\s*[\]】]', str(message.get('text') or '')) is not None
        if app_type == '19':
            return _has_record_image(appmsg) or placeholder
        if app_type == '57':
            refer = appmsg.find('refermsg')
            server_id = refer.findtext('svrid') if refer is not None else None
            original = by_server.get(str(server_id)) if server_id else None
            return (requested(original, visiting) if original else False) or placeholder
        return False

    return {uid: requested(message, set()) for uid, message in by_uid.items()}




def publish_referenced_images(reports_dir: Path, refs: list[dict], assets_dir: Path, cache: dict,
                              *, verified: dict | None = None) -> tuple[dict[str, str], list[str], list[str]]:
    """Publish exact referenced images; corrupt or escaping records fail the build."""
    if assets_dir.is_symlink() or not assets_dir.is_dir():
        raise ValueError('source image assets directory must be a real directory')
    result: dict[str, str] = {}
    assets: set[str] = set()
    dependencies: set[str] = set()
    seen = set()
    if verified is None:
        verified = {}
    decoded = decoded_root(reports_dir).resolve()
    output = str(assets_dir.resolve())
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        candidates = ref.get('image_source_uids')
        if not isinstance(candidates, list):
            candidates = [ref.get('message_uid')] if ref.get('type') == 'image' else []
        for uid in candidates:
            if not isinstance(uid, str) or not uid or uid in seen:
                continue
            seen.add(uid)
            entry = cache.get(uid)
            if entry is None:
                continue
            item = entry.get('item') if isinstance(entry, dict) else None
            if not isinstance(item, dict) or item.get('message_uid') != uid:
                raise ValueError('cached source image UID does not match the cited message')
            raw = item.get('image_path')
            if isinstance(raw, str):
                dependency = _dependency_path(raw, reports_dir)
                if dependency.resolve().parent != decoded:
                    raise ValueError('cached source image dependency escapes decoded media root')
                dependencies.add(str(dependency))
            if item.get('available') is not True or item.get('source_quality') not in ('original', 'client_full'):
                continue
            if item.get('download_required') is True:
                continue
            if not isinstance(raw, str):
                raise ValueError('available source image has no local image path')
            try:
                src = _mapped_image_path(raw, reports_dir)
            except FileNotFoundError:
                continue
            info = src.stat()
            key = (str(src), item.get('sha256'), info.st_size, info.st_mtime_ns, info.st_ctime_ns, output)
            name = verified.get(key)
            if name is None or not (assets_dir / name).is_file():
                name = _copy_verified(src, item.get('sha256', ''), assets_dir, Path(reports_dir).parent.parent)
                verified[key] = name
            result[uid] = '../assets/' + name
            assets.add('assets/' + name)
    return result, sorted(dependencies), sorted(assets)
