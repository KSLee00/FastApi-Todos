import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

APP_VERSION = "6.0.0"                            # 화면 배지와 /version 이 함께 쓰는 단일 출처
ENCODING = "utf-8"

BASE_DIR = Path(__file__).resolve().parent       # main.py 가 있는 폴더
INDEX_FILE = BASE_DIR / "templates" / "index.html"

# 데이터는 컨테이너 밖(Docker 볼륨)에 두어야 다시 배포해도 남는다 → docker-compose.yml 의 todo_data
DATA_DIR = Path(os.environ.get("TODO_DATA_DIR", BASE_DIR / "data"))
DB_FILE = DATA_DIR / "todos.db"
# 초기 데이터 / v4 까지 쓰던 JSON 파일 — DB 가 비어 있을 때 한 번만 옮겨 담는다 (앞에 있는 것이 우선)
# fastapi-app/todo.json 은 빈 목록([])으로 레포에 두고, 실제 데이터는 DB 에 쌓인다
LEGACY_FILES = [DATA_DIR / "todo.json", BASE_DIR / "todo.json"]

# "오늘"의 기준 — 컨테이너 기본 시간대는 UTC 라서 그대로 쓰면 반복·통계가 오전 9시에 바뀐다
APP_TZ = ZoneInfo(os.environ.get("APP_TZ", "Asia/Seoul"))

# v5 까지 만든 할 일(주인 없음)을 가져갈 아이디. 비워 두면 아무도 가져가지 않는다.
# "첫 가입자"로 정하지 않는 이유: 배포 직후 통합 테스트의 일회용 계정이 먼저 가입하면 기존 데이터를 가져간 뒤 함께 지워진다.
LEGACY_OWNER = os.environ.get("LEGACY_OWNER", "").strip()
# HTTPS 로 서비스할 때만 1 — 1 이면 쿠키가 HTTPS 에서만 전송된다
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
# API 문서(/docs, /redoc, /openapi.json) — 엔드포인트 목록이 그대로 드러나므로 기본은 끈다
ENABLE_DOCS = os.environ.get("ENABLE_DOCS", "0") == "1"

MAX_SUBTASKS = 20
MAX_TAGS = 5
STATS_DAYS = 7                                   # 통계 막대에 보여 줄 최근 일수

SESSION_COOKIE = "todo_session"
LOGIN_REQUIRED = "로그인이 필요합니다."
SESSION_DAYS = 7
MAX_FAILED_LOGINS = 5                            # 연속으로 틀리면
LOCK_MINUTES = 5                                 # 이만큼 로그인을 막는다
SCRYPT_N = 2 ** 15                               # 비밀번호 해시 비용 (테스트에서는 낮춰서 빠르게 돌린다)


def today() -> date:
    return datetime.now(APP_TZ).date()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- 데이터 모델 ----------

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
Repeat = Literal["none", "daily", "weekly"]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=20,
                                            pattern=r"^[A-Za-z0-9_]+$")]


class Subtask(BaseModel):                        # 할 일 안의 작은 단계
    model_config = ConfigDict(str_strip_whitespace=True)   # "   " 같은 공백 제목은 빈 제목으로 보고 거부

    title: str = Field(min_length=1, max_length=100)
    done: bool = False


class TodoIn(BaseModel):                         # 클라이언트가 보내는 데이터 (id 없음)
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    completed: bool = False
    due_date: date | None = None                 # 마감일 (YYYY-MM-DD), 없으면 null
    progress: int = Field(0, ge=0, le=100)       # 진행도 (0~100%), 기존 데이터는 0%로 읽힌다
    subtasks: list[Subtask] = Field(default_factory=list, max_length=MAX_SUBTASKS)
    tags: list[Tag] = Field(default_factory=list, max_length=MAX_TAGS)
    repeat: Repeat = "none"                      # 완료해도 다음 날(daily)·다음 주(weekly)에 다시 미완료로

    @model_validator(mode="after")
    def normalize(self) -> "TodoIn":
        # 하위 작업이 있으면 진행도는 체크한 비율로 서버가 정한다 (클라이언트가 보낸 값은 무시)
        if self.subtasks:
            done = sum(s.done for s in self.subtasks)
            self.progress = round(done / len(self.subtasks) * 100)
        elif self.completed:                     # 하위 작업이 없으면 완료 = 100% (화면에서 체크할 때와 같게)
            self.progress = 100
        # 태그는 대소문자만 다른 중복을 없애고 처음 입력한 순서를 지킨다
        unique: dict[str, str] = {}
        for tag in self.tags:
            unique.setdefault(tag.casefold(), tag)
        self.tags = list(unique.values())
        return self


