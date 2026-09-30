import pytest
from fastapi.testclient import TestClient

import main
from main import app, save_todos, load_todos, TodoItem

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_and_teardown(tmp_path, monkeypatch):
    # 실제 todo.json 대신 테스트마다 새 임시 파일 사용 (본인 데이터 보호 + 테스트 간 격리)
    # TODO_FILE 은 본인 main.py의 파일 경로 변수명에 맞게 수정 (Path 객체 그대로 넘김, str()로 감싸지 않음)
    monkeypatch.setattr(main, "TODO_FILE", tmp_path / "todo.json")
    save_todos([])  # 테스트 전 초기화
    yield
    # 테스트 후 정리: tmp_path 와 monkeypatch 가 자동으로 원상 복구

def test_get_todos_empty():
    response = client.get("/todos")
    assert response.status_code == 200
    assert response.json() == []

def test_get_todos_with_items():
    todo = TodoItem(id=1, title="Test", description="Test description", completed=False)
    save_todos([todo])  # save_todos 는 TodoItem 객체 리스트를 받음
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
    assert len(load_todos()) == 1                # 파일에도 저장됐는지 확인

def test_create_todo_invalid():
    todo = {"description": "Test description"}   # 필수 필드 title 누락
    response = client.post("/todos", json=todo)
    assert response.status_code == 422

def test_update_todo():
    todo = TodoItem(id=1, title="Test", description="Test description", completed=False)
    save_todos([todo])
    updated_todo = {"title": "Updated", "description": "Updated description", "completed": True}
    response = client.put("/todos/1", json=updated_todo)
    assert response.status_code == 200
    assert response.json()["title"] == "Updated"

def test_create_todo_progress_default():
    response = client.post("/todos", json={"title": "코딩테스트 LV2"})
    assert response.status_code == 201
    assert response.json()["progress"] == 0      # 진행도를 안 보내면 0%

@pytest.mark.parametrize("progress", [-1, 101])
def test_create_todo_progress_out_of_range(progress):
    response = client.post("/todos", json={"title": "코딩테스트 LV2", "progress": progress})
    assert response.status_code == 422          # 0~100 범위 밖은 거부

def test_update_todo_progress():
    save_todos([TodoItem(id=1, title="코딩테스트 LV2")])
    response = client.put("/todos/1", json={"title": "코딩테스트 LV2", "progress": 60})
    assert response.status_code == 200
    assert load_todos()[0].progress == 60        # 파일에도 반영됐는지 확인

def test_old_data_with_priority_still_loads():
    # v3 데이터에는 priority 가 있고 progress 가 없다 — 오류 없이 0%로 읽혀야 한다
    main.TODO_FILE.write_text('[{"id": 1, "title": "old", "priority": "high"}]', encoding="utf-8")
    response = client.get("/todos")
    assert response.status_code == 200
    assert response.json()[0]["progress"] == 0

def test_create_todo_subtasks_default_empty():
    response = client.post("/todos", json={"title": "코딩테스트 LV2"})
    assert response.json()["subtasks"] == []     # 하위 작업을 안 보내면 빈 목록

def test_subtasks_derive_progress():
    subtasks = [
        {"title": "문제 1 풀기", "done": True},
        {"title": "문제 2 풀기", "done": True},
        {"title": "문제 3 풀기", "done": False},
    ]
    # 클라이언트가 보낸 progress(10)는 무시되고 체크 비율(2/3 = 67%)로 정해진다
    response = client.post("/todos", json={"title": "코딩테스트 LV2", "progress": 10, "subtasks": subtasks})
    assert response.status_code == 201
    assert response.json()["progress"] == 67
    saved = load_todos()[0]
    assert [s.title for s in saved.subtasks] == ["문제 1 풀기", "문제 2 풀기", "문제 3 풀기"]
    assert saved.progress == 67

def test_update_subtask_recomputes_progress():
    client.post("/todos", json={"title": "코딩테스트 LV2", "subtasks": [{"title": "문제 1"}, {"title": "문제 2"}]})
    assert load_todos()[0].progress == 0
    response = client.put("/todos/1", json={
        "title": "코딩테스트 LV2",
        "subtasks": [{"title": "문제 1", "done": True}, {"title": "문제 2", "done": True}],
    })
    assert response.json()["progress"] == 100

def test_subtask_empty_title_rejected():
    response = client.post("/todos", json={"title": "코딩테스트 LV2", "subtasks": [{"title": ""}]})
    assert response.status_code == 422

def test_too_many_subtasks_rejected():
    subtasks = [{"title": f"문제 {i}"} for i in range(main.MAX_SUBTASKS + 1)]
    response = client.post("/todos", json={"title": "코딩테스트 LV2", "subtasks": subtasks})
    assert response.status_code == 422

def test_version_is_4():
    assert client.get("/version").json() == {"version": "4.0.0"}

def test_update_todo_not_found():
    updated_todo = {"title": "Updated", "description": "Updated description", "completed": True}
    response = client.put("/todos/1", json=updated_todo)
    assert response.status_code == 404

def test_delete_todo():
    todo = TodoItem(id=1, title="Test", description="Test description", completed=False)
    save_todos([todo])
    response = client.delete("/todos/1")
    assert response.status_code == 204           # 삭제 성공 = 204 No Content (응답 본문 없음)
    assert load_todos() == []

def test_delete_todo_not_found():
    response = client.delete("/todos/1")
    assert response.status_code == 404