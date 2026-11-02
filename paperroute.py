#!/usr/bin/env python3
"""Deliver selected Telegram PDFs to a local reading folder and iCloud."""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import fnmatch
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

DEFAULT_CONFIG = Path.home() / '.config/paperroute/config.json'
LABEL = 'com.paperroute.daily'


class RecoveryError(ValueError):
    """Application-defined recovery advice safe to display without account details."""


def atomic_write(path, content):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.paperroute-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, data):
    atomic_write(path, (json.dumps(data, indent=2) + '\n').encode())


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def safe_path(value):
    path = Path(value).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Choose a path without symlink components')
    return path.resolve()


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError('Create your config.json using config.example.json')
    for key in ('local_dir', 'mirror_dir'):
        value = config.get(key)
        if key == 'mirror_dir' and value is None:
            continue
        if not isinstance(value, str) or not value.strip() or not Path(value).expanduser().is_absolute():
            raise ValueError(f'{key} must be an absolute path (or start with ~)')
        config[key] = safe_path(value)
    if config.get('mirror_dir'):
        a, b = config['local_dir'], config['mirror_dir']
        if a == b or a in b.parents or b in a.parents:
            raise ValueError('Local and mirror folders must be separate, not nested')
    if type(config.get('cleanup_confirmed')) is not bool:
        raise ValueError('Set cleanup_confirmed to true or false after discussing retention')
    limit = config.get('scan_limit', 500)
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError('scan_limit must be an integer between 1 and 10000')
    config['scan_limit'] = limit
    publications = config.get('publications')
    if not isinstance(publications, list) or not publications:
        raise ValueError('Configure at least one publication')
    ids = set()
    for pub in publications:
        if not isinstance(pub, dict) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', pub.get('id', '')):
            raise ValueError('Each publication id must be a lowercase folder slug')
        if pub['id'] in ids:
            raise ValueError('Publication ids must be unique')
        ids.add(pub['id'])
        if not isinstance(pub.get('channel'), (str, int)) or isinstance(pub.get('channel'), bool) or not str(pub['channel']).strip():
            raise ValueError('Each publication needs a Telegram channel username or numeric id')
        patterns = pub.get('patterns')
        if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p.strip() for p in patterns):
            raise ValueError('Each publication needs a nonempty list of filename patterns')
    return config


def claim_root(path, identity=None):
    path = safe_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = path / '.paperroute'
    metadata = path / ('.state.json' if identity is None else '.mirror.json')
    if marker.is_symlink():
        raise ValueError('Refusing a symlink marker')
    if marker.exists():
        saved = marker.read_text().strip()
        uuid.UUID(saved)
        if identity is not None and saved != identity:
            raise ValueError('This mirror belongs to another PaperRoute folder')
        if not metadata.exists():
            raise RecoveryError(f'Folder metadata is missing; stop the schedule and restore {metadata.name} from a backup before retrying')
        return saved
    if any(p.name != '.DS_Store' for p in path.iterdir()):
        raise ValueError('Choose an empty folder; PaperRoute will not adopt existing files')
    write_json(metadata, {'files': {}, 'latest': {}} if identity is None else {})
    identity = identity or str(uuid.uuid4())
    atomic_write(marker, (identity + '\n').encode())
    return identity


@contextmanager
def lock_root(root):
    lock = root / '.lock'
    if lock.is_symlink():
        raise ValueError('Refusing a symlink lock')
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another PaperRoute run is active') from None
        yield


def managed_path(root, name):
    relative = Path(name)
    if relative.is_absolute() or len(relative.parts) != 2 or any(p in ('.', '..') for p in relative.parts):
        raise ValueError('Invalid managed file path')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', relative.parts[0]) or relative.suffix != '.pdf':
        raise ValueError('Invalid managed publication path')
    return safe_path(root / relative)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def valid_pdf(path):
    with path.open('rb') as stream:
        return stream.read(5) == b'%PDF-'


