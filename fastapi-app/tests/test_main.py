import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# main.py 가 있는 fastapi-app 폴더를 import 경로에 추가 — 어느 폴더에서 pytest 를 실행해도 main 을 찾는다
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from main import app, save_todos, load_todos, TodoItem  # noqa: E402

client = TestClient(app)                         # 매 테스트 tester 로 로그인된 클라이언트

TODAY = date(2026, 10, 7)                        # 수요일 — 반복·통계 테스트가 날짜에 흔들리지 않게 고정
TESTER = {"username": "tester", "password": "password123"}
TESTER_ID = 1                                    # 매 테스트 새 DB 에서 처음 가입하므로 항상 1


class Clock:
    # main.today() 를 바꿔 끼워 "다음 날"을 흉내 낸다
    def __init__(self):
        self.now = TODAY

    def advance(self, days):
        self.now += timedelta(days=days)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    # 실제 DB 대신 테스트마다 새 임시 DB 사용 (본인 데이터 보호 + 테스트 간 격리)
    # 테스트가 끝나면 tmp_path 와 monkeypatch 가 자동으로 원상 복구한다
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "todos.db")
    monkeypatch.setattr(main, "LEGACY_FILES", [tmp_path / "todo.json"])
    monkeypatch.setattr(main, "SCRYPT_N", 2 ** 8)   # 비밀번호 해시를 가볍게 — 테스트 속도용 (운영은 2**15)
    monkeypatch.setattr(main, "LEGACY_OWNER", "")
    # v6 부터 모든 할 일 API 는 로그인이 필요하다 — tester 로 가입해 쿠키를 받아 둔다
    client.cookies.clear()
    response = client.post("/auth/signup", json=TESTER)
    assert response.status_code == 201
    assert response.json()["id"] == TESTER_ID


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(main, "today", lambda: c.now)
    return c


def create(**fields):
    response = client.post("/todos", json={"title": "코딩테스트 LV2", **fields})
    assert response.status_code == 201
    return response.json()


def update(todo, **fields):
    response = client.put(f"/todos/{todo['id']}", json={**todo, **fields})
    assert response.status_code == 200
    return response.json()


# ---------- 기본 CRUD (v1~) ----------

def test_get_todos_empty():
    response = client.get("/todos")
    assert response.status_code == 200
    assert response.json() == []


def test_get_todos_with_items():
    todo = TodoItem(id=1, title="Test", description="Test description", completed=False)
    save_todos([todo], user_id=TESTER_ID)  # save_todos 는 TodoItem 객체 리스트와 주인 id 를 받음
    response = client.get("/todos")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["title"] == "Test"


def test_create_todo():
    todo = {"title": "Test", "description": "Test description", "completed": False}  # id 는 보내지 않음
    response = client.post("/todos", json=todo)
    assert response.status_code == 201           # 생성 성공 = 201 Created
    assert response.json()["title"] == "Test"
    assert response.json()["id"] == 1            # id 는 서버가 부여
    assert len(load_todos()) == 1                # DB 에도 저장됐는지 확인


def test_ids_are_not_reused_after_delete():
    first = create()
    client.delete(f"/todos/{first['id']}")
    assert create()["id"] == first["id"] + 1     # 지운 id 를 다시 쓰면 옛 링크·기록이 엉뚱한 항목을 가리킨다


def test_create_todo_invalid():
    todo = {"description": "Test description"}   # 필수 필드 title 누락
    response = client.post("/todos", json=todo)
    assert response.status_code == 422


@pytest.mark.parametrize("title", ["", "   "])
def test_blank_title_rejected(title):
    # 공백만 있는 제목도 빈 제목으로 본다 (화면을 거치지 않고 API 로 보내도 막힌다)
    response = client.post("/todos", json={"title": title})
    assert response.status_code == 422


def test_title_is_trimmed():
    assert create(title="  코딩테스트 LV2  ")["title"] == "코딩테스트 LV2"


