"""Flow — one command. Sets up everything, opens the simple TUI.

    python flow.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
WIN = os.name == "nt"
VENV_PY = os.path.join(ROOT, ".venv", "Scripts" if WIN else "bin",
                       "python.exe" if WIN else "python")
REQ = os.path.join(ROOT, "requirements.txt")


def ensure_env():
    if os.path.exists(VENV_PY):
        return VENV_PY
    print("[*] first run: creating isolated env (one time, ~1 min)...")
    try:
        subprocess.check_call([sys.executable, "-m", "venv",
                               os.path.join(ROOT, ".venv")])
        vp = VENV_PY
        subprocess.check_call([vp, "-m", "pip", "install", "--upgrade",
                               "pip", "-q"])
        if os.path.exists(REQ):
            subprocess.check_call([vp, "-m", "pip", "install", "-q",
                                   "-r", REQ])
        print("[*] env ready.")
        return vp
    except Exception as e:
        print(f"[!] auto-setup failed: {e}")
        print("    manual: python -m venv .venv && pip install -r requirements.txt")
        sys.exit(1)


def main():
    os.chdir(ROOT)
    vp = ensure_env()
    sys.exit(subprocess.call([vp, "-u", os.path.join(ROOT, "menu.py")]
                             + sys.argv[1:]))


if __name__ == "__main__":
    main()