def load_state(root):
    path = root / '.state.json'
    if path.is_symlink():
        raise ValueError('Refusing symlink state')
    if not path.exists():
        raise RecoveryError('Local metadata is missing; stop the schedule and restore .state.json from a backup before retrying')
    state = read_json(path)
    if not isinstance(state, dict) or set(state) != {'files', 'latest'} or not isinstance(state['files'], dict) or not isinstance(state['latest'], dict):
        raise ValueError('Invalid state; restore a backup before running cleanup')
    for name, sha in state['files'].items():
        managed_path(root, name)
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('Invalid file checksum in state')
    for pub, name in state['latest'].items():
        if name not in state['files'] or Path(name).parts[0] != pub:
            raise ValueError('Invalid current issue in state')
    return state


def filename(message):
    file = getattr(message, 'file', None)
    name = getattr(file, 'name', None)
    return name if isinstance(name, str) and name.lower().endswith('.pdf') else None


async def latest_message(client, publication, limit):
    channel = publication['channel']
    if isinstance(channel, str) and channel.lstrip('-').isdigit():
        channel = int(channel)
    # Resolve from the account's dialogs when a numeric channel isn't cached yet.
    try:
        entity = await client.get_entity(channel)
    except ValueError:
        async for _ in client.iter_dialogs():
            pass
        entity = await client.get_entity(channel)
    async for message in client.iter_messages(entity, limit=limit):
        name = filename(message)
        if name and any(fnmatch.fnmatchcase(name.casefold(), p.casefold()) for p in publication['patterns']):
            return message
    return None


async def deliver(message, publication, root, state, cleanup):
    pub = publication['id']
    original = filename(message)
    if not original:
        raise ValueError('Selected message is not a named PDF')
    # Telegram filenames are untrusted and never become directory components.
    clean = re.sub(r'[^A-Za-z0-9._ -]', '_', Path(original.replace('\\', '/')).name)[:120]
    clean = clean[:-4] if clean.lower().endswith('.pdf') else clean
    source = hashlib.sha256(str(publication['channel']).encode()).hexdigest()[:10]
    name = f'{pub}/{source}-{message.id}-{clean}.pdf'
    target = managed_path(root, name)
    target.parent.mkdir(exist_ok=True, mode=0o700)
    if target.exists() and name not in state['files']:
        raise RecoveryError('An untracked PDF occupies the delivery path and was preserved; move it outside the managed folder and retry')
    if target.exists() and digest(target) != state['files'][name]:
        raise RecoveryError('The current PDF was edited and was preserved; move it outside the managed folder and retry')
    # shortcut: same-post, same-filename media corrections are ignored; track document identity if corrections enter scope.
    verified = target.is_file() and state['files'].get(name) == digest(target) and valid_pdf(target)
    if not verified:
        fd, tmp = tempfile.mkstemp(prefix='.download-', suffix='.part', dir=target.parent)
        os.close(fd)
        temporary = Path(tmp)
        try:
            await message.download_media(file=str(temporary))
            expected = getattr(message.file, 'size', None)
            if not valid_pdf(temporary) or (expected is not None and temporary.stat().st_size != expected):
                raise ValueError('Download is incomplete or is not a PDF')
            sha = digest(temporary)
            # Record ownership before promotion so an interrupted rename can be retried.
            state['files'][name] = sha
            state['latest'][pub] = name
            write_json(root / '.state.json', state)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        print(f'Delivered {pub}: PDF verified')
    else:
        state['latest'][pub] = name
        print(f'Up to date: {pub}')
    if cleanup:
        for old in list(state['files']):
            if Path(old).parts[0] != pub or old == name:
                continue
            path = managed_path(root, old)
            if path.exists():
                if digest(path) != state['files'][old]:
                    raise RecoveryError('An older PDF was edited and was preserved; move it outside the managed folder and retry')
                path.unlink()
                print(f'Removed previous issue: {pub}')
            del state['files'][old]
    write_json(root / '.state.json', state)


