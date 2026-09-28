"""Package a fresh build and an explicit source list; never copy runtime data."""
import importlib.metadata
from pathlib import Path
import shutil
import sys
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
build = root / 'dist' / 'TelegramVideoSearch'
if not (build / 'TelegramVideoSearch.exe').is_file():
    raise SystemExit('Build the application with PyInstaller first.')
for path in build.rglob('*'):
    if path.is_symlink() or 'data' in path.relative_to(build).parts or path.suffix in {'.session', '.sqlite3', '.dpapi'}:
        raise SystemExit('Build directory contains runtime data or symbolic links; use a fresh build.')

release = root / 'release'
release.mkdir(exist_ok=True)
stage = release / 'TelegramVideoSearch'
if stage.exists():
    raise SystemExit('Release staging directory already exists; use a clean checkout for a new release.')
shutil.copytree(build, stage)
for name in ['README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'requirements.txt', 'requirements-dev.txt', 'TelegramVideoSearch.spec']:
    shutil.copy2(root / name, stage / name)
for folder in ['src', 'tests', 'scripts', 'licenses']:
    shutil.copytree(root / folder, stage / folder, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
licenses = stage / 'licenses'
for name in ['Telethon', 'PySide6-Essentials', 'shiboken6', 'python-socks', 'pyaes', 'rsa', 'pyasn1', 'qrcode', 'pyinstaller']:
    dist = importlib.metadata.distribution(name)
    target = licenses / name
    target.mkdir(exist_ok=True)
    for item in dist.files or []:
        if '.dist-info/' in str(item) and ('license' in str(item).lower() or 'copying' in str(item).lower()):
            source = Path(dist.locate_file(item))
            if source.is_file():
                shutil.copy2(source, target / source.name)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.exists():
    shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')

archive = release / 'TelegramVideoSearch-Windows.zip'
with ZipFile(archive, 'w', ZIP_DEFLATED) as zipped:
    for path in sorted(stage.rglob('*')):
        if path.is_file():
            zipped.write(path, path.relative_to(release))
with ZipFile(archive) as zipped:
    if zipped.testzip() is not None:
        raise SystemExit('Archive integrity check failed.')
print(f'Created {archive.name}')
