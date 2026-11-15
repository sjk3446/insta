import argparse
from calendar import monthrange
from contextlib import closing
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import Lock, Thread
from urllib.parse import urlsplit
import webbrowser

from auth import PasswordGate
from preview import PhotoPreview


ROOT = Path(__file__).resolve().parent
DB = ROOT / ".local" / "state.sqlite3"
GATE = None
PORT = 17861
HEALTH_MARKER = "instagram-local-downloader-v1"
PICKER_LOCK = Lock()
INSPECT_LOCK = Lock()
ACTIVE_PREVIEW = None
NAME = re.compile(r"[a-z0-9_](?:[a-z0-9_.]{0,28}[a-z0-9_])?", re.ASCII)
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".mp4"}
MEDIA_KINDS = {"photos": "사진", "videos": "동영상"}
DOWNLOAD_KINDS = {"photos": {"photos"}, "videos": {"videos"}, "both": {"photos", "videos"}}
FILE_TIMESTAMP = "%Y-%m-%d_%H-%M-%S"
MEDIA_NAME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2})_UTC_[A-Za-z0-9_-]+")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/login": ("login.html", "text/html; charset=utf-8"),
    "/login.js": ("login.js", "text/javascript; charset=utf-8"),
}


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as db, db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                username TEXT PRIMARY KEY, memo TEXT NOT NULL,
                directory TEXT NOT NULL, mode TEXT NOT NULL,
                updated TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL, directory TEXT NOT NULL,
                mode TEXT NOT NULL, incremental INTEGER NOT NULL, memo TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running', added INTEGER DEFAULT 0,
                started TEXT DEFAULT CURRENT_TIMESTAMP, ended TEXT,
                date_from TEXT, date_to TEXT, since_last INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL,
                time TEXT DEFAULT CURRENT_TIMESTAMP, message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
            CREATE TABLE IF NOT EXISTS checked (
                username TEXT NOT NULL, directory TEXT NOT NULL, kind TEXT NOT NULL,
                shortcode TEXT NOT NULL, PRIMARY KEY (username, directory, kind, shortcode)
            );
            CREATE TABLE IF NOT EXISTS sessions (token BLOB PRIMARY KEY, expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS download_watermarks (
                username TEXT NOT NULL, directory TEXT NOT NULL, kind TEXT NOT NULL,
                published_utc TEXT, PRIMARY KEY (username, directory, kind)
            );
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
        if "date_from" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN date_from TEXT")
        if "date_to" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN date_to TEXT")
        if "since_last" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN since_last INTEGER NOT NULL DEFAULT 0")
        db.execute("""
            INSERT INTO events(run_id, message)
            SELECT id, '앱이 종료되어 작업이 중단되었습니다.' FROM runs WHERE status = 'running'
        """)
        db.execute("""
            UPDATE runs SET status = 'interrupted', ended = CURRENT_TIMESTAMP
            WHERE status = 'running'
        """)


def username_from(value):
    raw = value.strip().lower()
    if raw.startswith(("http://", "https://")):
        url = urlsplit(raw)
        if url.netloc not in {"instagram.com", "www.instagram.com"}:
            raise ValueError("인스타그램 프로필 주소를 입력해 주세요.")
        raw = url.path.removeprefix("/").removesuffix("/")
    else:
        raw = raw.removeprefix("@")
    if not NAME.fullmatch(raw):
        raise ValueError("인스타그램 ID 또는 프로필 주소를 입력해 주세요.")
    return raw


def post_period(data):
    option = data.get("period", "all")
    if option == "all":
        return None, None
    if option in ("month", "quarter"):
        today = date.today()
        months = 1 if option == "month" else 3
        year, month_index = divmod(today.year * 12 + today.month - 1 - months, 12)
        month = month_index + 1
        return date(year, month, min(today.day, monthrange(year, month)[1])), today
    if option != "custom":
        raise ValueError("게시물 기간을 다시 선택해 주세요.")
    try:
        start, end = (date.fromisoformat(data.get(field, "")) for field in ("date_from", "date_to"))
    except (TypeError, ValueError):
        raise ValueError("시작일과 종료일을 입력해 주세요.") from None
    if start > end:
        raise ValueError("종료일은 시작일과 같거나 그 이후여야 합니다.")
    return start, end


def event(db, run_id, message):
    message = str(message).strip()
    if message:
        with db:
            db.execute("INSERT INTO events(run_id, message) VALUES (?, ?)", (run_id, message[:2500]))


def media_files(path):
    if not path.is_dir():
        return set()
    return {p.name for p in path.iterdir() if p.suffix.lower() in MEDIA_SUFFIXES and p.is_file()}


def latest_by_kind(files, kinds=MEDIA_KINDS, date_from=None, date_to=None):
    latest = {}
    for filename in files:
        path = Path(filename)
        match = MEDIA_NAME.fullmatch(path.stem)
        if not match:
            continue
        stamp = match[1]
        try:
            utc_date = datetime.strptime(stamp, FILE_TIMESTAMP).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        kind = "videos" if path.suffix.lower() == ".mp4" else "photos"
        if kind not in kinds:
            continue
        if date_from is not None and not date_from <= utc_date.astimezone().date() <= date_to:
            continue
        latest[kind] = max(latest.get(kind, ""), stamp)
    return latest


def download_watermarks(db, key, destination):
    current = dict(db.execute(
        "SELECT kind, published_utc FROM download_watermarks WHERE username = ? AND directory = ?", key
    ))
    if not current:
        # Adopt archives created before the app began recording publication cutoffs.
        latest = latest_by_kind(media_files(destination))
        current = {kind: latest.get(kind) for kind in MEDIA_KINDS}
        db.executemany("INSERT INTO download_watermarks VALUES (?, ?, ?, ?)", (
            (*key, kind, stamp) for kind, stamp in current.items()
        ))
    return {kind: stamp for kind, stamp in current.items() if stamp is not None}


def resolve_destination(data, username, required=True):
    entered = data.get("directory", "")
    if not required and not entered:
        return None
    selected = Path(entered)
    if not selected.is_absolute() or not selected.is_dir():
        raise ValueError("PC에 있는 저장 폴더를 선택해 주세요.")
    directory = selected.resolve(strict=True)
    if os.name == "nt" and directory.drive.startswith("\\\\"):
        raise ValueError("PC의 로컬 드라이브에 있는 폴더를 선택해 주세요.")
    destination = directory / f"instagram_{username}"
    if destination.resolve() != destination or (destination.exists() and not destination.is_dir()):
        raise ValueError("계정 폴더로 사용할 수 없는 경로입니다. 다른 저장 폴더를 선택해 주세요.")
    return destination


def resolve_cutoffs(db, username, destination, mode, since_last):
    key = (username, os.path.normcase(str(destination)))
    watermarks = download_watermarks(db, key, destination)
    if not since_last:
        return {}
    if not watermarks:
        raise ValueError("이 저장 폴더에 이전 다운로드가 없습니다. 먼저 이 옵션을 끄고 받아 주세요.")
    last_known = max(watermarks.values())
    return {kind: watermarks.get(kind, last_known) for kind in DOWNLOAD_KINDS[mode]}


def download(run_id, username, destination, mode, incremental, date_from, date_to, cutoffs, selected_codes):
    with closing(connect()) as db:
        status, added = "failed", 0
        before = None
        loader = None
        finished_source = False
        phase = "준비"
        try:
            import instaloader

            destination.mkdir(exist_ok=True)
            before = media_files(destination)
            selected = DOWNLOAD_KINDS[mode]
            key = (username, os.path.normcase(str(destination)))
            completed = {kind: set() for kind in selected}
            if incremental:
                for kind, shortcode in db.execute(
                    "SELECT kind, shortcode FROM checked WHERE username = ? AND directory = ?", key
                ):
                    if kind in completed:
                        completed[kind].add(shortcode)

            loader = instaloader.Instaloader(
                dirname_pattern=str(destination).replace("{", "{{").replace("}", "}}"),
                filename_pattern="{date_utc}_UTC_{shortcode}",
                download_video_thumbnails=False,
                save_metadata=False,
                post_metadata_txt_pattern="",
                request_timeout=30,
                max_connection_attempts=2,
                fatal_status_codes=[401, 403, 429],
            )
            loader.context.log = lambda *msg, sep="", **kwargs: event(
                db, run_id, sep.join(str(part) for part in msg)
            )
            original_error = loader.context.error

            def report_error(message, repeat_at_end=True):
                original_error(message, repeat_at_end)
                event(db, run_id, message)

            loader.context.error = report_error
            event(db, run_id, f"@{username} 다운로드 시작 · {destination}")
            if date_from is not None:
                event(db, run_id, f"게시물 등록일 · {date_from} ~ {date_to} (이 PC의 날짜 기준)")
            for kind, stamp in sorted(cutoffs.items()):
                local_stamp = datetime.strptime(stamp, FILE_TIMESTAMP).replace(tzinfo=timezone.utc).astimezone()
                event(db, run_id, f"{MEDIA_KINDS[kind]} 지난 다운로드 기준 · {local_stamp:%Y-%m-%d %H:%M:%S}")
            skipped, processed, outside_period, older = 0, 0, 0, 0
            if selected_codes:
                event(db, run_id, f"선택한 게시물 {len(selected_codes)}개")
                sources = [("선택한 게시물", lambda: (
                    instaloader.Post.from_shortcode(loader.context, code) for code in selected_codes
                ))]
            else:
                profile = instaloader.Profile.from_username(loader.context, username)
                if profile.is_private:
                    raise ValueError("비공개 계정입니다. 이 앱에서는 공개 계정만 지원합니다.")
                sources = [("피드", profile.get_posts)]
                if "videos" in selected:
                    sources.append(("릴스", profile.get_reels))
            for label, get_posts in sources:
                phase = label
                event(db, run_id, f"{label} 확인 중")
                for post in get_posts():
                    if date_from is not None and not date_from <= post.date_local.date() <= date_to:
                        outside_period += 1
                        continue
                    shortcode = post.shortcode
                    kinds = {kind for kind in selected if shortcode not in completed[kind]}
                    if label == "릴스":
                        kinds.discard("photos")
                    if kinds and cutoffs:
                        stamp = post.date_utc.strftime(FILE_TIMESTAMP)
                        eligible = {kind for kind in kinds if stamp >= cutoffs[kind]}
                        if not eligible:
                            older += 1
                            continue
                        kinds = eligible
                    if not kinds:
                        skipped += 1
                        continue
                    loader.download_pictures = "photos" in kinds
                    loader.download_videos = "videos" in kinds
                    errors_before = len(loader.context.error_log)
                    loader.download_post(post, target=username)
                    if len(loader.context.error_log) > errors_before:
                        continue
                    with db:
                        db.executemany("INSERT OR IGNORE INTO checked VALUES (?, ?, ?, ?)", (
                            (*key, kind, shortcode) for kind in kinds
                        ))
                    for kind in kinds:
                        completed[kind].add(shortcode)
                    processed += 1
                finished_source = True
            status = "failed" if loader.context.error_log else "completed"
            event(db, run_id, f"처리한 게시물 {processed}개 · 건너뛴 게시물 {skipped}개")
            if date_from is not None:
                event(db, run_id, f"선택 기간 밖 게시물 {outside_period}개 제외")
            if cutoffs:
                event(db, run_id, f"지난 다운로드 기준보다 오래된 게시물 {older}개 제외")
        except Exception as error:
            event(db, run_id, f"{phase} 오류 · {error}")
        finally:
            if loader:
                loader.close()
            latest = {}
            if before is not None:
                try:
                    saved = media_files(destination)
                    added = len(saved - before)
                    if status == "completed":
                        latest = latest_by_kind(saved - before if selected_codes else saved, selected, date_from, date_to)
                except OSError as error:
                    event(db, run_id, f"저장 파일 수를 확인할 수 없습니다 · {error}")
            if status == "failed" and (finished_source or added):
                status = "partial"
            event(db, run_id, f"새로 저장된 파일 {added}개")
            with db:
                # An incomplete scan may still have gaps older than its newest file.
                db.executemany("""
                    UPDATE download_watermarks
                    SET published_utc = CASE
                        WHEN published_utc IS NULL OR published_utc < ? THEN ? ELSE published_utc END
                    WHERE username = ? AND directory = ? AND kind = ?
                """, ((stamp, stamp, *key, kind) for kind, stamp in latest.items()))
                db.execute("""
                    UPDATE runs SET status = ?, added = ?, ended = CURRENT_TIMESTAMP WHERE id = ?
                """, (status, added, run_id))


def choose_folder():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(parent=root, mustexist=True, title="인스타그램 파일을 저장할 폴더")
        print(json.dumps(path))
    finally:
        root.destroy()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_data(self, status, data, content_type, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' https://*.cdninstagram.com https://*.fbcdn.net; "
                         "frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def reply(self, data, status=200, headers=()):
        self.send_data(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

    def redirect(self, location):
        self.send_data(303, b"", "text/plain; charset=utf-8", (("Location", location),))

    def authenticated(self):
        if GATE is None:
            return True
        with closing(connect()) as db:
            return GATE.authorized(db, self.headers.get("Cookie", ""))

    def valid_host(self):
        if self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}":
            return True
        self.reply({"error": "앱을 로컬 주소로 열어 주세요."}, 403)
        return False

    def do_GET(self):
        if not self.valid_host():
            return
        if self.path == "/health":
            self.reply({"service": HEALTH_MARKER})
            return
        if self.path == "/login" and self.authenticated():
            self.redirect("/")
            return
        if self.path not in {"/style.css", "/login", "/login.js"} and not self.authenticated():
            if self.path == "/":
                self.redirect("/login")
            else:
                self.reply({"error": "비밀번호를 입력해 주세요."}, 401)
            return
        if self.path in ASSETS:
            name, content_type = ASSETS[self.path]
            self.send_data(200, (ROOT / "web" / name).read_bytes(), content_type)
            return
        with closing(connect()) as db:
            if self.path == "/api/state":
                accounts = db.execute("SELECT * FROM accounts ORDER BY updated DESC, username")
                runs = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 100")
                self.reply({"accounts": list(map(dict, accounts)), "runs": list(map(dict, runs))})
                return
            match = re.fullmatch(r"/api/runs/([0-9]{1,15})/events", self.path)
            if match:
                rows = db.execute("""
                    SELECT * FROM (
                        SELECT time, message, id FROM events WHERE run_id = ? ORDER BY id DESC LIMIT 300
                    ) ORDER BY id
                """, (int(match[1]),))
                self.reply({"events": list(map(dict, rows))})
                return
        self.reply({"error": "주소를 찾을 수 없습니다."}, 404)

    def do_POST(self):
        if not self.valid_host():
            return
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Origin") != origin or self.headers.get("X-Local-App") != "1":
            self.reply({"error": "이 웹 화면에서 요청해 주세요."}, 403)
            return
        try:
            if self.path == "/api/login" and GATE is not None:
                data = self.read_json()
                if not isinstance(data.get("password"), str):
                    raise ValueError("비밀번호를 입력해 주세요.")
                with closing(connect()) as db:
                    status, cookie = GATE.login(db, data["password"])
                if status == 200:
                    self.reply({"ok": True}, headers=(("Set-Cookie", cookie),))
                elif status == 429:
                    self.reply({"error": "입력 시도가 많습니다. 1분 뒤 다시 시도해 주세요."}, 429)
                else:
                    self.reply({"error": "비밀번호가 올바르지 않습니다."}, 401)
                return
            if not self.authenticated():
                self.reply({"error": "비밀번호를 입력해 주세요."}, 401)
                return
            if self.path == "/api/folder":
                self.pick()
                return
            if self.path == "/api/photo-preview":
                with INSPECT_LOCK:
                    self.preview_photos(self.read_json())
                return
            if self.path != "/api/runs":
                self.reply({"error": "주소를 찾을 수 없습니다."}, 404)
                return
            with INSPECT_LOCK:
                self.start_download(self.read_json())
        except (ValueError, TypeError, KeyError, UnicodeError, AttributeError) as error:
            self.reply({"error": str(error)}, 400)
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as error:
            self.reply({"error": f"작업을 시작하지 못했습니다 · {error}"}, 500)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 20_000 or self.headers.get_content_type() != "application/json":
            raise ValueError("요청 형식이 올바르지 않습니다.")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("요청 형식이 올바르지 않습니다.")
        return data

    def preview_photos(self, data):
        global ACTIVE_PREVIEW
        import instaloader

        with closing(connect()) as db, db:
            if db.execute("SELECT 1 FROM runs WHERE status = 'running'").fetchone():
                self.reply({"error": "다운로드가 끝난 뒤 사진을 미리볼 수 있습니다."}, 409)
                return
            preview_id = data.get("preview_id")
            if preview_id:
                if ACTIVE_PREVIEW is None or preview_id != ACTIVE_PREVIEW.id:
                    raise ValueError("미리보기를 다시 시작해 주세요.")
                self.reply(ACTIVE_PREVIEW.page())
                return
            username = username_from(data["username"])
            since_last = data.get("since_last", False)
            destination = resolve_destination(data, username, required=since_last)
            date_from, date_to = post_period(data)
            cutoff, checked = None, set()
            if destination is not None:
                cutoff = resolve_cutoffs(db, username, destination, "photos", since_last).get("photos")
                if since_last or data.get("incremental", True):
                    key = (username, os.path.normcase(str(destination)))
                    checked = {row[0] for row in db.execute("""
                        SELECT shortcode FROM checked WHERE username = ? AND directory = ? AND kind = 'photos'
                    """, key)}
        if ACTIVE_PREVIEW:
            ACTIVE_PREVIEW.close()
            ACTIVE_PREVIEW = None
        loader = instaloader.Instaloader(
            request_timeout=30, max_connection_attempts=2, fatal_status_codes=[401, 403, 429]
        )
        try:
            profile = instaloader.Profile.from_username(loader.context, username)
            if profile.is_private:
                raise ValueError("비공개 계정입니다. 이 앱에서는 공개 계정만 지원합니다.")
            ACTIVE_PREVIEW = PhotoPreview(
                loader, profile.get_posts(), username, destination, date_from, date_to, cutoff, checked
            )
        except Exception as error:
            loader.close()
            raise ValueError(f"미리보기를 불러오지 못했습니다 · {error}") from error
        self.reply(ACTIVE_PREVIEW.page())

    def pick(self):
        if not PICKER_LOCK.acquire(blocking=False):
            self.reply({"error": "이미 열린 폴더 선택 창에서 경로를 선택해 주세요."}, 409)
            return
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            python_console = Path(sys.executable)
            if python_console.name.lower() == "pythonw.exe":
                python_console = python_console.with_name("python.exe")
            result = subprocess.run(
                [str(python_console), str(ROOT / "app.py"), "--pick-folder"],
                capture_output=True, text=True, encoding="utf-8", creationflags=flags,
                check=True, timeout=180,
            )
            self.reply({"directory": json.loads(result.stdout)})
        except subprocess.SubprocessError:
            self.reply({"error": "폴더 선택 창을 열거나 완료할 수 없습니다. 폴더 경로를 직접 입력해 주세요."}, 503)
        finally:
            PICKER_LOCK.release()

    def start_download(self, data):
        username = username_from(data["username"])
        memo = data["memo"].strip()
        if len(memo) > 300:
            raise ValueError("메모는 300자까지 입력할 수 있습니다.")
        mode = data["mode"]
        incremental = data["incremental"]
        since_last = data.get("since_last", False)
        if mode not in DOWNLOAD_KINDS or type(incremental) is not bool or type(since_last) is not bool:
            raise ValueError("다운로드 옵션을 다시 선택해 주세요.")
        incremental = incremental or since_last
        date_from, date_to = post_period(data)
        destination = resolve_destination(data, username)
        directory = destination.parent
        selected_codes = ()
        if "preview_id" in data:
            requested_codes = data.get("selected")
            if (
                ACTIVE_PREVIEW is None or ACTIVE_PREVIEW.id != data["preview_id"] or
                ACTIVE_PREVIEW.username != username or ACTIVE_PREVIEW.destination not in (None, destination) or
                mode != "photos" or not isinstance(requested_codes, list) or not requested_codes or
                any(not isinstance(code, str) or code not in ACTIVE_PREVIEW.shown for code in requested_codes)
            ):
                raise ValueError("사진 미리보기를 새로 열고 저장할 게시물을 선택해 주세요.")
            selected_codes = tuple(dict.fromkeys(requested_codes))
        with closing(connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runs WHERE status = 'running'").fetchone():
                self.reply({"error": "이미 다운로드 작업이 실행 중입니다."}, 409)
                return
            cutoffs = resolve_cutoffs(db, username, destination, mode, since_last)
            db.execute("""
                INSERT INTO accounts(username, memo, directory, mode) VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET memo=excluded.memo,
                    directory=excluded.directory, mode=excluded.mode, updated=CURRENT_TIMESTAMP
            """, (username, memo, str(directory), mode))
            run_id = db.execute("""
                INSERT INTO runs(username, directory, mode, incremental, memo, date_from, date_to, since_last)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (username, str(destination), mode, incremental, memo,
                  date_from.isoformat() if date_from else None,
                  date_to.isoformat() if date_to else None, since_last)).lastrowid
        Thread(target=download, args=(run_id, username, destination, mode, incremental, date_from, date_to,
                                      cutoffs, selected_codes),
               daemon=True).start()
        self.reply({"id": run_id}, 202)


def serve_installed(open_browser=True):
    global DB, GATE
    data = ROOT / ".local"
    DB = data / "state.sqlite3"
    config = json.loads((data / "config.json").read_text(encoding="utf-8"))
    GATE = PasswordGate.from_digest(config["password_digest"])
    with ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as server:
        initialize()
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pick-folder", action="store_true")
    args = parser.parse_args()
    if args.pick_folder:
        choose_folder()
        return
    serve_installed()


if __name__ == "__main__":
    main()