class TodoItem(TodoIn):                          # 서버가 돌려주는 데이터 (id 있음)
    id: int
    completed_on: date | None = None             # 마지막으로 완료한 날 — 반복 초기화에 쓴다 (클라이언트가 보내도 무시)


class DayCount(BaseModel):
    date: date
    count: int


class Stats(BaseModel):
    today: date
    streak: int                                  # 오늘(또는 어제)까지 하루도 빠짐없이 완료한 날 수
    recent: list[DayCount]                       # 최근 STATS_DAYS 일의 날짜별 완료 개수 (오래된 날 → 오늘)
    recent_total: int
    total: int                                   # 지금까지 완료한 횟수


class SignupIn(BaseModel):
    username: Username                           # 영문·숫자·_ 3~20자
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    # 로그인은 형식 검사를 느슨하게 — 틀린 형식도 422 가 아니라 똑같이 401 로 답해 단서를 주지 않는다
    username: str = Field(max_length=100)
    password: str = Field(max_length=128)


class User(BaseModel):
    id: int
    username: str


# ---------- SQLite ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,   -- 지운 id 를 다시 쓰지 않는다
    title        TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    completed    INTEGER NOT NULL DEFAULT 0,
    due_date     TEXT,
    progress     INTEGER NOT NULL DEFAULT 0,
    subtasks     TEXT    NOT NULL DEFAULT '[]',       -- JSON 배열
    tags         TEXT    NOT NULL DEFAULT '[]',       -- JSON 배열
    repeat       TEXT    NOT NULL DEFAULT 'none',
    completed_on TEXT
);
-- 완료 기록: 할 일을 지우거나 반복으로 초기화해도 통계는 남는다
CREATE TABLE IF NOT EXISTS completions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id      INTEGER NOT NULL,
    title        TEXT    NOT NULL,
    completed_on TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,   -- 대소문자만 다른 아이디는 같은 아이디
    password_hash TEXT    NOT NULL,                         -- scrypt 해시 (비밀번호 원문은 저장하지 않는다)
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT,
    created_at    TEXT    NOT NULL
);
-- 로그인 세션: 쿠키에 담긴 토큰의 SHA-256 만 저장한다 (DB 가 새도 바로 로그인할 수는 없다)
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at TEXT    NOT NULL
);
"""

COLUMNS = ("title", "description", "completed", "due_date", "progress",
           "subtasks", "tags", "repeat", "completed_on")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # v5 DB 에는 user_id 열이 없다 — 있으면 그대로, 없으면 추가한다 (기존 행은 주인 없음 = NULL)
    for table in ("todos", "completions"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "user_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS todos_by_user ON todos (user_id);
        CREATE INDEX IF NOT EXISTS completions_by_user_day ON completions (user_id, completed_on);
    """)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    # BEGIN IMMEDIATE: 읽기 → 판단 → 쓰기 전체를 한 번에 하나의 요청만 하게 한다.
    # 파일 하나를 여러 프로세스가 같이 써도 SQLite 가 잠가 주므로 --workers 를 늘려도 안전하다.
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_FILE, timeout=10, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:                        # 404 등 예외가 나면 하던 변경을 모두 되돌린다
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def to_row(todo: TodoItem) -> tuple:
    return (
        todo.title, todo.description, int(todo.completed),
        todo.due_date.isoformat() if todo.due_date else None,
        todo.progress,
        json.dumps([s.model_dump() for s in todo.subtasks], ensure_ascii=False),
        json.dumps(todo.tags, ensure_ascii=False),
        todo.repeat,
        todo.completed_on.isoformat() if todo.completed_on else None,
    )