def test_update_todo():
    todo = TodoItem(id=1, title="Test", description="Test description", completed=False)
    save_todos([todo], user_id=TESTER_ID)
    updated_todo = {"title": "Updated", "description": "Updated description", "completed": True}
    response = client.put("/todos/1", json=updated_todo)
    assert response.status_code == 200
    assert response.json()["title"] == "Updated"
    assert load_todos()[0].completed is True


def test_update_todo_not_found():
    updated_todo = {"title": "Updated", "description": "Updated description", "completed": True}
    response = client.put("/todos/1", json=updated_todo)
    assert response.status_code == 404


def test_delete_todo():
    todo = TodoItem(id=1, title="Test", description="Test description", completed=False)
    save_todos([todo], user_id=TESTER_ID)
    response = client.delete("/todos/1")
    assert response.status_code == 204           # 삭제 성공 = 204 No Content (응답 본문 없음)
    assert load_todos() == []


def test_delete_todo_not_found():
    response = client.delete("/todos/1")
    assert response.status_code == 404


def test_version():
    assert client.get("/version").json() == {"version": "6.0.0"}


# ---------- 진행도 · 하위 작업 (v4~) ----------

def test_create_todo_progress_default():
    assert create()["progress"] == 0             # 진행도를 안 보내면 0%


@pytest.mark.parametrize("progress", [-1, 101])
def test_create_todo_progress_out_of_range(progress):
    response = client.post("/todos", json={"title": "코딩테스트 LV2", "progress": progress})
    assert response.status_code == 422          # 0~100 범위 밖은 거부


def test_update_todo_progress():
    save_todos([TodoItem(id=1, title="코딩테스트 LV2")], user_id=TESTER_ID)
    response = client.put("/todos/1", json={"title": "코딩테스트 LV2", "progress": 60})
    assert response.status_code == 200
    assert load_todos()[0].progress == 60        # DB 에도 반영됐는지 확인


def test_completed_without_subtasks_is_100_percent():
    assert create(completed=True, progress=30)["progress"] == 100   # 하위 작업이 없으면 완료 = 100%


def test_create_todo_subtasks_default_empty():
    assert create()["subtasks"] == []            # 하위 작업을 안 보내면 빈 목록


def test_subtasks_derive_progress():
    subtasks = [
        {"title": "문제 1 풀기", "done": True},
        {"title": "문제 2 풀기", "done": True},
        {"title": "문제 3 풀기", "done": False},
    ]
    # 클라이언트가 보낸 progress(10)는 무시되고 체크 비율(2/3 = 67%)로 정해진다
    assert create(progress=10, subtasks=subtasks)["progress"] == 67
    saved = load_todos()[0]
    assert [s.title for s in saved.subtasks] == ["문제 1 풀기", "문제 2 풀기", "문제 3 풀기"]
    assert saved.progress == 67


def test_update_subtask_recomputes_progress():
    todo = create(subtasks=[{"title": "문제 1"}, {"title": "문제 2"}])
    assert todo["progress"] == 0
    todo = update(todo, subtasks=[{"title": "문제 1", "done": True}, {"title": "문제 2", "done": True}])
    assert todo["progress"] == 100


@pytest.mark.parametrize("title", ["", "   "])
def test_subtask_blank_title_rejected(title):
    response = client.post("/todos", json={"title": "코딩테스트 LV2", "subtasks": [{"title": title}]})
    assert response.status_code == 422


def test_too_many_subtasks_rejected():
    subtasks = [{"title": f"문제 {i}"} for i in range(main.MAX_SUBTASKS + 1)]
    response = client.post("/todos", json={"title": "코딩테스트 LV2", "subtasks": subtasks})
    assert response.status_code == 422


# ---------- v4 데이터 옮겨 담기 (v5~) ----------

