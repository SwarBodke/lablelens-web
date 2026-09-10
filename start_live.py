"""LableLens local + Cloudflare Quick Tunnel launcher.

Starts the FastAPI backend and, when possible, a temporary HTTPS Quick Tunnel.
Works on Windows, macOS, and Linux. Quick Tunnels are for demos/testing only.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
GITHUB_LATEST = "https://github.com/cloudflare/cloudflared/releases/latest/download"
URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


def _asset_for_platform() -> tuple[str, bool]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arm64 = machine in {"arm64", "aarch64"}
    amd64 = machine in {"x86_64", "amd64", "x64"}

    if system == "windows" and (amd64 or not arm64):
        return "cloudflared-windows-amd64.exe", False
    if system == "windows" and arm64:
        return "cloudflared-windows-amd64.exe", False  # official Windows ARM64 asset may not be published; x64 works under emulation on supported systems
    if system == "linux" and arm64:
        return "cloudflared-linux-arm64", False
    if system == "linux" and amd64:
        return "cloudflared-linux-amd64", False
    if system == "darwin" and arm64:
        return "cloudflared-darwin-arm64.tgz", True
    if system == "darwin" and amd64:
        return "cloudflared-darwin-amd64.tgz", True
    raise RuntimeError(f"Unsupported platform for automatic cloudflared download: {platform.system()} {platform.machine()}")


def _is_working_cloudflared(path: str | Path) -> bool:
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=8)
        return proc.returncode == 0
    except Exception:
        return False


def ensure_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found and _is_working_cloudflared(found):
        return found

    local_name = "cloudflared.exe" if platform.system().lower() == "windows" else "cloudflared"
    local = BASE_DIR / local_name
    if local.exists() and _is_working_cloudflared(local):
        return str(local)

    try:
        asset, is_tgz = _asset_for_platform()
    except RuntimeError as exc:
        print(f"[Tunnel] {exc}")
        return None

    print(f"[Tunnel] cloudflared not found. Downloading {asset} from the official Cloudflare GitHub release…")
    try:
        with tempfile.TemporaryDirectory(prefix="lablelens-cf-") as td:
            temp = Path(td) / asset
            urllib.request.urlretrieve(f"{GITHUB_LATEST}/{asset}", temp)
            if is_tgz:
                with tarfile.open(temp, "r:gz") as tf:
                    member = next((m for m in tf.getmembers() if Path(m.name).name == "cloudflared" and m.isfile()), None)
                    if member is None:
                        raise RuntimeError("cloudflared binary was not found inside the downloaded archive")
                    src = tf.extractfile(member)
                    if src is None:
                        raise RuntimeError("could not extract cloudflared")
                    local.write_bytes(src.read())
            else:
                shutil.copy2(temp, local)
        if platform.system().lower() != "windows":
            local.chmod(local.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if not _is_working_cloudflared(local):
            raise RuntimeError("downloaded cloudflared did not execute successfully")
        return str(local)
    except Exception as exc:
        print(f"[Tunnel] Automatic download failed: {exc}")
        print("[Tunnel] Install cloudflared manually, then rerun this launcher.")
        return None


def _python_executable() -> str:
    candidates = [
        BASE_DIR / ".venv" / "Scripts" / "python.exe",
        BASE_DIR / "venv" / "Scripts" / "python.exe",
        BASE_DIR / ".venv" / "bin" / "python",
        BASE_DIR / "venv" / "bin" / "python",
    ]
    return str(next((p for p in candidates if p.exists()), Path(sys.executable)))


def main() -> None:
    print("=" * 64)
    print("  LABLELENS — LOCAL SERVER + TEMPORARY CLOUDFLARE TUNNEL")
    print("=" * 64)
    print("Quick Tunnels are suitable for testing and temporary previews, not a stable production URL.\n")

    python_exe = _python_executable()
    port = os.getenv("PORT", "8000")
    backend_url = f"http://127.0.0.1:{port}"
    log_path = BASE_DIR / "server.log"
    log_file = log_path.open("w", encoding="utf-8")

    env = os.environ.copy()
    env.setdefault("LABLELENS_SECRET", "local-demo-change-me")
    backend = subprocess.Popen(
        [python_exe, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", port],
        cwd=BASE_DIR,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    try:
        time.sleep(2)
        if backend.poll() is not None:
            log_file.close()
            print(f"[Backend] Failed to start. See {log_path}")
            print(log_path.read_text(encoding="utf-8", errors="ignore"))
            return

        print(f"[Backend] Running locally at {backend_url}")
        cloudflared = ensure_cloudflared()
        if not cloudflared:
            print("[Tunnel] No public tunnel started. The local app is still running.")
            print("Press Ctrl+C to stop.")
            backend.wait()
            return

        print("[Tunnel] Requesting a temporary HTTPS URL…")
        tunnel = subprocess.Popen(
            [cloudflared, "tunnel", "--url", backend_url, "--no-autoupdate"],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            assert tunnel.stdout is not None
            for line in tunnel.stdout:
                match = URL_RE.search(line)
                if match:
                    print("\n" + "=" * 64)
                    print("  TEMPORARY PUBLIC DEMO URL")
                    print(f"  {match.group(0)}")
                    print("=" * 64 + "\n")
                    break
            tunnel.wait()
        finally:
            if tunnel.poll() is None:
                tunnel.terminate()
    except KeyboardInterrupt:
        print("\nStopping LableLens…")
    finally:
        if backend.poll() is None:
            backend.terminate()
        log_file.close()


if __name__ == "__main__":
    main()
