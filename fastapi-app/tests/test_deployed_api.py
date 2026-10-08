# 배포된 서버에 실제 HTTP 요청을 보내 API를 확인하는 통합 테스트
# 실행: BASE_URL=http://[서버 IP]:[port num] EXPECTED_VERSION=5.0.0 pytest fastapi-app/tests/test_deployed_api.py
# BASE_URL 이 없으면 이 파일은 통째로 건너뛴다 → pytest fastapi-app/tests 는 단위 테스트만 실행된다
#
# v1 ~ v5 가 동시에 떠 있어도 이 파일 하나로 모두 검사한다.
# 먼저 서버 버전을 알아낸 뒤, 그 버전에 없는 기능의 테스트는 skip 한다.
#   v1: CRUD                     v2: + /version, 설명 500자 제한
#   v3: + 마감일(due_date)        v4: + 진행도, 하위 작업, 동시 요청 잠금
#   v5: + SQLite·볼륨(다시 배포해도 데이터 유지), 태그·검색, 반복 할 일, 학습 통계(/stats)
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest


BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
if not BASE_URL:
    pytest.skip("BASE_URL 이 없어 통합 테스트를 건너뜀 (배포 주소를 주면 실행된다)", allow_module_level=True)
# 이 주소에 떠 있어야 하는 버전 (예: 3.0.0). 비워 두면 버전 일치 검사는 건너뛴다.
EXPECTED_VERSION = os.environ.get("EXPECTED_VERSION", "")


def parse(version):
    return tuple(int(x) for x in version.split("."))


def wait_until_up(http, seconds=30):
    # 배포 직후에는 컨테이너가 아직 뜨는 중일 수 있으므로 응답이 올 때까지 기다림
    for _ in range(seconds):
        try:
            if http.get("/todos").status_code == 200:
                return
        except httpx2.TransportError:  # 연결 거부, 타임아웃 등
            pass
        time.sleep(1)
    pytest.fail(f"{BASE_URL} 에 접속할 수 없음 (컨테이너 실행 여부, 포트, 방화벽 확인)")


@pytest.fixture(scope="session")
def client():
    # 단위 테스트의 TestClient(app) 대신, 실제 배포 주소로 요청을 보내는 클라이언트
    with httpx2.Client(base_url=BASE_URL, timeout=5) as http:
        wait_until_up(http)
        yield http


@pytest.fixture(scope="session")
def server_version(client):
    # v1 에는 /version 이 없으므로 404 면 1.0.0 으로 본다
    response = client.get("/version")
    if response.status_code == 404:
        return "1.0.0"
    assert response.status_code == 200
    return response.json()["version"]


def since(version):
    # 테스트 함수에 "이 버전부터 있는 기능" 표시만 붙인다 — 판단은 아래 skip_if_older 가 한다
    def mark(test):
        test.since = version
        return test
    return mark


@pytest.fixture(autouse=True)
def skip_if_older(request, server_version):
    # @since("4.0.0") 이 붙은 테스트는 그보다 오래된 서버에서 건너뛴다
    required = getattr(request.function, "since", None)
    if required and parse(server_version) < parse(required):
        pytest.skip(f"v{required} 기능 — 이 서버는 v{server_version}")


@pytest.fixture
def todo(client):
    # 테스트용 항목을 하나 만들고, 테스트가 끝나면(실패해도) 지움 → 배포 서버의 실제 데이터는 그대로
    title = f"통합테스트-{uuid.uuid4().hex[:8]}"
    response = client.post("/todos", json={"title": title, "description": "integration test"})
    assert response.status_code == 201
    item = response.json()
    yield item
    # v5 는 완료 기록을 통계에 남기므로, 테스트가 완료로 바꿔 놓았다면 체크를 먼저 풀어 기록을 지운다
    client.put(f"/todos/{item['id']}", json={"title": title, "completed": False})
    client.delete(f"/todos/{item['id']}")  # 테스트 안에서 이미 지웠다면 404가 오지만 상관없음


def get_item(client, todo_id):
    # 응답이 아니라 서버에 "저장된" 값을 확인하기 위해 목록을 다시 읽어 온다
    response = client.get("/todos")
    assert response.status_code == 200
    return next(t for t in response.json() if t["id"] == todo_id)


# ---------- 모든 버전 공통 ----------

def test_expected_version_is_deployed(server_version):
    # 예전 컨테이너가 그대로 떠 있거나 포트를 잘못 연결한 배포 실수를 잡는다
    if not EXPECTED_VERSION:
        pytest.skip("EXPECTED_VERSION 이 지정되지 않음")
    assert server_version == EXPECTED_VERSION


