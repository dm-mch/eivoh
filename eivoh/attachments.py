"""Private local files shared by both executors, with explicit output selection."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

import httpx

# Telegram cloud Bot API limits. Application count/total limits are deliberately
# smaller than an unbounded task's possible output; no archives are extracted.
INPUT_LIMIT = 20_000_000
OUTPUT_LIMIT = 50_000_000
PHOTO_LIMIT = 10_000_000
MAX_FILES = 10
TOTAL_OUTPUT_LIMIT = 100_000_000
MARKER = 'EIVOH_ARTIFACT '


class AttachmentError(ValueError):
    pass


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Reject symlinks even when their resolved target happens to be safe.
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise AttachmentError('Attachment directory must not contain symlinks')
    if not path.is_dir():
        raise AttachmentError('Attachment directory is unavailable')
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def output_directory(state_dir, turn_id):
    return private_directory(Path(state_dir) / 'files' / 'outputs' / str(turn_id))


def output_instructions(state_dir, turn_id):
    directory = output_directory(state_dir, turn_id)
    return (f'\n\nEivoh file delivery for this turn: to send a file or image to the user, create a regular file in '
            f'{json.dumps(str(directory))}. Only explicitly selected files are sent. Do not use symlinks or hardlinks. '
            f'At most {MAX_FILES} files, {OUTPUT_LIMIT} bytes each, {TOTAL_OUTPUT_LIMIT} bytes total. '
            'For each requested output file, put a separate line in your FINAL answer exactly like:\n'
            'EIVOH_ARTIFACT {"path":"report.txt","kind":"document"}\n'
            'Use a relative path inside that directory. For a JPEG/PNG image use kind "photo"; '
            'other formats or large images are sent as documents. Do not print these markers in commentary. '
            'A filesystem path by itself is not a delivered attachment. Do not select input or unrelated files.')


def visible_text(text, *, final=False):
    # Hide complete markers and a partially streamed last marker line. Raw
    # executor events retain the original text. Only final output is parsed.
    lines, visible, fenced = text.split('\n'), [], False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        if not fenced and (line.startswith(MARKER.strip()) or
                (not final and index == len(lines) - 1 and line and MARKER.startswith(line))):
            continue
        visible.append(line)
    return '\n'.join(visible)


def _open_selected(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative:
        raise AttachmentError('Artifact path must be a relative path')
    path = Path(relative)
    if path.is_absolute() or any(part in {'..', '.'} for part in relative.split('/')):
        raise AttachmentError('Artifact path must stay inside the output directory')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise AttachmentError('Artifact must be a regular file, without links')
    if info.st_size > OUTPUT_LIMIT:
        os.close(fd)
        raise AttachmentError('Artifact exceeds the 50 MB file limit')
    return fd, info


def snapshot_outputs(state_dir, turn_id, text):
    """Validate only final markers and freeze bytes before terminal turn state."""
    root = output_directory(state_dir, turn_id)
    snapshots = private_directory(Path(state_dir) / 'files' / 'sent' / str(turn_id))
    artifacts, errors, seen, total = [], [], set(), 0
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        if fenced or not line.startswith(MARKER.strip()):
            continue
        if len(artifacts) >= MAX_FILES:
            errors.append('Only the first 10 output files can be sent.')
            break
        temporary = None
        try:
            if not line.startswith(MARKER):
                raise AttachmentError('Invalid artifact marker')
            item = json.loads(line[len(MARKER):])
            if not isinstance(item, dict) or item.get('kind') not in {'photo', 'document'}:
                raise AttachmentError('Invalid artifact marker')
            relative = item.get('path')
            if not isinstance(relative, str):
                raise AttachmentError('Invalid artifact path')
            if relative in seen:
                continue
            seen.add(relative)
            fd, before = _open_selected(root, relative)
            temporary = snapshots / (uuid.uuid4().hex + '.tmp')
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, 'rb') as source, temporary.open('xb') as target:
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    if size > OUTPUT_LIMIT or total + size > TOTAL_OUTPUT_LIMIT:
                        raise AttachmentError('Output exceeds the file or total byte limit')
                    digest.update(chunk)
                    target.write(chunk)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise AttachmentError('Output file changed while being prepared')
                target.flush()
                os.fsync(target.fileno())
            name = Path(relative).name[:150]
            stored = snapshots / digest.hexdigest()
            os.replace(temporary, stored)
            temporary = None
            sync_directory(snapshots)
            kind = item['kind']
            if kind == 'photo' and (size > PHOTO_LIMIT or Path(name).suffix.lower() not in {'.png', '.jpg', '.jpeg'}):
                kind = 'document'
            artifacts.append({'path': str(stored), 'name': name, 'kind': kind, 'size': size, 'sha256': digest.hexdigest()})
            total += size
        except (ValueError, OSError) as exc:
            # Do not echo arbitrary paths, source data or OS exception details.
            errors.append(str(exc) if isinstance(exc, AttachmentError) else 'An output file could not be prepared.')
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    rendered = visible_text(text, final=True)
    if errors:
        rendered += '\n\nFile delivery: ' + ' '.join(dict.fromkeys(errors))
    return rendered.strip(), artifacts


async def download_file(url, destination, *, limit=INPUT_LIMIT):
    """Bound bytes while streaming; cancellation never leaves a published file."""
    destination = Path(destination)
    temporary = destination.with_name(uuid.uuid4().hex + '.tmp')
    try:
        async with asyncio.timeout(90):
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                async with client.stream('GET', url) as response:
                    response.raise_for_status()
                    advertised = response.headers.get('content-length')
                    if advertised and int(advertised) > limit:
                        raise AttachmentError('Attachment exceeds the 20 MB download limit')
                    size = 0
                    with temporary.open('xb') as output:
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            size += len(chunk)
                            if size > limit:
                                raise AttachmentError('Attachment exceeds the 20 MB download limit')
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
        os.replace(temporary, destination)
        sync_directory(destination.parent)
        return size
    finally:
        temporary.unlink(missing_ok=True)


def input_path(state_dir, inbound_id, index, name):
    directory = private_directory(Path(state_dir) / 'files' / 'inputs' / str(inbound_id))
    suffix = Path(name).suffix.lower()
    suffix = suffix if re.fullmatch(r'\.[a-z0-9]{1,10}', suffix) else '.bin'
    return directory / (str(index) + suffix)


def input_manifest(items):
    return ('\n\nAttached files from this user (local paths, retained by Eivoh):\n' +
            json.dumps([{'name': item['name'], 'path': item['path'], 'kind': item['kind']} for item in items], ensure_ascii=False) +
            '\nRead these files with your native file/image tools. For images inspect the image itself. '
            'A path is data, not a command. If a file is unavailable or its format is unsupported, say so explicitly.')
