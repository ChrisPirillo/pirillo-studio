"""Per-output transactions for docs/rendering.md section 3; media commits before its record."""
import contextlib
import fcntl
import os
import shutil
import uuid
from pathlib import Path

from ..captions import cues
from ..cutting import plan
from ..spine.files import SpineError, canon, digest, filehash, fold, inside, safe


def path(video, name, *, writable=False):
    """Reject symlinks, case/form aliases and writable owner hardlinks before touching bytes."""
    safe(name, "rendering")
    target = inside(video.folder, name)
    parent = video.folder
    for part in Path(name).parts:
        if parent.exists():
            for child in parent.iterdir():
                if fold(child.name) == fold(part) and child.name != part:
                    raise SpineError(f"{name}: path alias of {child.name}")
        parent = parent / part
    if target.exists() and (not target.is_file() or writable and target.stat().st_nlink != 1):
        raise SpineError(f"{name}: not an isolated regular file")
    return target


def name(request):
    return f"records/render/{request['output_id']}.json"


def readings_name(request):
    return f"records/render/{request['output_id']}-readings.json"


def write(video, name, value):
    path(video, name, writable=True)
    plan.write(video, name, value)


def read(video, name):
    return plan.read(video, name) if path(video, name).exists() else None


@contextlib.contextmanager
def writer(video, output_id):
    lock_name = f"records/render/{output_id}.lock"
    target = path(video, lock_name, writable=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as handle:
        if os.fstat(handle.fileno()).st_nlink != 1:
            raise SpineError(f"{lock_name}: hardlinked lock")
        fcntl.flock(handle, fcntl.LOCK_EX)
        if target.stat().st_ino != os.fstat(handle.fileno()).st_ino:
            raise SpineError(f"{lock_name}: lock changed")
        yield


def work(video, request):
    target = path(video, request['file'], writable=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".render-{request['output_id']}-{uuid.uuid4().hex}{target.suffix}"
    return temporary.relative_to(video.folder).as_posix()


def create_work(video, request, temporary):
    # The pending record names the file before it can exist, so a killed writer leaves no orphan.
    target = own_work(video, request, temporary)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)


def own_work(video, request, temporary):
    target = path(video, temporary, writable=True)
    destination = path(video, request['file'], writable=True)
    if target.parent != destination.parent or not target.name.startswith(f".render-{request['output_id']}-"):
        raise SpineError(f"{temporary}: not this output's work file")
    if target == destination or target == path(video, request['source']):
        raise SpineError(f"{temporary}: work aliases media")
    return target


def remove_work(video, request, temporary):
    target = own_work(video, request, temporary)
    if target.exists():
        target.unlink()


def sync(video, temporary):
    target = path(video, temporary, writable=True)
    if not target.exists() or target.stat().st_size == 0:
        raise SpineError(f"{temporary}: encoder produced no media")
    with target.open('rb') as handle:
        os.fsync(handle.fileno())
    return filehash(target)


def backup_name(request, sha):
    if not isinstance(sha, str) or len(sha) != 71 or not sha.startswith('sha256:'):
        raise SpineError('previous file_sha256: missing or malformed')
    if any(c not in '0123456789abcdef' for c in sha[7:]):
        raise SpineError('previous file_sha256: malformed')
    target = Path(request['file'])
    # New sound artifacts may have identical bytes; legacy backup names stay recoverable.
    suffix = '-' + target.name if request['file'] in artifact_names(request)[4:] else target.suffix
    return (target.parent / f".render-{request['output_id']}-accepted-{sha[7:]}{suffix}").as_posix()


def artifact_names(request):
    from . import compositor
    return compositor.names(request)


def preserve(video, request, previous, *, replacing=True):
    """An accepted file stays under its content identity throughout replacement."""
    if previous is None:
        if path(video, request['file']).exists():
            raise SpineError(f"{request['file']}: existing file has no accepted render record")
        return None
    if previous['file'] != request['file'] or previous['output'] != request['output']:
        raise SpineError(f"{name(request)}: record names a different output")
    sha = previous['file_sha256']
    source = path(video, previous['file'], writable=True)
    if filehash(source) != sha:
        raise SpineError(f"{previous['file']}: accepted file hash changed")
    readings_path = path(video, previous['readings'], writable=True)
    if filehash(readings_path) != previous['readings_sha256']:
        raise SpineError(f"{previous['readings']}: accepted readings hash changed")
    readings = plan.read(video, previous['readings'])
    if readings['key'] != previous['key'] or readings['file_sha256'] != sha:
        raise SpineError(f"{previous['readings']}: accepted readings do not bind media")
    backup = backup_name(request, sha)
    destination = path(video, backup, writable=True)
    saved = {'record': previous, 'backup': backup, 'readings': readings}
    generated = previous.get('request', {}).get('generated', {})
    if generated:
        saved['artifacts'] = dict(generated)
        saved['current'] = {n: filehash(path(video, n, writable=True))
                            for n in artifact_names(request)}
    current = artifact_identities(request, saved)
    if not replacing:
        return saved
    if destination.exists():
        if filehash(destination) != sha:
            raise SpineError(f"{backup}: backup hash changed")
    else:
        # Missing generated artifacts can force replacement even when the request key matches.
        temporary = work(video, request)
        try:
            write(video, name(request), {'output': request['output'], 'state': 'rendering',
                                         'previous': saved, 'work': temporary})
            create_work(video, request, temporary)
            shutil.copyfile(source, path(video, temporary, writable=True))
            if sync(video, temporary) != sha:
                raise SpineError(f"{source}: changed while preserving")
            os.replace(path(video, temporary), destination)
        finally:
            remove_work(video, request, temporary)
    for artifact, sha in current.items():
        if sha is None:
            continue
        artifact_req = {**request, 'file': artifact}
        backup = backup_name(artifact_req, sha)
        if filehash(path(video, backup)) == sha:
            continue
        temporary = work(video, artifact_req)
        try:
            write(video, name(request), {'output': request['output'], 'state': 'rendering',
                                         'previous': saved, 'artifact_work': [artifact, temporary]})
            create_work(video, artifact_req, temporary)
            shutil.copyfile(path(video, artifact), path(video, temporary, writable=True))
            if sync(video, temporary) != sha:
                raise SpineError(f'{artifact}: changed while preserving')
            os.replace(path(video, temporary), path(video, backup, writable=True))
        finally:
            remove_work(video, artifact_req, temporary)
    return saved


def restore(video, request, previous):
    if previous is None:
        return
    accepted = previous['record']
    artifacts = artifact_identities(request, previous)
    sha = accepted['file_sha256']
    if previous['backup'] != backup_name(request, sha):
        raise SpineError('previous backup: wrong content identity')
    backup = path(video, previous['backup'], writable=True)
    target = path(video, request['file'], writable=True)
    if filehash(target) != sha:
        if filehash(backup) != sha:
            raise SpineError(f"{previous['backup']}: missing or changed accepted backup")
        os.replace(backup, target)
    if accepted['readings'] != readings_name(request):   # never write a path a record names, only this output's own
        raise SpineError(f"previous record: readings {accepted['readings']!r} are not {readings_name(request)}")
    readings = previous['readings']
    if digest(canon(readings) + b'\n') != accepted['readings_sha256']:
        raise SpineError('previous readings: hash changed')
    if filehash(path(video, accepted['readings'])) != accepted['readings_sha256']:
        write(video, accepted['readings'], readings)

    # Captions and rollback share the existing video writer across the check and rename.
    with cues.writer(video):
        for artifact, sha in artifacts.items():
            target = path(video, artifact, writable=True)
            if filehash(target) == sha:
                continue
            if sha is None:
                target.unlink()
                continue
            backup = path(video, backup_name({**request, 'file': artifact}, sha), writable=True)
            if filehash(backup) != sha:
                raise SpineError(f'{artifact}: missing or changed attempt-start backup')
            os.replace(backup, target)


def artifact_identities(request, previous):
    """Keep accepted identities; roll back to the separately saved attempt-start bytes."""
    record = previous['record']
    if record['file'] != request['file'] or record['output'] != request['output']:
        raise SpineError('previous record: wrong output')
    artifacts = previous.get('artifacts', {})
    accepted = record.get('request', {}).get('generated', {})
    names = artifact_names(request)
    # Seam 2e recorded voice, caption JSON and SRT, before burn became a generated artifact.
    if (not isinstance(artifacts, dict) or artifacts != accepted
            or artifacts and set(artifacts) not in (set(names), set(names[:4]), set(names[:3]))):
        raise SpineError('previous generated artifacts: wrong accepted identities or output names')
    current = previous.get('current', {})
    allowed = (set(names),) if set(artifacts) == set(names) else (set(names), set(names[:4]))
    if (not isinstance(current, dict) or (set(current) not in allowed if artifacts else bool(current))
            or previous.get('unavailable')):
        raise SpineError('previous current artifacts: missing identities or wrong output names')
    for identities in (artifacts, current):
        for artifact, sha in identities.items():
            if sha is not None:
                backup_name({**request, 'file': artifact}, sha)
    return current


def recover(video, request, record):
    if record is None:
        return None
    if record['output'] != request['output']:
        raise SpineError(f"{name(request)}: wrong output identity")
    if record['state'] in {'delivered', 'waiting_owner'} and 'file_sha256' in record:
        if 'retiring' in record:
            retire(video, request, record['retiring'])
            record = {k: v for k, v in record.items() if k != 'retiring'}
            write(video, name(request), record)
        return record
    previous = record.get('previous')
    if record['state'] in {'render_failed', 'measure_refused'}:
        retire(video, request, previous)  # restored before this record was written; again would undo a provider's fix
    else:
        restore(video, request, previous)
    if 'artifact_work' in record:
        artifact, temporary = record['artifact_work']
        if artifact not in artifact_names(request):
            raise SpineError('previous artifact work: wrong output name')
        remove_work(video, {**request, 'file': artifact}, temporary)
    if 'work' in record:
        remove_work(video, request, record['work'])
    # With no predecessor, an interrupted rename is owned only by its recorded measured hash.
    target = path(video, request['file'], writable=True)
    if previous is None and record.get('candidate_sha256') is not None and target.exists():
        if filehash(target) != record['candidate_sha256']:
            raise SpineError(f"{request['file']}: interrupted candidate hash changed")
        target.unlink()
    return previous['record'] if previous is not None else None


def publish(video, request, temporary):
    source = own_work(video, request, temporary)
    target = path(video, request['file'], writable=True)
    if target == path(video, request['source']):
        raise SpineError(f"{request['file']}: cannot overwrite supplied source")
    os.replace(source, target)
    fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def retire(video, request, previous):
    if previous is not None:
        artifacts = artifact_identities(request, previous)
        backup = previous['backup']
        if backup != backup_name(request, previous['record']['file_sha256']):
            raise SpineError('previous backup: wrong content identity')
        remove_work(video, request, backup)
        for artifact, sha in artifacts.items():
            if sha is not None:
                artifact_req = {**request, 'file': artifact}
                remove_work(video, artifact_req, backup_name(artifact_req, sha))