def write_legacy(rows):
    main.LEGACY_FILES[0].write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def claim_as_tester():
    # tester 를 LEGACY_OWNER 로 지정하고 다시 로그인하면 주인 없는 할 일을 가져간다
    main.LEGACY_OWNER = "tester"                 # isolated_db 의 monkeypatch 가 테스트 뒤 "" 로 되돌린다
    assert client.post("/auth/login", json=TESTER).status_code == 200


def test_migrate_legacy_json():
    # v3 의 priority 가 남아 있어도 읽히고, 원래 id 가 그대로 유지된다
    write_legacy([
        {"id": 3, "title": "옛 할 일", "priority": "high"},
        {"id": 7, "title": "코딩테스트", "progress": 40, "subtasks": [{"title": "문제 1", "done": True}]},
    ])
    assert main.migrate_legacy_json() == 2
    claim_as_tester()
    todos = client.get("/todos").json()
    assert [t["id"] for t in todos] == [3, 7]
    assert todos[0]["progress"] == 0
    assert todos[1]["progress"] == 100           # 하위 작업 기준으로 다시 계산
    assert create()["id"] == 8                   # 새 id 는 옮겨 온 id 다음부터
    # 다시 옮기지 않도록 이름을 바꿔 둔다
    assert not main.LEGACY_FILES[0].exists()
    assert main.LEGACY_FILES[0].with_name("todo.json.migrated").exists()


def test_migrate_skipped_when_db_has_data():
    create(title="이미 있는 할 일")
    write_legacy([{"id": 1, "title": "옛 할 일"}])
    assert main.migrate_legacy_json() == 0       # DB 에 데이터가 있으면 덮어쓰지 않는다
    assert [t["title"] for t in client.get("/todos").json()] == ["이미 있는 할 일"]


def test_migrate_empty_seed_file():
    # 레포에 들어 있는 fastapi-app/todo.json 은 빈 목록 — 서버가 처음 떠도 오류 없이 빈 상태로 시작한다
    write_legacy([])
    assert main.migrate_legacy_json() == 0
    assert client.get("/todos").json() == []
    assert main.LEGACY_FILES[0].exists()         # 빈 파일은 이름을 바꾸지 않는다 (git 에서 삭제로 보이지 않게)


def test_migrate_without_legacy_file():
    assert main.migrate_legacy_json() == 0


def test_startup_runs_migration():
    write_legacy([{"id": 1, "title": "옛 할 일"}])
    main.LEGACY_OWNER = "tester"
    with TestClient(app) as started:             # with 로 열어야 서버 시작(lifespan)이 실행된다
        assert started.post("/auth/login", json=TESTER).status_code == 200
        assert [t["title"] for t in started.get("/todos").json()] == ["옛 할 일"]


# ---------- 태그 · 검색 (v5~) ----------

def test_tags_default_empty():
    assert create()["tags"] == []


def test_tags_trimmed_and_deduplicated():
    todo = create(tags=[" LV2 ", "프로그래머스", "lv2", "LV2"])
    assert todo["tags"] == ["LV2", "프로그래머스"]  # 대소문자만 다른 중복은 처음 것만 남긴다


@pytest.mark.parametrize("tags", [
    ["a", "b", "c", "d", "e", "f"],              # 6개 — 최대 5개
    ["   "],                                    # 공백 태그
    ["x" * 21],                                 # 21자 — 최대 20자
])
def test_invalid_tags_rejected(tags):
    response = client.post("/todos", json={"title": "코딩테스트 LV2", "tags": tags})
    assert response.status_code == 422


def test_filter_by_tag():
    create(title="A", tags=["LV1"])
    create(title="B", tags=["LV2", "프로그래머스"])
    create(title="C", tags=["lv2"])
    titles = [t["title"] for t in client.get("/todos", params={"tag": "LV2"}).json()]
    assert titles == ["B", "C"]                  # 태그는 대소문자를 구분하지 않는다