def mirror(config, state, identity):
    source, destination = config['local_dir'], config.get('mirror_dir')
    if destination is None:
        return
    claim_root(destination, identity)
    manifest_path = destination / '.mirror.json'
    if manifest_path.is_symlink():
        raise ValueError('Refusing a symlink mirror manifest')
    if not manifest_path.exists():
        raise RecoveryError('Mirror metadata is missing; stop the schedule and restore .mirror.json from a backup before retrying')
    previous = read_json(manifest_path)
    if not isinstance(previous, dict):
        raise ValueError('Invalid mirror manifest')
    for name, sha in previous.items():
        managed_path(destination, name)
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('Invalid mirror checksum')
    for name, sha in state['files'].items():
        src, dst = managed_path(source, name), managed_path(destination, name)
        if not src.is_file() or digest(src) != sha or not valid_pdf(src):
            raise ValueError('Local PDF is missing or changed; mirror cleanup skipped')
        dst.parent.mkdir(exist_ok=True, mode=0o700)
        if dst.exists():
            actual = digest(dst)
            if actual == sha:
                if previous.get(name) != sha:
                    previous[name] = sha
                    write_json(manifest_path, previous)
                continue
            if name not in previous or actual != previous[name]:
                raise RecoveryError('A destination PDF was edited or is untracked and was preserved; move it outside the managed folder and retry')
        fd, tmp = tempfile.mkstemp(prefix='.mirror-', dir=dst.parent)
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            if digest(Path(tmp)) != sha:
                raise ValueError('Mirror copy verification failed')
            # A newly promoted file must already be owned if a later copy fails.
            if name not in previous:
                previous[name] = sha
                write_json(manifest_path, previous)
            os.replace(tmp, dst)
            if previous[name] != sha:
                previous[name] = sha
                write_json(manifest_path, previous)
        finally:
            Path(tmp).unlink(missing_ok=True)
    for name in previous.keys() - state['files'].keys():
        path = managed_path(destination, name)
        if path.exists():
            if digest(path) != previous[name]:
                raise RecoveryError('An older mirrored PDF was edited and was preserved; move it outside the managed folder and retry')
            path.unlink()
        del previous[name]
    write_json(manifest_path, previous)
    print(f'Mirror ready: {len(state["files"])} PDF(s)')


async def run_delivery(client, config):
    root = config['local_dir']
    identity = claim_root(root)
    with lock_root(root):
        state = load_state(root)
        failed = []
        for publication in config['publications']:
            try:
                message = await latest_message(client, publication, config['scan_limit'])
                if message is None:
                    print(f'No matching PDF in scan: {publication["id"]}; previous issue retained')
                    continue
                await deliver(message, publication, root, state, config['cleanup_confirmed'])
            except Exception as exc:
                failed.append(publication['id'])
                detail = str(exc) if isinstance(exc, RecoveryError) else f'{type(exc).__name__}; check access, filters and local files'
                print(f'Failed: {publication["id"]}: {detail}', file=sys.stderr)
        if failed:
            raise RuntimeError('Delivery failed; mirror deferred. Fix the issue and rerun')
        mirror(config, state, identity)


def credentials(directory, interactive=False):
    path = directory / 'credentials.json'
    if path.is_symlink():
        raise ValueError('Refusing symlink credentials')
    data = read_json(path)
    if data is None and interactive:
        try:
            api_id = int(getpass.getpass('Telegram API ID (hidden): '))
        except ValueError:
            raise ValueError('Telegram API ID must be a number') from None
        data = {'api_id': api_id,
                'api_hash': getpass.getpass('Telegram API hash (hidden): ').strip()}
        if data['api_id'] <= 0 or not re.fullmatch('[0-9a-fA-F]{32}', data['api_hash']):
            raise ValueError('Check your Telegram API ID and hash')
        write_json(path, data)
    if not data:
        raise ValueError('Run login first to configure Telegram credentials')
    return data