def from_row(row: sqlite3.Row) -> TodoItem:
    data = dict(row)
    data.pop("user_id", None)                    # 소유자는 응답에 넣지 않는다
    data["completed"] = bool(data["completed"])
    data["subtasks"] = json.loads(data["subtasks"])
    data["tags"] = json.loads(data["tags"])
    return TodoItem(**data)


def insert(conn: sqlite3.Connection, todo: TodoItem, user_id: int | None, keep_id: bool = False) -> int:
    if keep_id:                                  # 옮겨 담기·테스트용: 원래 id 를 그대로 쓴다
        conn.execute(f"INSERT INTO todos (id, user_id, {', '.join(COLUMNS)}) VALUES (?, ?{', ?' * len(COLUMNS)})",
                     (todo.id, user_id, *to_row(todo)))
        return todo.id
    cursor = conn.execute(f"INSERT INTO todos (user_id, {', '.join(COLUMNS)}) VALUES (?{', ?' * len(COLUMNS)})",
                          (user_id, *to_row(todo)))
    return cursor.lastrowid


def fetch(conn: sqlite3.Connection, todo_id: int, user_id: int) -> TodoItem:
    # 다른 사람의 할 일도 "없는 할 일"과 똑같이 404 — 그 id 가 있는지조차 알려 주지 않는다
    row = conn.execute("SELECT * FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "To-Do item not found")
    return from_row(row)


def load_todos(user_id: int | None = None) -> list[TodoItem]:
    with transaction() as conn:
        if user_id is None:
            rows = conn.execute("SELECT * FROM todos ORDER BY id")
        else:
            rows = conn.execute("SELECT * FROM todos WHERE user_id = ? ORDER BY id", (user_id,))
        return [from_row(r) for r in rows]


def save_todos(todos: list[TodoItem], user_id: int | None = None) -> None:
    # 전체를 통째로 바꾼다 — 데이터 옮겨 담기와 테스트 준비에서만 쓴다
    with transaction() as conn:
        conn.execute("DELETE FROM todos")
        for todo in todos:
            insert(conn, todo, user_id, keep_id=True)


def migrate_legacy_json() -> int:
    # DB 가 비어 있고 v4 의 todo.json 이 있으면 옮겨 담고, 다시 옮기지 않도록 이름을 바꿔 둔다
    # 옮겨 온 할 일은 주인 없음(NULL) — LEGACY_OWNER 계정이 가입·로그인할 때 가져간다
    source = next((f for f in LEGACY_FILES if f.exists()), None)
    if source is None:
        return 0
    with transaction() as conn:
        if conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]:
            return 0
        todos = [TodoItem(**t) for t in json.loads(source.read_text(encoding=ENCODING))]
        for todo in todos:
            insert(conn, todo, None, keep_id=True)
    if not todos:                                # 빈 초기 데이터(레포의 todo.json)는 그대로 둔다 — git 에서 지워진 것처럼 보이지 않게
        return 0
    try:
        source.rename(source.with_name(source.name + ".migrated"))
    except OSError:                              # 읽기 전용 위치여도 DB 가 차 있으니 다음엔 건너뛴다
        pass
    return len(todos)


# ---------- 계정 · 로그인 ----------

