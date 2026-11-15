import argparse
from getpass import getpass
import hashlib
from importlib.util import find_spec
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys


SOURCE = Path(__file__).resolve().parent
SCHEME = "insta-local"
FILES = ("app.py", "auth.py", "preview.py", "launch.pyw", "web/index.html", "web/app.js",
         "web/style.css", "web/login.html", "web/login.js")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true", help="Generate the first local password.")
    parser.add_argument("--no-open", action="store_true", help="Install without opening the app.")
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info < (3, 10):
        parser.exit(1, "Windows with Python 3.10 or later is required.\n")
    import winreg

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        parser.exit(1, "pythonw.exe was not found in the Python installation.\n")
    destination = Path(os.environ["LOCALAPPDATA"]) / "InstagramLocalDownloader"
    config = destination / ".local" / "config.json"
    command = f'"{pythonw}" "{destination / "launch.pyw"}"'
    registry_path = fr"Software\Classes\{SCHEME}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path + r"\shell\open\command") as key:
            old_command = winreg.QueryValueEx(key, "")[0]
    except FileNotFoundError:
        old_command = ""
    if old_command and not old_command.endswith(f' "{destination / "launch.pyw"}"'):
        parser.exit(1, "The insta-local URL scheme is already used by another application.\n")

    if any(find_spec(dependency) is None for dependency in ("instaloader", "truststore")):
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(SOURCE / "requirements.txt")], check=True)
    generated = None
    if not config.exists():
        candidate = "" if args.generate else getpass("Choose a password (12+ characters), or press Enter to generate one: ")
        if not candidate:
            generated = candidate = secrets.token_urlsafe(18)
        elif not 12 <= len(candidate) <= 256:
            parser.exit(1, "The password must contain 12 to 256 characters.\n")
        config.parent.mkdir(parents=True, exist_ok=True)
        with config.open("x", encoding="utf-8") as file:
            json.dump({"password_digest": hashlib.sha256(candidate.encode("utf-8")).hexdigest()}, file)

    for name in FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, target)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Instagram Local Downloader")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(key, r"shell\open\command") as action:
            winreg.SetValueEx(action, "", 0, winreg.REG_SZ, command)

    print(f"Installed: {destination}")
    if generated:
        print("First login password: " + generated)
    if not args.no_open:
        os.startfile(SCHEME + ":open")


if __name__ == "__main__":
    main()