@pytest.mark.parametrize("q, expected", [
    ("코딩", ["코딩테스트 LV2"]),                 # 제목
    ("백준", ["알고리즘"]),                        # 설명
    ("dfs", ["알고리즘"]),                         # 하위 작업 (대소문자 무시)
    ("프로그래머스", ["코딩테스트 LV2"]),          # 태그
    ("", ["코딩테스트 LV2", "알고리즘"]),          # 빈 검색어 = 전체
    ("없는말", []),
])
def test_search(q, expected):
    create(title="코딩테스트 LV2", tags=["프로그래머스"])
    create(title="알고리즘", description="백준 골드", subtasks=[{"title": "DFS 복습"}])
    assert [t["title"] for t in client.get("/todos", params={"q": q}).json()] == expected


def test_search_and_tag_together():
    create(title="LV2 1번", tags=["LV2"])
    create(title="LV2 2번", tags=["LV2"])
    create(title="LV1 2번", tags=["LV1"])
    titles = [t["title"] for t in client.get("/todos", params={"q": "2번", "tag": "LV2"}).json()]
    assert titles == ["LV2 2번"]


# ---------- 반복 할 일 (v5~) ----------

def test_repeat_default_none():
    assert create()["repeat"] == "none"


def test_invalid_repeat_rejected():
    response = client.post("/todos", json={"title": "코딩테스트 LV2", "repeat": "monthly"})
    assert response.status_code == 422


def test_daily_repeat_resets_next_day(clock):
    todo = create(repeat="daily", subtasks=[{"title": "문제 1"}, {"title": "문제 2"}])
    todo = update(todo, completed=True, subtasks=[{"title": "문제 1", "done": True}, {"title": "문제 2", "done": True}])
    assert todo["completed_on"] == TODAY.isoformat()

    assert client.get("/todos").json()[0]["completed"] is True     # 같은 날에는 완료 상태 그대로

    clock.advance(1)
    again = client.get("/todos").json()[0]
    assert again["completed"] is False                              # 다음 날 다시 미완료로
    assert again["completed_on"] is None
    assert again["progress"] == 0
    assert [s["done"] for s in again["subtasks"]] == [False, False]


def test_weekly_repeat_resets_next_week(clock):
    todo = update(create(repeat="weekly"), completed=True)           # 수요일에 완료
    clock.advance(4)                                                # 같은 주 일요일
    assert client.get("/todos").json()[0]["completed"] is True
    clock.advance(1)                                                # 다음 주 월요일
    assert client.get("/todos").json()[0]["completed"] is False
    assert todo["repeat"] == "weekly"


def test_repeat_moves_past_due_date(clock):
    todo = update(create(repeat="weekly", due_date=TODAY.isoformat()), completed=True)
    clock.advance(9)                                                # 다음 주 금요일
    again = client.get("/todos").json()[0]
    assert again["due_date"] == (TODAY + timedelta(days=14)).isoformat()   # 오늘 이후의 가장 가까운 수요일
    assert todo["due_date"] == TODAY.isoformat()


def test_non_repeating_todo_stays_completed(clock):
    update(create(), completed=True)
    clock.advance(3)
    assert client.get("/todos").json()[0]["completed"] is True


def test_client_cannot_set_completed_on(clock):
    todo = create()
    response = client.put(f"/todos/{todo['id']}", json={**todo, "completed_on": "2000-01-01"})
    assert response.json()["completed_on"] is None                  # 서버가 정하는 값이라 무시


# ---------- 학습 통계 (v5~) ----------

def stats():
    response = client.get("/stats")
    assert response.status_code == 200
    return response.json()


def test_stats_empty(clock):
    s = stats()
    assert s["today"] == TODAY.isoformat()
    assert s["streak"] == 0
    assert s["total"] == 0
    assert len(s["recent"]) == main.STATS_DAYS
    assert s["recent"][-1] == {"date": TODAY.isoformat(), "count": 0}       # 마지막이 오늘
    assert s["recent"][0]["date"] == (TODAY - timedelta(days=6)).isoformat()


