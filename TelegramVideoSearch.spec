from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

source = Path(SPECPATH) / 'src'
a = Analysis(
    [str(source / 'app.py')],
    pathex=[str(source)], binaries=[], datas=[],
    hiddenimports=collect_submodules('python_socks.async_.asyncio'),
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='TelegramVideoSearch',
          console=False, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='TelegramVideoSearch')