def hash_password(password: str) -> str:
    # scrypt: 파이썬 기본 모듈로 쓸 수 있는 느린 해시 — 비밀번호마다 다른 salt 를 붙인다
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=8, p=1, maxmem=2 ** 26, dklen=32)
    return f"scrypt${SCRYPT_N}$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    _, n, r, p, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                               maxmem=2 ** 26, dklen=len(digest) // 2)
    return hmac.compare_digest(candidate.hex(), digest)   # 비교 시간으로 정답을 추측하지 못하게


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(conn: sqlite3.Connection, user_id: int, response: Response) -> None:
    now = utcnow()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))   # 만료된 세션 정리
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                 (token_hash(token), user_id, (now + timedelta(days=SESSION_DAYS)).isoformat()))
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, path="/",
        httponly=True,                           # 스크립트가 쿠키를 읽지 못한다 (XSS 로 세션을 훔치기 어렵게)
        samesite="strict",                       # 다른 사이트에서 보낸 요청에는 쿠키를 붙이지 않는다 (CSRF 방어)
        secure=COOKIE_SECURE,
    )


def claim_legacy(conn: sqlite3.Connection, user: User) -> None:
    # 주인 없는 v5 데이터를 LEGACY_OWNER 로 지정한 계정에 넘긴다
    if LEGACY_OWNER and user.username.casefold() == LEGACY_OWNER.casefold():
        conn.execute("UPDATE todos SET user_id = ? WHERE user_id IS NULL", (user.id,))
        conn.execute("UPDATE completions SET user_id = ? WHERE user_id IS NULL", (user.id,))


def current_user(token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None) -> User:
    if not token:
        raise HTTPException(401, LOGIN_REQUIRED)
    with transaction() as conn:
        row = conn.execute(
            """SELECT users.id, users.username FROM sessions JOIN users ON users.id = sessions.user_id
               WHERE sessions.token_hash = ? AND sessions.expires_at > ?""",
            (token_hash(token), utcnow().isoformat())).fetchone()
    if row is None:
        raise HTTPException(401, LOGIN_REQUIRED)
    return User(id=row["id"], username=row["username"])


CurrentUser = Annotated[User, Depends(current_user)]


# ---------- 반복 할 일 ----------

def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())   # 월요일


def period_passed(todo: TodoItem, now: date) -> bool:
    if not (todo.completed and todo.completed_on):
        return False
    if todo.repeat == "daily":
        return todo.completed_on < now
    if todo.repeat == "weekly":
        return week_start(todo.completed_on) < week_start(now)
    return False


def next_due(due: date | None, repeat: Repeat, now: date) -> date | None:
    # 지난 마감일을 반복 주기만큼 밀어서 오늘 이후의 가장 가까운 날로 맞춘다
    if due is None or due >= now:
        return due
    step = 1 if repeat == "daily" else 7
    return due + timedelta(days=step * math.ceil((now - due).days / step))


def refresh_repeats(conn: sqlite3.Connection, user_id: int) -> None:
    # 완료한 반복 할 일은 주기가 지나면 다시 미완료로 돌아온다 (완료 기록은 completions 에 남아 있다)
    now = today()
    rows = conn.execute("SELECT * FROM todos WHERE user_id = ? AND repeat != 'none' AND completed = 1", (user_id,))
    for todo in map(from_row, rows.fetchall()):
        if period_passed(todo, now):
            reset = todo.model_copy(update={
                "completed": False, "completed_on": None, "progress": 0,
                "subtasks": [s.model_copy(update={"done": False}) for s in todo.subtasks],
                "due_date": next_due(todo.due_date, todo.repeat, now),
            })
            write(conn, reset)


def write(conn: sqlite3.Connection, todo: TodoItem) -> None:
    conn.execute(f"UPDATE todos SET {', '.join(c + ' = ?' for c in COLUMNS)} WHERE id = ?",
                 (*to_row(todo), todo.id))


def record_completion_change(conn: sqlite3.Connection, user_id: int,
                             old: TodoItem | None, new: TodoItem) -> TodoItem:
    # 미완료 → 완료: 오늘 완료로 기록한다. 완료 → 미완료: 실수로 누른 것으로 보고 그 기록을 지운다.
    was_done = bool(old and old.completed)
    if new.completed and not was_done:
        conn.execute("INSERT INTO completions (user_id, todo_id, title, completed_on) VALUES (?, ?, ?, ?)",
                     (user_id, new.id, new.title, today().isoformat()))
        return new.model_copy(update={"completed_on": today()})
    if was_done and not new.completed:
        conn.execute("""DELETE FROM completions WHERE id = (
                            SELECT id FROM completions WHERE todo_id = ? AND completed_on = ?
                            ORDER BY id DESC LIMIT 1)""",
                     (new.id, old.completed_on.isoformat() if old.completed_on else None))
        return new.model_copy(update={"completed_on": None})
    return new.model_copy(update={"completed_on": old.completed_on if old else None})