def test_completing_counts_in_stats(clock):
    update(create(), completed=True)
    create(completed=True)                       # 처음부터 완료로 만들어도 센다
    s = stats()
    assert s["recent"][-1]["count"] == 2
    assert s["recent_total"] == 2
    assert s["total"] == 2
    assert s["streak"] == 1


def test_uncheck_removes_completion(clock):
    todo = update(create(), completed=True)
    update(todo, completed=False)                # 실수로 누른 체크를 풀면 기록도 지운다
    assert stats()["total"] == 0


def test_editing_completed_todo_does_not_double_count(clock):
    todo = update(create(), completed=True)
    update(todo, description="메모 추가")        # 완료 상태에서 내용만 고쳐도 다시 세지 않는다
    assert stats()["total"] == 1


def test_delete_keeps_history(clock):
    todo = update(create(), completed=True)
    client.delete(f"/todos/{todo['id']}")
    assert stats()["total"] == 1                 # 할 일을 지워도 공부한 기록은 남는다


def test_streak_counts_consecutive_days(clock):
    todo = create(repeat="daily")
    for _ in range(3):                           # 3일 연속 완료
        todo = update(client.get("/todos").json()[0], completed=True)
        clock.advance(1)
    assert stats()["streak"] == 3                # 오늘은 아직 안 했지만 어제까지 이어졌다
    clock.advance(1)
    assert stats()["streak"] == 0                # 하루를 건너뛰면 끊긴다
    assert stats()["total"] == 3
    assert todo["repeat"] == "daily"


def test_recent_window_drops_old_days(clock):
    update(create(), completed=True)
    clock.advance(main.STATS_DAYS)               # 7일 뒤에는 최근 7일 막대에서 빠진다
    s = stats()
    assert s["recent_total"] == 0
    assert s["total"] == 1


# ---------- 계정 · 로그인 (v6~) ----------

class UtcClock:
    # main.utcnow() 를 바꿔 끼워 세션 만료·로그인 잠금 시간을 흉내 낸다
    def __init__(self):
        self.now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture
def utc(monkeypatch):
    c = UtcClock()
    monkeypatch.setattr(main, "utcnow", lambda: c.now)
    return c


def new_user(username, password="password123"):
    # 쿠키를 따로 갖는 새 브라우저처럼 동작하는 클라이언트
    other = TestClient(app)
    assert other.post("/auth/signup", json={"username": username, "password": password}).status_code == 201
    return other


@pytest.mark.parametrize("method, url", [
    ("get", "/todos"), ("post", "/todos"), ("put", "/todos/1"), ("delete", "/todos/1"),
    ("get", "/stats"), ("get", "/auth/me"), ("delete", "/auth/me"),
])
def test_requires_login(method, url):
    anonymous = TestClient(app)
    response = getattr(anonymous, method)(url, **({"json": {"title": "x"}} if method in ("post", "put") else {}))
    assert response.status_code == 401


def test_public_pages_without_login():
    anonymous = TestClient(app)
    assert anonymous.get("/").status_code == 200                # 로그인 화면도 같은 페이지
    assert anonymous.get("/version").status_code == 200


def test_signup_sets_safe_session_cookie():
    response = TestClient(app).post("/auth/signup", json={"username": "alice", "password": "password123"})
    assert response.status_code == 201
    assert response.json() == {"id": 2, "username": "alice"}    # 비밀번호는 응답에 없다
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie                                 # 스크립트가 읽지 못한다
    assert "samesite=strict" in cookie                          # 다른 사이트 요청에 붙지 않는다


def test_password_is_hashed():
    with sqlite3.connect(main.DB_FILE) as db:
        stored, = db.execute("SELECT password_hash FROM users WHERE username = 'tester'").fetchone()
    assert stored.startswith("scrypt$")
    assert TESTER["password"] not in stored


