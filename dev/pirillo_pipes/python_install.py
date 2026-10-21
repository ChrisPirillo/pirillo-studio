"""Explicit checkout links with fresh-process import provenance; no package downloads."""
import os
from pathlib import Path
import subprocess


SITE = "import site, sys, sysconfig; print(sysconfig.get_paths()['purelib'] if sys.prefix != sys.base_prefix else site.getusersitepackages())"


def install(python, root, package, pth_name):
    """Link one top-level package in the selected interpreter, then prove its origin."""
    root = os.path.abspath(root)
    if (not isinstance(package, str) or not package.isidentifier()
            or not isinstance(pth_name, str) or Path(pth_name).name != pth_name
            or not pth_name.endswith(".pth") or any(c in pth_name + root for c in "\r\n\0")
            or not os.path.isdir(os.path.join(root, package))):
        raise ValueError("Install requires an existing package and a plain .pth filename and path")
    site = subprocess.run([python, "-c", SITE], check=True, capture_output=True, text=True).stdout.strip()
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(site, pth_name), "w") as stream:
        stream.write(root + "\n")
    got = subprocess.run([python, "-c",
                          f"import importlib; print(importlib.import_module({package!r}).__file__)"], cwd="/",
                         check=True, capture_output=True, text=True).stdout.strip()
    if os.path.dirname(got) != os.path.join(root, package):
        raise SystemExit(f"install: {python} imports {package} from {got}, not this checkout")
    return f"{python}: {site}/{pth_name}"