# ---------- 검색 ----------

def matches(todo: TodoItem, q: str, tag: str) -> bool:
    if tag and tag.casefold() not in (t.casefold() for t in todo.tags):
        return False
    if q:
        haystack = " ".join([todo.title, todo.description, *todo.tags, *(s.title for s in todo.subtasks)])
        return q.casefold() in haystack.casefold()
    return True


# ---------- API ----------

NOT_FOUND = {404: {"description": "To-Do item not found"}}   # API 문서(/docs)에 404 응답을 표시
UNAUTHORIZED = {401: {"description": LOGIN_REQUIRED}}
AUTH_REQUIRED = {**UNAUTHORIZED, **NOT_FOUND}

# 브라우저가 지키는 보안 헤더 — 클릭재킹(다른 사이트가 프레임으로 감싸기), MIME 추측, 외부 스크립트를 막는다
# 화면이 인라인 <script>·<style> 을 쓰므로 그 둘만 'unsafe-inline' 을 허용한다
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    migrate_legacy_json()                        # 서버가 뜰 때 한 번 — 옮길 것이 없으면 아무 일도 안 한다
    yield


app = FastAPI(
    title="To-Do List API", version=APP_VERSION, lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)


# Prometheus 메트릭(/metrics) — 경로별 요청 수·응답 시간을 자동으로 기록한다
# 로그인 없이 열려 있어야 Prometheus 가 가져갈 수 있다 (실습용. 운영에서는 프록시로 외부 접근을 막는다)
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


@app.post("/auth/signup", status_code=201, responses={409: {"description": "이미 있는 아이디"}})
def signup(payload: SignupIn, response: Response) -> User:
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (payload.username,)).fetchone():
            raise HTTPException(409, "이미 사용 중인 아이디입니다.")
        cursor = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                              (payload.username, hash_password(payload.password), utcnow().isoformat()))
        user = User(id=cursor.lastrowid, username=payload.username)
        claim_legacy(conn, user)
        start_session(conn, user.id, response)
    return user


@app.post("/auth/login", responses={
    401: {"description": "아이디 또는 비밀번호가 올바르지 않음"},
    429: {"description": "로그인 시도 제한"},
})
def login(payload: LoginIn, response: Response) -> User:
    # 실패 횟수는 저장해야 하므로 트랜잭션을 끝낸(COMMIT) 뒤에 오류를 돌려준다
    now = utcnow()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (payload.username.strip(),)).fetchone()
        if row is None:
            hash_password(payload.password)      # 없는 아이디도 같은 시간을 써서 아이디가 있는지 응답 시간으로 알 수 없게
            outcome = "fail"
        elif row["locked_until"] and row["locked_until"] > now.isoformat():
            outcome = "locked"
        elif not verify_password(payload.password, row["password_hash"]):
            failed = row["failed_logins"] + 1
            locked_until = (now + timedelta(minutes=LOCK_MINUTES)).isoformat() if failed >= MAX_FAILED_LOGINS else None
            conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                         (0 if locked_until else failed, locked_until, row["id"]))
            outcome = "fail"
        else:
            conn.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (row["id"],))
            user = User(id=row["id"], username=row["username"])
            claim_legacy(conn, user)
            start_session(conn, user.id, response)
            outcome = "ok"
    if outcome == "locked":
        raise HTTPException(429, f"로그인 시도가 너무 많습니다. {LOCK_MINUTES}분 뒤 다시 시도해 주세요.")
    if outcome == "fail":
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
    return user