def test_duplicate_username_is_case_insensitive():
    response = TestClient(app).post("/auth/signup", json={"username": "TESTER", "password": "password123"})
    assert response.status_code == 409


@pytest.mark.parametrize("username, password", [
    ("ab", "password123"),                       # 아이디 3자 미만
    ("bad name!", "password123"),                # 영문·숫자·_ 외의 문자
    ("alice", "short"),                          # 비밀번호 8자 미만
])
def test_signup_validation(username, password):
    response = TestClient(app).post("/auth/signup", json={"username": username, "password": password})
    assert response.status_code == 422


def test_login_and_me():
    other = TestClient(app)
    assert other.post("/auth/login", json=TESTER).json() == {"id": TESTER_ID, "username": "tester"}
    assert other.get("/auth/me").json()["username"] == "tester"


@pytest.mark.parametrize("credentials", [
    {"username": "tester", "password": "wrong-password"},
    {"username": "nobody", "password": "password123"},
])
def test_login_failure_gives_same_message(credentials):
    # 틀린 비밀번호와 없는 아이디를 같은 응답으로 — 어떤 아이디가 있는지 알려 주지 않는다
    response = TestClient(app).post("/auth/login", json=credentials)
    assert response.status_code == 401
    assert response.json()["detail"] == "아이디 또는 비밀번호가 올바르지 않습니다."


def test_login_lockout_after_failures(utc):
    other = TestClient(app)
    wrong = {"username": "tester", "password": "wrong-password"}
    for _ in range(main.MAX_FAILED_LOGINS):
        assert other.post("/auth/login", json=wrong).status_code == 401
    assert other.post("/auth/login", json=TESTER).status_code == 429     # 맞는 비밀번호도 잠시 막힌다
    utc.advance(minutes=main.LOCK_MINUTES, seconds=1)
    assert other.post("/auth/login", json=TESTER).status_code == 200     # 시간이 지나면 풀린다


def test_successful_login_resets_failures(utc):
    other = TestClient(app)
    wrong = {"username": "tester", "password": "wrong-password"}
    for _ in range(main.MAX_FAILED_LOGINS - 1):
        other.post("/auth/login", json=wrong)
    assert other.post("/auth/login", json=TESTER).status_code == 200
    assert other.post("/auth/login", json=wrong).status_code == 401       # 다시 1번째 실패 — 잠기지 않는다


def test_logout_invalidates_session():
    token = client.cookies.get(main.SESSION_COOKIE)
    assert client.post("/auth/logout").status_code == 204
    assert client.get("/todos").status_code == 401
    stolen = TestClient(app, cookies={main.SESSION_COOKIE: token})       # 로그아웃한 토큰은 다시 쓸 수 없다
    assert stolen.get("/todos").status_code == 401


def test_session_expires(utc):
    other = TestClient(app)
    other.post("/auth/login", json=TESTER)
    utc.advance(days=main.SESSION_DAYS, seconds=1)
    assert other.get("/todos").status_code == 401


def test_forged_cookie_rejected():
    forged = TestClient(app, cookies={main.SESSION_COOKIE: "made-up-token"})
    assert forged.get("/todos").status_code == 401


def test_users_cannot_touch_each_others_todos():
    mine = update(create(title="tester 의 할 일"), completed=True)
    bob = new_user("bob")
    assert bob.get("/todos").json() == []                               # 남의 할 일은 목록에 없다
    assert bob.get("/todos", params={"q": "tester"}).json() == []        # 검색으로도 안 보인다
    assert bob.put(f"/todos/{mine['id']}", json={"title": "가로채기"}).status_code == 404
    assert bob.delete(f"/todos/{mine['id']}").status_code == 404        # 있는지조차 알려 주지 않는다
    assert bob.get("/stats").json()["total"] == 0                       # 통계도 사람별
    assert client.get("/todos").json()[0]["title"] == "tester 의 할 일"  # 원래 주인 것은 그대로


