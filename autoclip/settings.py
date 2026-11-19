"""User settings that outlive a run (where clips go). Stored per user, outside the project folder, so a `git pull`
never touches them:  Windows %APPDATA%\\autoclip\\settings.json   (override the folder with AUTOCLIP_CONFIG_DIR)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def config_dir() -> Path:
    if os.environ.get("AUTOCLIP_CONFIG_DIR"):
        return Path(os.environ["AUTOCLIP_CONFIG_DIR"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "autoclip"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autoclip"


def path() -> Path:
    return config_dir() / "settings.json"


def load() -> dict:
    try:
        d = json.loads(path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def update(**kw) -> dict:
    d = load()
    d.update({k: v for k, v in kw.items() if v is not None})
    save(d)
    return d


def apply(cfg) -> None:
    """Saved folders override config.toml (a --out on the command line still wins, applied after this)."""
    s = load()
    if s.get("out_dir"):
        cfg.out_dir = s["out_dir"]
    if s.get("export_dir") is not None:
        cfg.export_dir = s["export_dir"]


def validate_dir(p: str, create: bool = True) -> Path:
    """A folder we can really write into, or ValueError with a plain-language reason."""
    p = (p or "").strip().strip('"')
    if not p:
        raise ValueError("folder is empty")
    d = Path(p).expanduser()
    if not d.is_absolute():
        raise ValueError("use a full path such as D:\\Clips")
    try:
        if create:
            d.mkdir(parents=True, exist_ok=True)
        if not d.is_dir():
            raise ValueError("that folder does not exist")
        with tempfile.NamedTemporaryFile(dir=d, prefix=".autoclip_write_test_", delete=True):
            pass
    except OSError as e:
        raise ValueError(f"cannot write to that folder ({e.strerror or e})") from None
    return d.resolve()


_PICKER = ("import sys, tkinter as tk\nfrom tkinter import filedialog\n"
           "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
           "print(filedialog.askdirectory(initialdir=sys.argv[1] or None, title='Choose a folder') or '')\n")


def pick_folder(initial: str = "") -> str | None:
    """Native 'choose folder' dialog on this computer. None = cancelled or no dialog available."""
    try:
        r = subprocess.run([sys.executable, "-c", _PICKER, initial or ""], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def reveal(p: Path) -> None:
    """Show a file (selected) or a folder in the system file manager."""
    p = Path(p)
    if sys.platform == "win32":
        if p.is_file():
            subprocess.Popen(["explorer", f"/select,{p}"])
        else:
            os.startfile(str(p))                                    # noqa: S606 - opens a folder the user chose
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)] if p.is_file() else ["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
