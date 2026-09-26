import json
import os
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

APP_VERSION = "3.0.0"                            # 화면 배지와 /version 이 함께 쓰는 단일 출처

BASE_DIR = Path(__file__).resolve().parent       # main.py 가 있는 폴더
TODO_FILE = BASE_DIR / "todo.json"
INDEX_FILE = BASE_DIR / "templates" / "index.html"

if not TODO_FILE.exists():                       # 없으면 빈 목록으로 만들어 둔다
    TODO_FILE.write_text("[]", encoding="utf-8")

app = FastAPI(title="To-Do List API", version=APP_VERSION)


class TodoIn(BaseModel):                         # 클라이언트가 보내는 데이터 (id 없음)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    completed: bool = False
    due_date: date | None = None                 # 마감일 (YYYY-MM-DD), 없으면 null
    priority: Literal["high", "medium", "low"] = "medium"   # 기존 데이터는 "보통"으로 읽힌다


class TodoItem(TodoIn):                          # 서버가 돌려주는 데이터 (id 있음)
    id: int


def load_todos() -> list[TodoItem]:
    raw = TODO_FILE.read_text(encoding="utf-8") if TODO_FILE.exists() else "[]"
    return [TodoItem(**t) for t in json.loads(raw)]


def save_todos(todos: list[TodoItem]) -> None:
    # mode="json" 이어야 date 가 "YYYY-MM-DD" 문자열로 바뀐다
    data = json.dumps([t.model_dump(mode="json") for t in todos], indent=2, ensure_ascii=False)
    # 원본을 직접 덮어쓰면 쓰는 도중 중단됐을 때 파일이 깨진다.
    # 임시 파일에 먼저 쓰고 통째로 갈아끼운다 (os.replace 는 원자적 연산).
    tmp_file = TODO_FILE.with_name(TODO_FILE.name + ".tmp")
    tmp_file.write_text(data, encoding="utf-8")
    os.replace(tmp_file, TODO_FILE)


def find_index(todos: list[TodoItem], todo_id: int) -> int:
    for i, todo in enumerate(todos):
        if todo.id == todo_id:
            return i
    raise HTTPException(404, "To-Do item not found")


@app.get("/todos")                               # 목록 조회
def get_todos() -> list[TodoItem]:
    return load_todos()


@app.post("/todos", status_code=201)             # 추가 — id 는 서버가 매긴다
def create_todo(payload: TodoIn) -> TodoItem:
    todos = load_todos()
    new_id = max((t.id for t in todos), default=0) + 1
    todo = TodoItem(id=new_id, **payload.model_dump())
    save_todos(todos + [todo])
    return todo


@app.put("/todos/{todo_id}")                     # 수정
def update_todo(todo_id: int, payload: TodoIn) -> TodoItem:
    todos = load_todos()
    index = find_index(todos, todo_id)           # 없는 id 면 여기서 404
    todo = TodoItem(id=todo_id, **payload.model_dump())
    todos[index] = todo
    save_todos(todos)
    return todo


@app.delete("/todos/{todo_id}", status_code=204)  # 삭제
def delete_todo(todo_id: int) -> None:
    todos = load_todos()
    del todos[find_index(todos, todo_id)]
    save_todos(todos)


@app.get("/version")                             # 화면 배지가 읽어가는 앱 버전
def get_version() -> dict[str, str]:
    return {"version": APP_VERSION}


@app.get("/", include_in_schema=False)           # 화면 서빙
def read_root() -> FileResponse:
    return FileResponse(INDEX_FILE, media_type="text/html")