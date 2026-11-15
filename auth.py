from collections import deque
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import re
import secrets
from threading import Lock
import time


SESSION_AGE = 30 * 24 * 60 * 60
COOKIE = "instagram_local_session"
SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class PasswordGate:
    def __init__(self, password, secure):
        self.password_digest = hashlib.sha256(password.encode("utf-8")).digest()
        self.secure = secure
        self.failures = deque()
        self.lock = Lock()

    @classmethod
    def from_digest(cls, digest):
        gate = cls("", secure=False)
        gate.password_digest = bytes.fromhex(digest)
        if len(gate.password_digest) != hashlib.sha256().digest_size:
            raise ValueError("Invalid local password digest.")
        return gate

    def token_digest(self, token):
        return hmac.digest(self.password_digest, token.encode("ascii"), "sha256")

    def login(self, db, password):
        now = time.monotonic()
        supplied = hashlib.sha256(password.encode("utf-8")).digest()
        with self.lock:
            while self.failures and now - self.failures[0] >= 60:
                self.failures.popleft()
            if len(self.failures) >= 8:
                return 429, None
            if not hmac.compare_digest(self.password_digest, supplied):
                self.failures.append(now)
                return 401, None
            self.failures.clear()
        token = secrets.token_urlsafe(32)
        with db:
            now_unix = int(time.time())
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now_unix,))
            db.execute("INSERT INTO sessions VALUES (?, ?)", (self.token_digest(token), now_unix + SESSION_AGE))
        cookie = f"{COOKIE}={token}; Path=/; Max-Age={SESSION_AGE}; HttpOnly; SameSite=Strict"
        return 200, cookie + ("; Secure" if self.secure else "")

    def authorized(self, db, cookie_header):
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except CookieError:
            return False
        token = cookies[COOKIE].value if COOKIE in cookies else ""
        if not SESSION_TOKEN.fullmatch(token):
            return False
        return db.execute(
            "SELECT 1 FROM sessions WHERE token = ? AND expires > ?",
            (self.token_digest(token), int(time.time())),
        ).fetchone() is not None
