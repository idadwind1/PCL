"""Sync this fork, build with downloads enabled, and publish versioned binaries."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile


SECRET = Path('Plain Craft Launcher 2/Modules/ModSecret.vb')
BASE = Path('Plain Craft Launcher 2/Modules/Base/ModBase.vb')
ARCHIVE = Path('最新正式版.zip')
ARCHIVE_ENTRY = 'Plain Craft Launcher 2.exe'
DOWNLOAD_FLAG = re.compile(
    rb'(?mi)^([ \t]*Public Const RestrictMcDownload As Boolean[ \t]*=[ \t]*)'
    rb'(True|False)([ \t]*(?:\'[^\r\n]*)?)(?=\r?$)'
)
VERSION = re.compile(r'(?m)^\s*Public Const VersionBaseName As String = "(\d+(?:\.\d+){2,3})"')


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def api(path, method='GET', data=None, missing_ok=False):
    body = None if data is None else json.dumps(data).encode()
    request = Request(
        'https://api.github.com' + path, data=body, method=method,
        headers={
            'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
            'User-Agent': 'PCL-fork-release',
            'X-GitHub-Api-Version': '2022-11-28',
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 404 and missing_ok:
            return None
        raise RuntimeError(f'GitHub API {method} {path} failed: HTTP {error.code}') from error


def enable_downloads(content):
    if len(DOWNLOAD_FLAG.findall(content)) != 1:
        raise ValueError('Expected exactly one RestrictMcDownload declaration; upstream changed.')
    return DOWNLOAD_FLAG.sub(lambda match: match[1] + b'False' + match[3], content)


def source_version(content):
    matches = VERSION.findall(content)
    if len(matches) != 1:
        raise ValueError('Expected exactly one numeric VersionBaseName; upstream changed.')
    return matches[0]


def merge_upstream(sha):
    try:
        git('merge', '--no-edit', sha)
    except subprocess.CalledProcessError:
        conflicts = git('-c', 'core.quotepath=false', 'diff', '--name-only', '--diff-filter=U').splitlines()
        if conflicts != [ARCHIVE.as_posix()]:
            raise
        # This generated binary tracks our release, rather than upstream's EXE.
        # Only this known archive may be resolved automatically.
        git('checkout', '--ours', '--', ARCHIVE.as_posix())
        git('add', '--', ARCHIVE.as_posix())
        git('commit', '--no-edit')


def output(**values):
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
        for key, value in values.items():
            value = str(value)
            if '\n' in value or '\r' in value:
                raise ValueError('Multiline workflow output is not supported.')
            stream.write(f'{key}={value}\n')


def sync():
    branch = os.environ['FORK_BRANCH']
    upstream = os.environ['UPSTREAM_REPOSITORY']
    upstream_branch = os.environ['UPSTREAM_BRANCH']
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    git('fetch', '--no-tags', 'origin', f'+refs/heads/{branch}:refs/remotes/origin/{branch}')
    git('checkout', '-B', branch, f'origin/{branch}')
    before = git('rev-parse', 'HEAD')
    git('fetch', '--no-tags', f'https://github.com/{upstream}.git', f'refs/heads/{upstream_branch}')
    upstream_sha = git('rev-parse', 'FETCH_HEAD')
    version = source_version(git('show', f'{upstream_sha}:{BASE.as_posix()}'))
    # Preserve fork files and the generated release archive. Source conflicts fail.
    merge_upstream(upstream_sha)
    if source_version(BASE.read_text(encoding='utf-8-sig')) != version:
        raise ValueError('Fork version differs from upstream; refusing to label the build.')
    original = SECRET.read_bytes()
    patched = enable_downloads(original)
    if patched != original:
        SECRET.write_bytes(patched)
        git('add', '--', SECRET.as_posix())
        git('commit', '-m', f'Enable Minecraft downloads for {version}')
    sha = git('rev-parse', 'HEAD')
    if sha != before:
        # No force push: concurrent changes to main must be reviewed/retried.
        git('push', 'origin', f'HEAD:refs/heads/{branch}')
    tag = f'v{version}'
    repo = os.environ['GITHUB_REPOSITORY']
    release = api(f'/repos/{repo}/releases/tags/{quote(tag, safe="")}', missing_ok=True)
    build = release is None or release['draft']
    output(build=str(build).lower(), sha=sha, version=version, tag=tag, upstream_sha=upstream_sha)
    print(f'Synced {upstream}@{upstream_sha}; {tag}: ' + ('build required' if build else 'already published'))


def replace_once(content, old, new, description):
    if content.count(old) != 1:
        raise ValueError(f'Expected exactly one {description}; upstream changed.')
    return content.replace(old, new)


def prepare():
    content = SECRET.read_bytes()
    if content != enable_downloads(content):
        raise ValueError('The commit being built does not have Minecraft downloads enabled.')
    for field, variable, secret in (
        ('OAuthClientId', 'PCL_MS_CLIENT_ID', 'CLIENT_ID'),
        ('CurseForgeAPIKey', 'PCL_CURSEFORGE_API_KEY', 'CURSEFORGE_API_KEY'),
    ):
        value = os.environ.get(secret, '')
        if not value:
            continue
        if '\r' in value or '\n' in value:
            raise ValueError(f'{secret} must be a single line.')
        old = f'Public {field} As String = If(Environment.GetEnvironmentVariable("{variable}"), "")'
        escaped = value.replace('"', '""')
        new = f'Public {field} As String = If(Environment.GetEnvironmentVariable("{variable}"), "{escaped}")'
        content = replace_once(content, old.encode(), new.encode(), field)
    SECRET.write_bytes(content)
    old = b'Public Const CommitHash As String = ""'
    new = f'Public Const CommitHash As String = "{os.environ["BUILD_SHA"]}"'.encode()
    BASE.write_bytes(replace_once(BASE.read_bytes(), old, new, 'CommitHash declaration'))


def package():
    version = os.environ['RELEASE_VERSION']
    if source_version(BASE.read_text(encoding='utf-8-sig')) != version:
        raise ValueError('Build version does not match the intended release.')
    executable = Path('Plain Craft Launcher 2/bin/Plain Craft Launcher 2.exe')
    if not executable.is_file() or executable.stat().st_size == 0:
        raise ValueError('MSBuild did not produce a nonempty launcher executable.')
    directory = Path('release-assets')
    directory.mkdir(exist_ok=True)
    asset = directory / f'PCL-{version}.exe'
    shutil.copy2(executable, asset)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    (directory / 'SHA256SUMS.txt').write_text(f'{digest}  {asset.name}\n', encoding='utf-8')
    provenance = {
        'version': version, 'fork_commit': os.environ['BUILD_SHA'],
        'upstream_repository': os.environ['UPSTREAM_REPOSITORY'],
        'upstream_commit': os.environ['UPSTREAM_SHA'],
        'configuration': 'Release', 'RestrictMcDownload': False,
    }
    (directory / 'build-info.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')


def publish():
    repo = os.environ['GITHUB_REPOSITORY']
    sha = os.environ['BUILD_SHA']
    version = os.environ['RELEASE_VERSION']
    tag = os.environ['RELEASE_TAG']
    root = f'/repos/{repo}'
    encoded_tag = quote(tag, safe='')
    assets = Path('release-assets')
    executable = assets / f'PCL-{version}.exe'
    expected = f'{hashlib.sha256(executable.read_bytes()).hexdigest()}  {executable.name}\n'
    if (assets / 'SHA256SUMS.txt').read_text(encoding='utf-8') != expected:
        raise ValueError('Downloaded release artifact failed checksum validation.')
    info = json.loads((assets / 'build-info.json').read_text(encoding='utf-8'))
    if info['fork_commit'] != sha or info['version'] != version or info['RestrictMcDownload'] is not False:
        raise ValueError('Downloaded artifact does not match the release commit/version.')
    ref = api(root + f'/git/ref/tags/{encoded_tag}', missing_ok=True)
    if ref is None:
        api(root + '/git/refs', 'POST', {'ref': f'refs/tags/{tag}', 'sha': sha})
    else:
        target = ref['object']
        while target['type'] == 'tag':
            target = api(root + f'/git/tags/{target["sha"]}')['object']
        if target['type'] != 'commit' or target['sha'] != sha:
            raise ValueError(f'{tag} already points to another commit; refusing to move it.')
    release = api(root + f'/releases/tags/{encoded_tag}', missing_ok=True)
    if release is not None and not release['draft']:
        print(f'{tag} is already published; leaving it unchanged.')
        return
    if release is None:
        upstream = os.environ['UPSTREAM_REPOSITORY']
        upstream_sha = os.environ['UPSTREAM_SHA']
        release = api(root + '/releases', 'POST', {
            'tag_name': tag, 'target_commitish': sha,
            'name': f'PCL {version} (downloads enabled)', 'draft': True,
            'body': f'Fork build of [{upstream}](https://github.com/{upstream}/commit/{upstream_sha}).\n\n'
                    'Minecraft downloads are enabled (`RestrictMcDownload = False`).\n'
                    'Other open-source edition limitations remain. This is an unofficial build.\n\n'
                    f'Built from fork commit `{sha}` using the Release configuration.\n'
                    'The executable, SHA-256 checksum, and build metadata are attached.',
        })
    # Retry a failed upload by replacing assets only while the release is a draft.
    for asset in release['assets']:
        if asset['name'] in {executable.name, 'SHA256SUMS.txt', 'build-info.json'}:
            request = Request('https://api.github.com' + root + f'/releases/assets/{asset["id"]}',
                              method='DELETE', headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                                                        'User-Agent': 'PCL-fork-release'})
            with urlopen(request, timeout=60):
                pass
    for name in (executable.name, 'SHA256SUMS.txt', 'build-info.json'):
        # gh handles GitHub's binary upload endpoint without exposing the token.
        subprocess.run(['gh', 'release', 'upload', tag, str(assets / name), '--repo', repo], check=True)
    release = api(root + f'/releases/{release["id"]}', 'PATCH', {'draft': False})
    print(f'Published {release["html_url"]}')


def archive_bytes(executable):
    buffer = io.BytesIO()
    # Fixed metadata makes repeated runs produce identical ZIP bytes.
    entry = zipfile.ZipInfo(ARCHIVE_ENTRY)
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(entry, executable, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def archive_matches(digest):
    if not ARCHIVE.is_file():
        return False
    try:
        with zipfile.ZipFile(ARCHIVE) as archive:
            return (archive.namelist() == [ARCHIVE_ENTRY]
                    and hashlib.sha256(archive.read(ARCHIVE_ENTRY)).hexdigest() == digest)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return False


def refresh_archive():
    repo = os.environ['GITHUB_REPOSITORY']
    version = os.environ['RELEASE_VERSION']
    tag = os.environ['RELEASE_TAG']
    branch = os.environ['FORK_BRANCH']
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    git('fetch', '--no-tags', 'origin', f'+refs/heads/{branch}:refs/remotes/origin/{branch}')
    git('checkout', '-B', branch, f'origin/{branch}')
    if source_version(BASE.read_text(encoding='utf-8-sig')) != version:
        print(f'Main has moved to another source version; skipping archive for {tag}.')
        return
    release = api(f'/repos/{repo}/releases/tags/{quote(tag, safe="")}')
    if release['draft']:
        raise ValueError('The root archive can only be updated from a published release.')
    name = f'PCL-{version}.exe'
    assets = [asset for asset in release['assets'] if asset['name'] == name]
    if len(assets) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', assets[0].get('digest') or ''):
        raise ValueError('Expected one release executable with a GitHub SHA-256 digest.')
    asset = assets[0]
    digest = asset['digest'].removeprefix('sha256:')
    if archive_matches(digest):
        print(f'{ARCHIVE} already contains the executable from {tag}.')
        return
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(['gh', 'release', 'download', tag, '--repo', repo,
                        '--pattern', name, '--dir', directory], check=True)
        executable = (Path(directory) / name).read_bytes()
    if (not executable.startswith(b'MZ') or len(executable) != asset['size']
            or hashlib.sha256(executable).hexdigest() != digest):
        raise ValueError('Release executable failed validation; leaving the root archive unchanged.')
    ARCHIVE.write_bytes(archive_bytes(executable))
    git('add', '--', ARCHIVE.as_posix())
    git('commit', '-m', f'Refresh latest launcher archive for {tag}')
    # Keep release tags on the original compiled source commit. This follow-up
    # commit updates only the convenience download on main, without force pushes.
    git('push', 'origin', f'HEAD:refs/heads/{branch}')
    print(f'Updated {ARCHIVE} from the published {tag} executable.')


if __name__ == '__main__':
    commands = {'sync': sync, 'prepare': prepare, 'package': package, 'publish': publish,
                'refresh-archive': refresh_archive}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit('Usage: upstream_release.py sync|prepare|package|publish|refresh-archive')
    commands[sys.argv[1]]()
