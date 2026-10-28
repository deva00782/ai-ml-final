#!/usr/bin/env python3
"""
Cross-platform Chrome launcher with remote debugging enabled.

Replaces the macOS-only start_chrome.sh with a Python implementation that
works on Windows, macOS, and Linux.

Features:
- Detects existing Chrome on port 9222 via socket (no lsof dependency)
- Resolves Chrome binary per-OS (macOS .app, Linux which, Windows Program Files)
- Uses a dedicated debug profile (~/chrome-debug-profile) so the user's
  real profile is never disturbed. Copied once on first run (skips caches/locks).
- Launches Chrome with --remote-debugging-port=9222 --user-data-dir=<debug>
- Waits up to ~20s for the debug port to become reachable.

Usage:
    python start_chrome.py              # default port 9222
    python start_chrome.py --port 9222
    python start_chrome.py --help
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# Force UTF-8 stdout/stderr encoding on Windows consoles (cp1252 default)
# to prevent UnicodeEncodeError when printing status glyphs (emoji).
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


DEBUG_PORT_DEFAULT = 9222
PROFILE_READY_FLAG = ".debug-profile-ready"

# Directories / files to skip when copying the real profile (caches, locks)
EXCLUDE_NAMES = {
    "Cache",
    "CacheStorage",
    "Code Cache",
    "GPUCache",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "lockfile",
    "ShaderCache",
}

EXCLUDE_PREFIXES = ("Cache",)

EXE_CHROME = "chrome.exe"
EXE_MSEDGE = "msedge.exe"
DIR_CONFIG = ".config"


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def _get_chrome_darwin() -> str | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for p in candidates:
        if Path(p).is_file():
            return p
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _get_chrome_linux() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome", "chrome-browser"):
        found = shutil.which(name)
        if found:
            return found
    for p in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
              "/usr/bin/chromium-browser", "/usr/bin/chromium",
              "/snap/bin/chromium", "/snap/bin/google-chrome",
              "/opt/google/chrome/google-chrome"):
        if Path(p).is_file():
            return p
    try:
        r = subprocess.run(["flatpak", "list", "--app"], capture_output=True, text=True, timeout=2)
        if "com.google.Chrome" in r.stdout:
            return "flatpak run com.google.Chrome"
    except Exception:
        pass
    return None


def _get_chrome_windows() -> str | None:
    candidates: list[str] = []
    for name in ("chrome", EXE_CHROME, "google-chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local_app = os.environ.get("LOCALAPPDATA", "")
    user_profile = os.environ.get("USERPROFILE", "")
    candidates.extend([
        str(Path(pf) / "Google" / "Chrome" / "Application" / EXE_CHROME),
        str(Path(pf_x86) / "Google" / "Chrome" / "Application" / EXE_CHROME),
        str(Path(local_app) / "Google" / "Chrome" / "Application" / EXE_CHROME),
        str(Path(user_profile) / "AppData" / "Local" / "Google" / "Chrome" / "Application" / EXE_CHROME),
        rf"C:\Program Files\Google\Chrome\Application\{EXE_CHROME}",
        rf"C:\Program Files (x86)\Google\Chrome\Application\{EXE_CHROME}",
    ])
    for p in candidates:
        if p and Path(p).is_file():
            return p
    for p in (
        str(Path(pf) / "Chromium" / "Application" / EXE_CHROME),
        str(Path(local_app) / "Chromium" / "Application" / EXE_CHROME),
        str(Path(pf) / "Microsoft" / "Edge" / "Application" / EXE_MSEDGE),
        str(Path(pf_x86) / "Microsoft" / "Edge" / "Application" / EXE_MSEDGE),
        str(Path(local_app) / "Microsoft" / "Edge" / "Application" / EXE_MSEDGE),
    ):
        if Path(p).is_file():
            return p
    try:
        r = subprocess.run(["where", "chrome"], capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if Path(line.strip()).is_file():
                    return line.strip()
    except Exception:
        pass
    return None


def get_chrome_binary() -> str | None:
    system = platform.system()
    if system == "Darwin":
        return _get_chrome_darwin()
    if system == "Linux":
        return _get_chrome_linux()
    if system == "Windows":
        return _get_chrome_windows()

    for name in ("google-chrome", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def get_default_profile_dir() -> Path | None:
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    if system == "Linux":
        # Try google-chrome first, then chromium
        for cand in (home / DIR_CONFIG / "google-chrome", home / DIR_CONFIG / "chromium", home / "snap" / "chromium" / "common" / "chromium"):
            if cand.is_dir():
                return cand
        return home / DIR_CONFIG / "google-chrome"
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        p = Path(local) / "Google" / "Chrome" / "User Data"
        if p.is_dir():
            return p
        # fallback to Edge profile if Chrome not found
        edge = Path(local) / "Microsoft" / "Edge" / "User Data"
        if edge.is_dir():
            return edge
        return p
    return None


def get_debug_dir() -> Path:
    return Path.home() / "chrome-debug-profile"


def should_exclude(path: Path) -> bool:
    name = path.name
    if name in EXCLUDE_NAMES:
        return True
    for prefix in EXCLUDE_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def _copy_profile_item(item: Path, dst: Path):
    if should_exclude(item):
        return
    dest = dst / item.name
    try:
        if item.is_dir():
            if dest.exists():
                return
            shutil.copytree(item, dest, symlinks=True, ignore=lambda d, names: {n for n in names if n in EXCLUDE_NAMES or any(n.startswith(p) for p in EXCLUDE_PREFIXES)})
        else:
            shutil.copy2(item, dest)
    except Exception as e:
        print(f"  skip {item.name}: {e}")


def prepare_debug_profile(src: Path | None, dst: Path) -> None:
    flag = dst / PROFILE_READY_FLAG
    if flag.is_file():
        print(f"Using existing debug profile: {dst}")
        return

    print("Preparing Chrome debug profile (first-time setup)...")
    dst.mkdir(parents=True, exist_ok=True)

    if src is not None and src.is_dir():
        try:
            for item in src.iterdir():
                _copy_profile_item(item, dst)
            print(f"✅ Chrome profile copied to debug profile ({dst})")
        except Exception as e:
            print(f"  profile copy warning: {e}")
            print("  Using empty debug profile")
    else:
        print("Using empty debug profile (no existing Chrome profile found)")

    try:
        flag.touch(exist_ok=True)
    except Exception:
        pass


def launch_chrome(chrome_bin: str, debug_dir: Path, port: int, url: str | None = None) -> subprocess.Popen:
    # Support flatpak invocation (chrome_bin contains spaces)
    if chrome_bin.startswith("flatpak "):
        args = chrome_bin.split() + [f"--remote-debugging-port={port}", f"--user-data-dir={str(debug_dir)}"]
    else:
        args = [
            chrome_bin,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={str(debug_dir)}",
        ]
    # Linux needs --no-first-run and --no-default-browser-check to avoid prompts
    if platform.system() == "Linux":
        args.extend(["--no-first-run", "--no-default-browser-check"])
    if url:
        args.append(url)
    # Keep Chrome output quiet; detach appropriately per OS
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    system = platform.system()
    if system == "Windows":
        # CREATE_NO_WINDOW (0x08000000) prevents any console / command prompt window
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | CREATE_NO_WINDOW
        return subprocess.Popen(args, **kwargs)
    else:
        kwargs["start_new_session"] = True
        return subprocess.Popen(args, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start Chrome with remote debugging (cross-platform)")
    parser.add_argument("--port", type=int, default=DEBUG_PORT_DEFAULT, help=f"Remote debugging port (default {DEBUG_PORT_DEFAULT})")
    parser.add_argument("--url", type=str, default="https://www.jiosaavn.com", help="Initial URL to open in Chrome (default https://www.jiosaavn.com)")
    parser.add_argument("--no-copy-profile", action="store_true", help="Skip copying existing Chrome profile; use empty debug profile")
    parser.add_argument("--debug-dir", type=str, default=None, help="Custom debug profile directory (default ~/chrome-debug-profile)")
    args = parser.parse_args(argv)

    port = args.port
    print(f"Starting Chrome with remote debugging on port {port}...")

    if is_port_open(port):
        print(f"Chrome is already running with remote debugging on port {port}")
        print("You can connect to it now.")
        return 0

    chrome_bin = get_chrome_binary()
    if not chrome_bin:
        print("❌ Chrome/Chromium binary not found.", file=sys.stderr)
        print("Please install Google Chrome and/or ensure it is on your PATH.", file=sys.stderr)
        print(f"Or start Chrome manually with: chrome --remote-debugging-port={port} --user-data-dir=/tmp/chrome-debug", file=sys.stderr)
        return 1

    debug_dir = Path(args.debug_dir).expanduser() if args.debug_dir else get_debug_dir()
    profile_src = None if args.no_copy_profile else get_default_profile_dir()

    prepare_debug_profile(profile_src, debug_dir)

    print(f"Launching: {chrome_bin}")
    try:
        launch_chrome(chrome_bin, debug_dir, port, url=args.url)
    except Exception as e:
        print(f"❌ Failed to launch Chrome: {e}", file=sys.stderr)
        return 1

    print("Waiting for Chrome to start with remote debugging...")
    for _ in range(20):
        if is_port_open(port):
            print(f"✅ Chrome is ready with remote debugging on port {port}")
            print("Navigate to JioSaavn in this Chrome instance for full automation support.")
            print("")
            print("IMPORTANT: This system is designed specifically for JioSaavn web player control.")
            print("Chrome remote debugging is required due to OS security restrictions.")
            print("")
            print("✅ Your Chrome profile (if any) has been copied to the debug profile.")
            print("You should be already logged in to JioSaavn!")
            return 0
        time.sleep(1)

    print("❌ Chrome failed to start with remote debugging", file=sys.stderr)
    print("", file=sys.stderr)
    print("Troubleshooting steps:", file=sys.stderr)
    print("1. Make sure no other Chrome is blocking the port", file=sys.stderr)
    print("2. Try running the script again", file=sys.stderr)
    print("3. Or start Chrome manually with:", file=sys.stderr)
    print(f'   "{chrome_bin}" --remote-debugging-port={port} --user-data-dir="{debug_dir}"', file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
