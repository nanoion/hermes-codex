"""Bounded, no-symlink attachment intake and immutable queued snapshots."""
import hashlib
import os
import stat
from pathlib import Path

from .state import Conflict, Denied, encoded

KINDS = {'photo', 'image', 'document', 'audio', 'voice', 'video', 'animation', 'sticker', 'video_note'}
MAX_BYTES = 20 * 1024 * 1024


def scoped_bytes(path, root):
    """Walk beneath an authorized root using dirfds; reject symlinks at every step."""
    path, root = Path(path), Path(root)
    if not path.is_absolute() or '..' in path.parts:
        raise Denied('Attachment path must be absolute without traversal')
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise Denied('Attachment must be in the assigned workspace') from None
    if not parts:
        raise ValueError('Attachment must be a file')
    parent = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (*root.parts[1:], *parts[:-1]):
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = new
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError('Attachment must be a regular file no larger than 20 MiB')
            data = source.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError('Attachment exceeds 20 MiB')
            return data
    finally:
        os.close(parent)


def stage(service, owner, p):
    from .controls import text
    task = service.store.task(owner, p['task'])
    key = text(p['key'], 'Attachment key', 200)
    if p['kind'] not in KINDS:
        raise ValueError('Unsupported attachment kind')
    album = text(p.get('album', key), 'Album', 200)
    caption = p.get('caption', '')
    if not isinstance(caption, str) or len(caption) > 4000:
        raise ValueError('Caption must be at most 4000 characters')
    candidates = [task['workspace'], *service.attachment_roots]
    source_root = next((r for r in candidates if Path(r) in Path(p['path']).parents), None)
    if source_root is None:
        raise Denied('Attachment is outside the workspace and configured ingress roots')
    data = scoped_bytes(p['path'], source_root)
    digest = hashlib.sha256(data).hexdigest()
    batch_id = hashlib.sha256(encoded([task['id'], album]).encode()).hexdigest()
    file_id = hashlib.sha256(encoded([task['id'], key]).encode()).hexdigest()
    signature = hashlib.sha256(encoded([digest, p['kind'], caption]).encode()).hexdigest()
    try:
        previous = service.store.get(owner, 'attachment-key', file_id)
    except Denied:
        previous = None
    if previous:
        if previous['signature'] != signature or previous['batch'] != batch_id:
            raise Conflict('Attachment key already binds different media')
        return service.store.get(owner, 'attachment', batch_id)
    try:
        batch = service.store.get(owner, 'attachment', batch_id)
    except Denied:
        batch = {'id': batch_id, 'task': task['id'], 'album': album, 'state': 'staged', 'files': []}
    if batch['state'] != 'staged' or len(batch['files']) >= 20:
        raise Conflict('Album is consumed/cleared or has reached 20 files')
    directory = service.home / 'attachments' / hashlib.sha256(owner.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / (file_id + '-' + digest)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600) if not destination.exists() else None
    if fd is not None:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    elif scoped_bytes(str(destination), str(directory)) != data:
        raise Conflict('Staged attachment differs from its immutable digest')
    native_image = p['kind'] in {'image', 'photo'} and (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff') or data.startswith((b'GIF87a', b'GIF89a')))
    if p['kind'] in {'image', 'photo'} and not native_image:
        raise ValueError('Native image needs a recognized PNG/JPEG/GIF header')
    batch['files'].append({'id': file_id, 'path': str(destination), 'sha256': digest, 'kind': p['kind'], 'caption': caption, 'native_image': native_image, 'size': len(data)})
    with service.store.transaction():
        service.store.put(owner, 'attachment', batch_id, batch)
        service.store.put(owner, 'attachment-key', file_id, {'signature': signature, 'batch': batch_id})
    return batch


def consume(service, owner, task, queue_id, batch_ids=None):
    """Call inside the same transaction that inserts the queue record."""
    files = []
    if batch_ids is not None:
        for key in batch_ids:
            batch = service.store.get(owner, 'attachment', key)
            if batch['task'] != task or batch['state'] != 'staged':
                raise Conflict('Selected attachment batch is unavailable')
    for batch in service.store.list(owner, 'attachment'):
        if batch['task'] == task and batch['state'] == 'staged' and (batch['id'] in batch_ids if batch_ids is not None else batch.get('use_next', True)):
            files.extend(batch['files'])
            batch.update(state='queued', queue=queue_id)
            service.store.put(owner, 'attachment', batch['id'], batch)
    if len(files) > 20:
        raise ValueError('A message supports at most 20 staged attachments')
    return files


def inputs(files):
    result = []
    for file in files:
        data = scoped_bytes(file['path'], str(Path(file['path']).parent))
        if hashlib.sha256(data).hexdigest() != file['sha256']:
            raise Conflict('Queued media changed; refusing to send it')
        if file['native_image']:
            result.append({'type': 'localImage', 'path': file['path']})
        else:
            result.append({'type': 'text', 'text': f"Attached {file['kind']} file: {file['path']}. Native transcription/video understanding is not implied."})
        if file['caption']:
            result.append({'type': 'text', 'text': file['caption']})
    return result
