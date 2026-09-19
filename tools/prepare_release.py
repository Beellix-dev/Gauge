"""Create versioned release files and checksums from an already tested build."""

from hashlib import sha256
from pathlib import Path
from runpy import run_path
import shutil
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    version = run_path(str(ROOT / "src" / "__init__.py"))["__version__"]
    executable = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "apps" / "Gauge.exe"
    if not executable.is_file():
        raise SystemExit("Build Gauge.exe before preparing a release.")
    subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "export_source.py")], check=True)
    destination = ROOT / "exports" / f"v{version}"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(executable, destination / f"Gauge-v{version}-windows-x64.exe")
    shutil.copy2(ROOT / "exports" / "Gauge-source.zip", destination / f"Gauge-v{version}-source.zip")
    notice_archive = destination / f"Gauge-v{version}-licenses.zip"
    with ZipFile(notice_archive, "w", ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "docs" / "licenses").iterdir()):
            if path.is_file():
                archive.write(path, path.name)
    names = [f"Gauge-v{version}-windows-x64.exe", f"Gauge-v{version}-source.zip", notice_archive.name]
    lines = [f"{sha256((destination / name).read_bytes()).hexdigest()}  {name}" for name in names]
    (destination / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
