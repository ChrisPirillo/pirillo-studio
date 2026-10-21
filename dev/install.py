"""Make pirillo_studio importable from each named Python, live from this checkout (no copy to go stale):
writes pirillo-studio.pth into its site-packages (a venv's own, else the user's), then imports the package to prove it.

usage: python3 dev/install.py <python> [<python> ...]
"""
import os, sys
from pirillo_pipes import python_install
from pirillo_pipes.python_install import SITE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install(python):
    return python_install.install(python, ROOT, "pirillo_studio", "pirillo-studio.pth")


if __name__ == "__main__":
    if len(sys.argv) < 2 or "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) > 1 else 2)
    print("\n".join(install(p) for p in sys.argv[1:]))