def test_delete_account_removes_only_own_data():
    create(title="tester 의 할 일")
    bob = new_user("bob")
    bob.post("/todos", json={"title": "bob 의 할 일", "completed": True})
    assert bob.delete("/auth/me").status_code == 204
    assert bob.get("/todos").status_code == 401
    assert TestClient(app).post("/auth/login", json={"username": "bob", "password": "password123"}).status_code == 401
    with sqlite3.connect(main.DB_FILE) as db:
        assert db.execute("SELECT COUNT(*) FROM completions WHERE user_id = 2").fetchone()[0] == 0
    assert [t["title"] for t in client.get("/todos").json()] == ["tester 의 할 일"]


def test_legacy_data_not_claimed_without_owner():
    # LEGACY_OWNER 를 정하지 않으면 주인 없는 v5 데이터는 아무도 가져가지 않는다
    # (배포 직후 통합 테스트의 일회용 계정이 가져갔다가 계정과 함께 지우는 사고를 막는다)
    write_legacy([{"id": 5, "title": "옛 할 일"}])
    main.migrate_legacy_json()
    client.post("/auth/login", json=TESTER)
    new_user("first_after_upgrade")
    assert client.get("/todos").json() == []
    with sqlite3.connect(main.DB_FILE) as db:
        assert db.execute("SELECT COUNT(*) FROM todos WHERE user_id IS NULL").fetchone()[0] == 1


def test_legacy_data_claimed_only_by_owner():
    write_legacy([{"id": 5, "title": "옛 할 일"}])
    main.migrate_legacy_json()
    main.LEGACY_OWNER = "Owner"                  # 대소문자는 구분하지 않는다
    client.post("/auth/login", json=TESTER)
    assert client.get("/todos").json() == []                            # tester 는 주인이 아니다
    owner = new_user("owner")
    assert [t["title"] for t in owner.get("/todos").json()] == ["옛 할 일"]


def test_upgrade_v5_database(tmp_path, monkeypatch, clock):
    # v5 DB(user_id 열 없음)를 그대로 두고 v6 를 띄워도, 열을 추가하고 LEGACY_OWNER 가 데이터와 통계를 가져간다
    v5_db = tmp_path / "v5.db"
    with sqlite3.connect(v5_db) as db:
        db.executescript("""
            CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', completed INTEGER NOT NULL DEFAULT 0, due_date TEXT,
                progress INTEGER NOT NULL DEFAULT 0, subtasks TEXT NOT NULL DEFAULT '[]',
                tags TEXT NOT NULL DEFAULT '[]', repeat TEXT NOT NULL DEFAULT 'none', completed_on TEXT);
            CREATE TABLE completions (id INTEGER PRIMARY KEY AUTOINCREMENT, todo_id INTEGER NOT NULL,
                title TEXT NOT NULL, completed_on TEXT NOT NULL);
            INSERT INTO todos (id, title, completed, completed_on) VALUES (9, 'v5 할 일', 1, '2026-10-07');
            INSERT INTO completions (todo_id, title, completed_on) VALUES (9, 'v5 할 일', '2026-10-07');
        """)
    monkeypatch.setattr(main, "DB_FILE", v5_db)
    main.LEGACY_OWNER = "owner"
    owner = new_user("owner")
    todos = owner.get("/todos").json()
    assert [(t["id"], t["title"]) for t in todos] == [(9, "v5 할 일")]
    assert owner.get("/stats").json()["total"] == 1


def test_security_headers():
    headers = client.get("/").headers
    assert headers["x-frame-options"] == "DENY"                         # 클릭재킹 방지
    assert headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert client.get("/todos").headers["x-frame-options"] == "DENY"    # API 응답에도 붙는다


@pytest.mark.parametrize("url", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_disabled_by_default(url):
    assert client.get(url).status_code == 404
