"""Collect managed PyAV without importing its DLL-dependent extensions."""

import importlib.util
from pathlib import Path

from build import native_target
from pyav_source import WORK, _library_files

spec = importlib.util.find_spec("av")
if spec is None or not spec.submodule_search_locations:
    raise RuntimeError("Managed PyAV package is missing during freezing")
package = Path(next(iter(spec.submodule_search_locations)))
modules = {"av"}
for path in package.rglob("*"):
    if path.is_file() and path.suffix in (".py", ".pyd", ".so"):
        parts = list(path.relative_to(package).parts)
        parts[-1] = parts[-1].split(".", 1)[0]
        if parts[-1] == "__init__":
            parts.pop()
        modules.add(".".join(["av", *parts]))
if not {"av._core", "av.frame", "av.audio.frame", "av.video.frame"}.issubset(modules):
    raise RuntimeError("Managed PyAV package is missing required frame extensions")
hiddenimports = sorted(modules | {"fractions", "dataclasses", "uuid"})
target = native_target()
binaries = [
    (str(path), ".")
    for path in _library_files(WORK / "pyav" / target / "prefix", target)
]
