"""Export only reviewed project files; never recursively copy a workspace."""
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import stat

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitattributes", ".gitignore", "LICENSE", "README.md", "gauge.py",
    "docs/SECURITY.md", "docs/CONTRIBUTING.md", "docs/THIRD_PARTY_NOTICES.md",
    "config/requirements.txt", "config/requirements-dev.txt", "config/Gauge.spec",
    "src/__init__.py", "src/main.py", "src/account_switch.py", "src/preferences.py",
    "src/claude_provider.py", "src/i18n.py", "src/models.py", "src/codex_provider.py",
    "assets/gauge.svg", "assets/gauge.png", "assets/gauge.ico",
    "assets/openai.svg", "assets/claude.svg",
    "tests/__init__.py", "tests/test_account_switch.py",
    "tests/test_preferences.py", "tests/test_gauge.py",
    "tests/test_codex_provider.py",
    "tests/test_switch_ui.py", "tests/test_window_resize.py", "tests/test_login.py", "tests/test_claude.py",
    "tools/build.ps1", "tools/render_icon.py", "tools/export_source.py",
    "tools/capture_previews.py", "tools/prepare_release.py",
    "docs/CHANGELOG.md", "docs/releases/v0.1.0.md",
    "docs/images/monitor-dark.png", "docs/images/monitor-light.png", "docs/images/settings.png",
    "docs/licenses/index.html", "docs/licenses/LICENSE",
    "docs/licenses/python.txt", "docs/licenses/pyinstaller.txt",
    "docs/licenses/qtbase-6.8.1.txt", "docs/licenses/qtsvg-6.8.1.txt",
    "docs/licenses/pyside-6.8.1.txt", "docs/licenses/simple-icons.txt", "docs/licenses/runtime.txt",
)


def main():
    # Read and validate everything before creating an archive.
    contents = []
    for name in FILES:
        path = ROOT / name
        for part in (path, *path.parents):
            if part == ROOT:
                break
            attributes = getattr(part.lstat(), "st_file_attributes", 0)
            if part.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"Linked paths cannot be exported: {name}")
        if not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"File outside project: {name}")
        contents.append((name, path.read_bytes()))
    destination = ROOT / "exports"
    destination.mkdir(exist_ok=True)
    archive = destination / "Gauge-source.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for name, data in contents:
            bundle.writestr(f"Gauge/{name}", data)
    digest = sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"Exported {len(contents)} files: {archive}")
    print(f"SHA256: {digest}")


if __name__ == "__main__":
    main()
