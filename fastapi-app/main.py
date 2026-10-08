import json
import math
import os
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

APP_VERSION = "5.0.0"                            # 화면 배지와 /version 이 함께 쓰는 단일 출처
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

MAX_SUBTASKS = 20
MAX_TAGS = 5
STATS_DAYS = 7                                   # 통계 막대에 보여 줄 최근 일수


def today() -> date:
    return datetime.now(APP_TZ).date()


# ---------- 데이터 모델 ----------

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
Repeat = Literal["none", "daily", "weekly"]


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
CREATE INDEX IF NOT EXISTS completions_by_day ON completions (completed_on);
"""

COLUMNS = ("title", "description", "completed", "due_date", "progress",
           "subtasks", "tags", "repeat", "completed_on")


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    # BEGIN IMMEDIATE: 읽기 → 판단 → 쓰기 전체를 한 번에 하나의 요청만 하게 한다.
    # 파일 하나를 여러 프로세스가 같이 써도 SQLite 가 잠가 주므로 --workers 를 늘려도 안전하다.
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_FILE, timeout=10, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
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
    data["completed"] = bool(data["completed"])
    data["subtasks"] = json.loads(data["subtasks"])
    data["tags"] = json.loads(data["tags"])
    return TodoItem(**data)


def insert(conn: sqlite3.Connection, todo: TodoItem, keep_id: bool = False) -> int:
    if keep_id:                                  # 옮겨 담기·테스트용: 원래 id 를 그대로 쓴다
        conn.execute(f"INSERT INTO todos (id, {', '.join(COLUMNS)}) VALUES (?{', ?' * len(COLUMNS)})",
                     (todo.id, *to_row(todo)))
        return todo.id
    cursor = conn.execute(f"INSERT INTO todos ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                          to_row(todo))
    return cursor.lastrowid


def fetch(conn: sqlite3.Connection, todo_id: int) -> TodoItem:
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "To-Do item not found")
    return from_row(row)


def load_todos() -> list[TodoItem]:
    with transaction() as conn:
        return [from_row(r) for r in conn.execute("SELECT * FROM todos ORDER BY id")]


def save_todos(todos: list[TodoItem]) -> None:
    # 전체를 통째로 바꾼다 — 데이터 옮겨 담기와 테스트 준비에서만 쓴다
    with transaction() as conn:
        conn.execute("DELETE FROM todos")
        for todo in todos:
            insert(conn, todo, keep_id=True)


def migrate_legacy_json() -> int:
    # DB 가 비어 있고 v4 의 todo.json 이 있으면 옮겨 담고, 다시 옮기지 않도록 이름을 바꿔 둔다
    source = next((f for f in LEGACY_FILES if f.exists()), None)
    if source is None:
        return 0
    with transaction() as conn:
        if conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]:
            return 0
        todos = [TodoItem(**t) for t in json.loads(source.read_text(encoding=ENCODING))]
        for todo in todos:
            insert(conn, todo, keep_id=True)
    if not todos:                                # 빈 초기 데이터(레포의 todo.json)는 그대로 둔다 — git 에서 지워진 것처럼 보이지 않게
        return 0
    try:
        source.rename(source.with_name(source.name + ".migrated"))
    except OSError:                              # 읽기 전용 위치여도 DB 가 차 있으니 다음엔 건너뛴다
        pass
    return len(todos)


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


def refresh_repeats(conn: sqlite3.Connection) -> None:
    # 완료한 반복 할 일은 주기가 지나면 다시 미완료로 돌아온다 (완료 기록은 completions 에 남아 있다)
    now = today()
    rows = conn.execute("SELECT * FROM todos WHERE repeat != 'none' AND completed = 1")
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


def record_completion_change(conn: sqlite3.Connection, old: TodoItem | None, new: TodoItem) -> TodoItem:
    # 미완료 → 완료: 오늘 완료로 기록한다. 완료 → 미완료: 실수로 누른 것으로 보고 그 기록을 지운다.
    was_done = bool(old and old.completed)
    if new.completed and not was_done:
        conn.execute("INSERT INTO completions (todo_id, title, completed_on) VALUES (?, ?, ?)",
                     (new.id, new.title, today().isoformat()))
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

@asynccontextmanager
async def lifespan(_: FastAPI):
    migrate_legacy_json()                        # 서버가 뜰 때 한 번 — 옮길 것이 없으면 아무 일도 안 한다
    yield


app = FastAPI(title="To-Do List API", version=APP_VERSION, lifespan=lifespan)


@app.get("/todos")                               # 목록 조회 — ?q=검색어 &tag=태그 로 거를 수 있다
def get_todos(q: str = "", tag: str = "") -> list[TodoItem]:
    with transaction() as conn:
        refresh_repeats(conn)
        todos = [from_row(r) for r in conn.execute("SELECT * FROM todos ORDER BY id")]
    return [t for t in todos if matches(t, q.strip(), tag.strip())]


@app.post("/todos", status_code=201)             # 추가 — id 는 서버가 매긴다
def create_todo(payload: TodoIn) -> TodoItem:
    with transaction() as conn:
        todo = TodoItem(id=0, **payload.model_dump())
        todo = todo.model_copy(update={"id": insert(conn, todo)})
        todo = record_completion_change(conn, None, todo)   # 처음부터 완료로 만들면 오늘 완료로 센다
        write(conn, todo)
    return todo


@app.put("/todos/{todo_id}", responses=NOT_FOUND)  # 수정
def update_todo(todo_id: int, payload: TodoIn) -> TodoItem:
    with transaction() as conn:
        refresh_repeats(conn)
        old = fetch(conn, todo_id)               # 없는 id 면 여기서 404 (트랜잭션은 ROLLBACK)
        todo = record_completion_change(conn, old, TodoItem(id=todo_id, **payload.model_dump()))
        write(conn, todo)
    return todo


@app.delete("/todos/{todo_id}", status_code=204, responses=NOT_FOUND)  # 삭제 — 완료 기록(통계)은 남긴다
def delete_todo(todo_id: int) -> None:
    with transaction() as conn:
        fetch(conn, todo_id)
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))


@app.get("/stats")                               # 학습 통계 — 연속 완료일과 최근 7일 완료 개수
def get_stats() -> Stats:
    now = today()
    with transaction() as conn:
        rows = conn.execute("SELECT completed_on, COUNT(*) FROM completions GROUP BY completed_on").fetchall()
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


@app.get("/version")                             # 화면 배지가 읽어가는 앱 버전
def get_version() -> dict[str, str]:
    return {"version": APP_VERSION}


@app.get("/", include_in_schema=False)           # 화면 서빙
def read_root() -> FileResponse:
    return FileResponse(INDEX_FILE, media_type="text/html")