@app.post("/auth/logout", status_code=204)
def logout(response: Response, token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None) -> None:
    if token:
        with transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")


@app.get("/auth/me", responses=UNAUTHORIZED)
def me(user: CurrentUser) -> User:
    return user


@app.delete("/auth/me", status_code=204, responses=UNAUTHORIZED)  # 계정 삭제 — 내 할 일·기록·세션도 모두 지운다
def delete_account(user: CurrentUser, response: Response) -> None:
    with transaction() as conn:
        for table in ("todos", "completions", "sessions"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user.id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user.id,))
    response.delete_cookie(SESSION_COOKIE, path="/")


@app.get("/todos", responses=UNAUTHORIZED)       # 목록 조회 — ?q=검색어 &tag=태그 로 거를 수 있다
def get_todos(user: CurrentUser, q: str = "", tag: str = "") -> list[TodoItem]:
    with transaction() as conn:
        refresh_repeats(conn, user.id)
        rows = conn.execute("SELECT * FROM todos WHERE user_id = ? ORDER BY id", (user.id,))
        todos = [from_row(r) for r in rows]
    return [t for t in todos if matches(t, q.strip(), tag.strip())]


@app.post("/todos", status_code=201, responses=UNAUTHORIZED)  # 추가 — id 는 서버가 매긴다
def create_todo(user: CurrentUser, payload: TodoIn) -> TodoItem:
    with transaction() as conn:
        todo = TodoItem(id=0, **payload.model_dump())
        todo = todo.model_copy(update={"id": insert(conn, todo, user.id)})
        todo = record_completion_change(conn, user.id, None, todo)   # 처음부터 완료로 만들면 오늘 완료로 센다
        write(conn, todo)
    return todo


@app.put("/todos/{todo_id}", responses=AUTH_REQUIRED)  # 수정
def update_todo(user: CurrentUser, todo_id: int, payload: TodoIn) -> TodoItem:
    with transaction() as conn:
        refresh_repeats(conn, user.id)
        old = fetch(conn, todo_id, user.id)      # 없거나 남의 id 면 여기서 404 (트랜잭션은 ROLLBACK)
        todo = record_completion_change(conn, user.id, old, TodoItem(id=todo_id, **payload.model_dump()))
        write(conn, todo)
    return todo


@app.delete("/todos/{todo_id}", status_code=204, responses=AUTH_REQUIRED)  # 삭제 — 완료 기록(통계)은 남긴다
def delete_todo(user: CurrentUser, todo_id: int) -> None:
    with transaction() as conn:
        fetch(conn, todo_id, user.id)
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))


@app.get("/stats", responses=UNAUTHORIZED)       # 학습 통계 — 연속 완료일과 최근 7일 완료 개수
def get_stats(user: CurrentUser) -> Stats:
    now = today()
    with transaction() as conn:
        rows = conn.execute("""SELECT completed_on, COUNT(*) FROM completions
                               WHERE user_id = ? GROUP BY completed_on""", (user.id,)).fetchall()
    per_day = {date.fromisoformat(day): count for day, count in rows}

    # 오늘 아직 못 했어도 어제까지 이어졌으면 연속 기록은 살아 있다
    day = now if now in per_day else now - timedelta(days=1)
    streak = 0
    while day in per_day:
        streak += 1
        day -= timedelta(days=1)

    recent = [DayCount(date=now - timedelta(days=i), count=per_day.get(now - timedelta(days=i), 0))
              for i in reversed(range(STATS_DAYS))]
    return Stats(today=now, streak=streak, recent=recent,
                 recent_total=sum(d.count for d in recent), total=sum(per_day.values()))


@app.get("/version")                             # 화면 배지가 읽어가는 앱 버전 (로그인 없이 볼 수 있다)
def get_version() -> dict[str, str]:
    return {"version": APP_VERSION}


@app.get("/", include_in_schema=False)           # 화면 서빙 — 로그인 화면도 같은 페이지가 보여 준다
def read_root() -> FileResponse:
    return FileResponse(INDEX_FILE, media_type="text/html")
