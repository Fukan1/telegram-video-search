"""Open the desktop client explicitly; avoid browser/association indirection."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def desktop_executable(data_dir):
    settings = Path(data_dir) / "desktop.json"
    try:
        value = json.loads(settings.read_text(encoding="utf-8-sig")).get("executable", "")
        if value and Path(value).is_file() and Path(value).name.lower() == "telegram.exe":
            return value  # Preserve the exact launch-path casing for Telegram's IPC key.
    except (OSError, ValueError, AttributeError):
        pass
    if os.name == "nt":
        import winreg
        for root, key in ((winreg.HKEY_CURRENT_USER, r"Software\Classes\tg\shell\open\command"),
                          (winreg.HKEY_CLASSES_ROOT, r"tg\shell\open\command")):
            try:
                with winreg.OpenKey(root, key) as handle:
                    command = winreg.QueryValue(handle, None)
                match = re.match(r'^\s*"([^"]+\.exe)"', command, re.I)
                if match and Path(match[1]).is_file() and Path(match[1]).name.lower() == "telegram.exe":
                    return match[1]
            except OSError:
                pass
    return None


def save_desktop_executable(data_dir, executable):
    if not Path(executable).is_file() or Path(executable).name.lower() != "telegram.exe":
        raise ValueError("请选择 Telegram Desktop 的 Telegram.exe。")
    (Path(data_dir) / "desktop.json").write_text(json.dumps({"executable": str(executable)}, ensure_ascii=False), encoding="utf-8")


def launch_desktop(executable, url):
    if not url.startswith(("tg://privatepost?", "tg://resolve?")):
        raise ValueError("仅支持 Telegram 消息链接。")
    env = os.environ.copy()
    for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(key, None)
    # PyInstaller changes the DLL search path; do not pass its Qt DLLs to Telegram.
    frozen_windows = os.name == "nt" and getattr(sys, "frozen", False)
    if frozen_windows:
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen([str(executable), "--", url], cwd=str(Path(executable).parent), env=env)
    finally:
        if frozen_windows:
            ctypes.windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))