def test_index_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_create_and_get(client, todo):
    response = client.get("/todos")
    assert response.status_code == 200
    assert todo["id"] in [t["id"] for t in response.json()]


def test_update(client, todo):
    payload = {"title": todo["title"], "description": "updated", "completed": True}
    response = client.put(f"/todos/{todo['id']}", json=payload)
    assert response.status_code == 200
    assert response.json()["completed"] is True


def test_update_is_persisted(client, todo):
    payload = {"title": todo["title"], "description": "persisted", "completed": True}
    assert client.put(f"/todos/{todo['id']}", json=payload).status_code == 200
    saved = get_item(client, todo["id"])       # 컨테이너 안에서 파일 쓰기가 막혀 있으면 여기서 드러난다
    assert saved["description"] == "persisted"
    assert saved["completed"] is True


def test_korean_round_trip(client, todo):
    payload = {"title": f"{todo['title']} 코딩테스트 LV2", "description": "프로그래머스 3문제 ✅"}
    assert client.put(f"/todos/{todo['id']}", json=payload).status_code == 200
    saved = get_item(client, todo["id"])       # 파일 인코딩이 틀리면 글자가 깨져서 돌아온다
    assert saved["title"] == payload["title"]
    assert saved["description"] == payload["description"]


def test_delete(client, todo):
    assert client.delete(f"/todos/{todo['id']}").status_code == 204
    assert client.delete(f"/todos/{todo['id']}").status_code == 404  # 이미 지운 항목은 404


def test_update_not_found(client):
    missing_id = max((t["id"] for t in client.get("/todos").json()), default=0) + 1000
    response = client.put(f"/todos/{missing_id}", json={"title": "없는 항목"})
    assert response.status_code == 404


def test_create_invalid(client):
    response = client.post("/todos", json={"description": "제목 없음"})  # 필수 필드 title 누락
    assert response.status_code == 422


# ---------- v3 부터 ----------

@since("3.0.0")
def test_due_date_round_trip(client, todo):
    payload = {"title": todo["title"], "due_date": "2026-12-31"}
    assert client.put(f"/todos/{todo['id']}", json=payload).status_code == 200
    assert get_item(client, todo["id"])["due_date"] == "2026-12-31"


# ---------- v4 부터 ----------

@since("4.0.0")
def test_index_page_is_v4(client):
    # HTML 이 오는지만 보면 예전 템플릿이 배포돼도 통과하므로 v4 화면의 문구를 확인한다
    html = client.get("/").text
    assert "하위 작업 추가" in html
    assert "코딩테스트 LV2" in html


@since("4.0.0")
def test_progress_is_persisted(client, todo):
    payload = {"title": todo["title"], "progress": 40}
    assert client.put(f"/todos/{todo['id']}", json=payload).status_code == 200
    assert get_item(client, todo["id"])["progress"] == 40


@since("4.0.0")
def test_subtasks_derive_progress(client, todo):
    payload = {
        "title": todo["title"],
        "progress": 5,                         # 하위 작업이 있으면 서버가 무시해야 하는 값
        "subtasks": [
            {"title": "문제 1 풀기", "done": True},
            {"title": "문제 2 풀기", "done": True},
            {"title": "문제 3 풀기", "done": False},
        ],
    }
    response = client.put(f"/todos/{todo['id']}", json=payload)
    assert response.status_code == 200
    assert response.json()["progress"] == 67   # 2/3
    saved = get_item(client, todo["id"])
    assert saved["progress"] == 67
    assert [s["title"] for s in saved["subtasks"]] == ["문제 1 풀기", "문제 2 풀기", "문제 3 풀기"]
    assert [s["done"] for s in saved["subtasks"]] == [True, True, False]


@since("4.0.0")  # v1~v3 에는 잠금이 없어서 실패한다 (이미 배포된 옛 버전이라 고치지 않음)
def test_concurrent_creates(client):
    # 동시에 추가해도 id 가 겹치거나 저장한 항목이 사라지면 안 된다 (main.py 의 todo_lock)
    n = 15
    prefix = f"통합테스트-동시-{uuid.uuid4().hex[:8]}"

    def create(i):
        return client.post("/todos", json={"title": f"{prefix}-{i}"})

    with ThreadPoolExecutor(n) as pool:
        responses = list(pool.map(create, range(n)))
    try:
        assert [r.status_code for r in responses] == [201] * n
        ids = [r.json()["id"] for r in responses]
        assert len(set(ids)) == n              # id 가 모두 달라야 한다
        saved = [t for t in client.get("/todos").json() if t["title"].startswith(prefix)]
        assert len(saved) == n                 # 전부 파일에 남아 있어야 한다
    finally:
        # 겹친 id 가 있었어도 이 테스트가 만든 항목은 모두 지운다
        for t in client.get("/todos").json():
            if t["title"].startswith(prefix):
                client.delete(f"/todos/{t['id']}")


