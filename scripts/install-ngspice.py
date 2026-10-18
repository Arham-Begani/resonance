"""Install the official ngspice 47 Windows build into .tools/ (project-local, git-ignored).

Usage (from the repo root):  uv run --with py7zr python scripts/install-ngspice.py
On Linux/macOS install ngspice from the system package manager instead
(e.g. `apt-get install ngspice`) or set NGSPICE_BIN.

The archive is verified against the SHA-256 published on the ngspice SourceForge
file listing before extraction.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

VERSION = "47"
ARCHIVE = f"ngspice-{VERSION}_64.7z"
URL = f"https://sourceforge.net/projects/ngspice/files/ng-spice-rework/{VERSION}/{ARCHIVE}/download"
SHA256 = "59225971bd68cdd1199443649aa4615a9e6d684933f205ab49006a3942518f5a"

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / ".tools" / f"ngspice-{VERSION}"
BINARY = TARGET / "Spice64" / "bin" / "ngspice_con.exe"


def main() -> int:
    if sys.platform != "win32":
        print("This script installs the Windows build. On Linux/macOS: apt-get install ngspice")
        return 1
    if BINARY.is_file():
        print(f"ngspice already installed: {BINARY}")
        return 0
    try:
        import py7zr  # noqa: PLC0415
    except ImportError:
        print("py7zr is required: uv run --with py7zr python scripts/install-ngspice.py")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / ARCHIVE
        print(f"Downloading {URL}")
        request = urllib.request.Request(URL, headers={"User-Agent": "Wget/1.21"})  # noqa: S310
        with urllib.request.urlopen(request, timeout=120) as resp, archive.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != SHA256:
            print(f"Checksum mismatch: expected {SHA256}, got {digest}. Aborting.")
            return 1
        print("Checksum OK; extracting")
        TARGET.mkdir(parents=True, exist_ok=True)
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(TARGET)

    out = subprocess.run([str(BINARY), "-v"], capture_output=True, text=True, timeout=30)  # noqa: S603
    first = next((ln for ln in out.stdout.splitlines() if "ngspice" in ln), "").strip()
    print(f"Installed: {BINARY}\n{first}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
