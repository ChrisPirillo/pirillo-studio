"""Make pirillo_studio importable from each named Python, live from this checkout (no copy to go stale):
writes pirillo-studio.pth into its site-packages (a venv's own, else the user's), then imports the package to prove it.

usage: python3 dev/install.py <python> [<python> ...]
"""
import os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = "import site, sys, sysconfig; print(sysconfig.get_paths()['purelib'] if sys.prefix != sys.base_prefix else site.getusersitepackages())"


def install(python):
    site = subprocess.run([python, "-c", SITE], check=True, capture_output=True, text=True).stdout.strip()
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(site, "pirillo-studio.pth"), "w") as f:
        f.write(ROOT + "\n")
    got = subprocess.run([python, "-c", "import pirillo_studio; print(pirillo_studio.__file__)"], cwd="/",
                         check=True, capture_output=True, text=True).stdout.strip()
    if os.path.dirname(got) != os.path.join(ROOT, "pirillo_studio"):
        raise SystemExit(f"install: {python} imports pirillo_studio from {got}, not this checkout")
    return f"{python}: {site}/pirillo-studio.pth"


if __name__ == "__main__":
    if len(sys.argv) < 2 or "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) > 1 else 2)
    print("\n".join(install(p) for p in sys.argv[1:]))
