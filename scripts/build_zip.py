"""Build the installable Blender extension zip: `python scripts/build_zip.py [output-dir]`.

The zip holds the add-on's files at its root (what Blender's "Install from Disk" expects), without tests or caches.
"""
import sys
import tomllib
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "blender" / "partyline_relay"


def build(output):
    manifest = tomllib.loads((SOURCE / "blender_manifest.toml").read_text())
    target = Path(output) / f"{manifest['id']}-{manifest['version']}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in SOURCE.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                   and p.suffix != ".pyc" and p.name != "README.md")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(SOURCE).as_posix())
    return target


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "dist"))