# ---------- v5 부터 ----------

@since("5.0.0")
def test_index_page_is_v5(client):
    html = client.get("/").text
    assert 'id="search"' in html                # 검색창
    assert "학습 통계" in html
    assert "매일 반복" in html


@since("5.0.0")
def test_blank_title_rejected(client):
    # v4 까지는 공백만 있는 제목이 저장됐다
    assert client.post("/todos", json={"title": "   "}).status_code == 422


@since("5.0.0")
def test_tags_and_repeat_are_persisted(client, todo):
    payload = {"title": todo["title"], "tags": [" LV2 ", "프로그래머스", "lv2"], "repeat": "weekly"}
    response = client.put(f"/todos/{todo['id']}", json=payload)
    assert response.status_code == 200
    saved = get_item(client, todo["id"])
    assert saved["tags"] == ["LV2", "프로그래머스"]   # 공백·대소문자 중복 정리
    assert saved["repeat"] == "weekly"


@since("5.0.0")
def test_search_and_tag_filter(client, todo):
    marker = uuid.uuid4().hex[:10]               # 서버의 다른 데이터와 겹치지 않는 검색어·태그
    payload = {"title": todo["title"], "description": f"검색-{marker}", "tags": [f"tag-{marker}"]}
    assert client.put(f"/todos/{todo['id']}", json=payload).status_code == 200

    by_query = client.get("/todos", params={"q": marker.upper()}).json()      # 대소문자 무시
    assert [t["id"] for t in by_query] == [todo["id"]]
    by_tag = client.get("/todos", params={"tag": f"TAG-{marker}"}).json()
    assert [t["id"] for t in by_tag] == [todo["id"]]
    assert client.get("/todos", params={"q": f"없는-{marker}"}).json() == []


@since("5.0.0")
def test_stats_shape(client):
    response = client.get("/stats")
    assert response.status_code == 200
    stats = response.json()
    assert len(stats["recent"]) == 7
    assert stats["recent"][-1]["date"] == stats["today"]                       # 마지막 막대가 오늘
    assert stats["recent_total"] == sum(d["count"] for d in stats["recent"])
    assert stats["streak"] >= (1 if stats["recent"][-1]["count"] else 0)


@since("5.0.0")
def test_completion_updates_stats(client, todo):
    before = client.get("/stats").json()
    done = {"title": todo["title"], "completed": True}
    try:
        saved = client.put(f"/todos/{todo['id']}", json=done).json()
        assert saved["completed_on"] == before["today"]                     # 서버가 완료한 날을 기록
        after = client.get("/stats").json()
        assert after["total"] == before["total"] + 1
        assert after["recent"][-1]["count"] == before["recent"][-1]["count"] + 1
        assert after["streak"] >= 1
    finally:
        # 체크를 풀면 기록도 지워지므로 배포 서버의 실제 통계는 그대로 남는다
        client.put(f"/todos/{todo['id']}", json={**done, "completed": False})
    assert client.get("/stats").json()["total"] == before["total"]


PERSIST_TITLE = "통합테스트-배포유지확인 (지우지 마세요)"


@since("5.0.0")
def test_data_survives_redeploy(client):
    # 배포할 때마다 실행되면, 앞선 배포 때 만든 항목이 이번 배포 뒤에도 남아 있는지 확인한다.
    # 처음 실행하면 항목만 만들고 skip — 다음 배포 뒤 실행부터 실제로 검사된다.
    existing = [t for t in client.get("/todos").json() if t["title"] == PERSIST_TITLE]
    if not existing:
        response = client.post("/todos", json={"title": PERSIST_TITLE, "description": "Docker 볼륨 확인용"})
        assert response.status_code == 201
        pytest.skip("확인용 항목을 만들었음 — 다음 배포 뒤 실행하면 데이터 유지 여부를 검사한다")
    assert len(existing) == 1                    # 중복으로 만들어졌다면 이전 데이터가 한 번 사라졌던 것
