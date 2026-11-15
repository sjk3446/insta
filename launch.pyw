from contextlib import closing
import ctypes
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
from time import sleep
import traceback
import webbrowser

from app import HEALTH_MARKER, PORT, serve_installed


def open_existing():
    try:
        with closing(HTTPConnection("127.0.0.1", PORT, timeout=.4)) as connection:
            connection.request("GET", "/health")
            response = connection.getresponse()
            data = json.loads(response.read(1024))
            if response.status == 200 and data.get("service") == HEALTH_MARKER:
                webbrowser.open(f"http://127.0.0.1:{PORT}/")
                return True
    except (OSError, ValueError, AttributeError):
        pass
    return False


def launch():
    if open_existing():
        return
    try:
        serve_installed()
    except OSError:
        for _ in range(4):
            sleep(.15)
            if open_existing():
                return
        raise


if __name__ == "__main__":
    logfile = Path(__file__).resolve().parent / ".local" / "launcher.log"
    with logfile.open("a", encoding="utf-8", buffering=1) as stream:
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
        try:
            launch()
        except Exception:
            traceback.print_exc(file=stream)
            ctypes.windll.user32.MessageBoxW(
                None, "다운로드 프로그램을 열지 못했습니다. 설치 폴더의 .local/launcher.log를 확인해 주세요.",
                "Instagram 로컬 다운로드", 0x10,
            )
