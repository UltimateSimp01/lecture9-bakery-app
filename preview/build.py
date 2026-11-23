"""Build the GitHub Pages edition using the same UI, Python rules, and course inputs."""
import argparse
import hashlib
import io
import shutil
import sys
import tarfile
from pathlib import Path
from urllib.request import Request, urlopen

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import app

PYODIDE_VERSION = "314.0.8"
ARCHIVE_HASH = "ff3e5f9d9bc8e98ff30b99515535fc24d375994f47a978c61975d653e162ca37"
RUNTIME_FILES = ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm",
                 "python_stdlib.zip", "pyodide-lock.json")


def download(url):
    request = Request(url, headers={"User-Agent": "lecture9-bakery-preview-builder"})
    with urlopen(request, timeout=90) as response:
        return response.read()


def install_runtime(folder, archive):
    if archive is None:
        release = f"https://github.com/pyodide/pyodide/releases/download/{PYODIDE_VERSION}"
        contents = download(f"{release}/pyodide-core-{PYODIDE_VERSION}.tar.bz2")
    else:
        contents = archive.read_bytes()
    if hashlib.sha256(contents).hexdigest() != ARCHIVE_HASH:
        raise ValueError("The Pyodide runtime archive did not match the pinned release digest.")
    folder.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(contents), mode="r:bz2") as bundle:
        for name in RUNTIME_FILES:
            with bundle.extractfile(f"pyodide/{name}") as source, (folder / name).open("wb") as target:
                shutil.copyfileobj(source, target)
    for name, upstream in {
        "LICENSE-pyodide.txt": f"pyodide/pyodide/{PYODIDE_VERSION}/LICENSE",
        "LICENSE-python.txt": "python/cpython/v3.14.2/LICENSE",
    }.items():
        (folder / name).write_bytes(download("https://raw.githubusercontent.com/" + upstream))


def build(output, archive):
    output.mkdir(parents=True, exist_ok=True)
    start_script = "<script>"
    if app.PAGE_HTML.count(start_script) != 1:
        raise ValueError("The app's startup script needs an explicit browser-preview hook.")
    page = app.PAGE_HTML.replace(start_script, '<script src="./client.js"></script>\n' + start_script)
    (output / "index.html").write_text(page, encoding="utf-8")
    for name in ("app.py", "README.html", "existing_orders.csv", *app.DM_FILES.values()):
        shutil.copyfile(PROJECT / name, output / name)
    for name in ("client.js", "worker.mjs", "bridge.py"):
        shutil.copyfile(PROJECT / "preview" / name, output / name)
    install_runtime(output / "runtime", archive)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--runtime-archive", type=Path, help="Use a previously downloaded release archive")
    args = parser.parse_args()
    build(args.output, args.runtime_archive)