async def telegram_command(args):
    from telethon import TelegramClient
    directory = safe_path(args.config.parent)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_mode & 0o077:
        raise ValueError('Use a dedicated config directory with permissions 700')
    data = credentials(directory, args.command == 'login')
    session = directory / 'telegram.session'
    if session.is_symlink():
        raise ValueError('Refusing a symlink session')
    client = TelegramClient(str(session), data['api_id'], data['api_hash'],
                            receive_updates=False, connection_retries=3, request_retries=3)
    try:
        if args.command == 'login':
            await client.start(phone=lambda: getpass.getpass('Telegram phone (hidden): '),
                               code_callback=lambda: getpass.getpass('Telegram login code (hidden): '),
                               password=lambda: getpass.getpass('Telegram 2FA password (hidden): '))
            print('Telegram login saved locally. Configure your publications next.')
        else:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError('Telegram login is missing or expired; run login interactively')
            if args.command == 'channels':
                async for dialog in client.iter_dialogs():
                    if dialog.is_channel:
                        print(f'{dialog.id}\t{dialog.name}')
            else:
                await asyncio.wait_for(run_delivery(client, load_config(args.config)), timeout=600)
    finally:
        await client.disconnect()


def schedule(args):
    load_config(args.config)
    if not 0 <= args.hour <= 23 or not 0 <= args.minute <= 59:
        raise ValueError('Choose an hour 0–23 and a minute 0–59')
    directory = safe_path(args.config.parent)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    script = Path(__file__).resolve()
    data = {'Label': LABEL, 'ProgramArguments': [sys.executable, str(script), '--config', str(args.config), 'run'],
            'StartCalendarInterval': {'Hour': args.hour, 'Minute': args.minute},
            'StandardOutPath': str(directory / 'daily.log'),
            'StandardErrorPath': str(directory / 'daily-error.log'), 'Umask': 0o077}
    output = directory / f'{LABEL}.plist'
    atomic_write(output, plistlib.dumps(data))
    if args.install:
        if sys.platform != 'darwin':
            raise ValueError('Schedule installation requires macOS')
        protected = [Path.home() / p for p in ('Desktop', 'Documents', 'Downloads')]
        if any(p in script.parents for p in protected):
            raise ValueError('Move the repo to ~/Applications/PaperRoute before installing its schedule')
        destination = Path.home() / 'Library/LaunchAgents' / output.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ValueError('Refusing a symlink launch agent')
        domain = f'gui/{os.getuid()}'
        if destination.exists():
            subprocess.run(['launchctl', 'bootout', domain, str(destination)], check=False, capture_output=True)
        atomic_write(destination, plistlib.dumps(data))
        subprocess.run(['launchctl', 'bootstrap', domain, str(destination)], check=True)
        print(f'Daily delivery installed at {args.hour:02}:{args.minute:02} local time')
    else:
        print(f'Schedule written to {output}; not installed')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=lambda p: Path(p).expanduser().absolute(), default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('login', 'channels', 'validate', 'run'):
        commands.add_parser(name)
    timer = commands.add_parser('schedule')
    timer.add_argument('--hour', type=int, default=7)
    timer.add_argument('--minute', type=int, default=0)
    timer.add_argument('--install', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'validate':
            config = load_config(args.config)
            print(f'Configuration valid: {len(config["publications"])} publication(s); cleanup {"confirmed" if config["cleanup_confirmed"] else "disabled"}')
        elif args.command == 'schedule':
            schedule(args)
        else:
            asyncio.run(telegram_command(args))
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, (ValueError, RuntimeError)):
            print(f'PaperRoute: {exc}', file=sys.stderr)
        else:
            print(f'PaperRoute: {type(exc).__name__}; check configuration, connectivity and permissions', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
