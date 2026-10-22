"""MCP_PACKAGE, ENV_PACKAGES, doctor: moved from the host by dev/port.py; `s` is the host (pirillo_studio.host.View)."""
import os
import shutil
import subprocess
import sys


MCP_PACKAGE = "mcp==2.3.0"


ENV_PACKAGES = ["faster-whisper", "numpy", "pillow", "opencv-python-headless", "torch", "torchaudio", "yt-dlp", MCP_PACKAGE]


def doctor(s, fix=False):
    """Is the runtime there? Python venv, Whisper, ffmpeg, the Whisper model cache. With --fix,
    in a Cowork device shell (Linux), build Studio/env-cowork in the background. Nothing is ever
    taken from "Safe to delete/"."""
    import platform
    studio = s.places.path(s.HOME, "Studio")
    env = os.path.join(studio, "env" if platform.system() == "Darwin" else "env-cowork")
    py = os.path.join(env, "bin", "python")
    lines = [f"runtime: {env}"]
    ok = os.path.exists(py)
    if ok:
        r = subprocess.run([py, "-c", "import faster_whisper, numpy, PIL, cv2; print(faster_whisper.__version__)"],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        lines.append(f"python + faster-whisper: {'ok ' + r.stdout.strip() if ok else 'MISSING (' + (r.stderr.strip().splitlines() or ['?'])[-1] + ')'}")
    else:
        lines.append("python venv: MISSING")
    asr_py = os.path.join(studio, "tools", "asr.py")
    if os.path.exists(py) and os.path.exists(asr_py):      # which engine a new transcript gets here, and a fallback's why (asr.py)
        r = subprocess.run([py, asr_py], capture_output=True, text=True)
        lines.append("speech engine: " + "; ".join(x[5:] if x.startswith("asr: ") else x for x in r.stdout.strip().splitlines() or ["?"]))
    if os.path.exists(py):      # archive.py fetches his own old videos with it (references/cut.md, "His own old videos")
        r = subprocess.run([py, "-c", "import yt_dlp; print(yt_dlp.version.__version__)"], capture_output=True, text=True)
        lines.append("yt-dlp: " + (f"ok {r.stdout.strip()}" if r.returncode == 0
                                   else f"MISSING (archive.py needs it: {py} -m pip install yt-dlp)"))
        if r.returncode != 0 and fix:
            subprocess.run([py, "-m", "pip", "install", "--quiet", "yt-dlp"], capture_output=True)
            lines.append("yt-dlp: installed")
    if os.path.exists(py):
        try:
            r = subprocess.run([py, "-c", "from mcp.server import MCPServer; from importlib.metadata import version; print(version('mcp'))"],
                               capture_output=True, text=True, timeout=10)
            available = r.returncode == 0 and r.stdout.strip() == MCP_PACKAGE.split("==")[1]
        except (OSError, subprocess.TimeoutExpired) as error:
            available = False
            lines.append("MCP SDK probe: " + str(error))
        lines.append("MCP SDK: " + ("ok " + r.stdout.strip() if available else
                     f"MISSING (RPC transport checks need {MCP_PACKAGE}: {py} -m pip install {MCP_PACKAGE})"))
        if not available and fix:
            try:
                r = subprocess.run([py, "-m", "pip", "install", "--quiet", MCP_PACKAGE], capture_output=True, text=True,
                                   env=dict(os.environ, PIP_NO_CACHE_DIR="1", PIP_CONFIG_FILE=os.devnull), timeout=120)
                lines.append("MCP SDK install: " + ("ok" if r.returncode == 0 else "FAILED (" + r.stderr[-1000:].strip() + ")"))
            except (OSError, subprocess.TimeoutExpired) as error:
                lines.append("MCP SDK install: FAILED (" + str(error) + ")")
    ff = shutil.which("ffmpeg", path=os.path.join(env, "bin") + os.pathsep + os.environ.get("PATH", ""))
    lines.append(f"ffmpeg: {ff or 'MISSING'}")
    hf = os.path.join(studio, "assets", "models", "hf")
    hub = os.path.join(hf, "hub")
    have = sorted(x.split("--")[-1] for x in os.listdir(hub) if x.startswith("models--")) if os.path.isdir(hub) else []
    lines.append(f"whisper models: {', '.join(have) or 'none cached yet (downloads on first use)'}")
    if fix and not ok and platform.system() != "Darwin":
        if not os.path.exists(py):
            subprocess.run([sys.executable, "-m", "venv", env], check=True)
        log_p = os.path.join(env, "install.log")
        subprocess.Popen(f'nohup "{py}" -m pip install --quiet {" ".join(ENV_PACKAGES)} > "{log_p}" 2>&1 &', shell=True)
        lines.append(f"installing in the background; check again with `studio.py doctor` (log: {log_p})")
    elif fix and not ok:
        lines.append(s.MAC_ENV_NOTE)
    lines += s.spotlight_lines()
    lines.append("then:  . " + os.path.join(studio, "tools", "env.sh"))
    return "\n".join(lines)
