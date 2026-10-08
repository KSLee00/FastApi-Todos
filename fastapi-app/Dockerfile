# Python 3.13 slim 베이스 이미지 (3.9는 2025-10 지원 종료, 최신 FastAPI는 Python 3.10 이상 필요)
FROM python:3.13-slim

# .pyc 생성 안 함 / 로그 즉시 출력 / pip 캐시·버전체크 끄기
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TODO_DATA_DIR=/app/data

# root가 아닌 실행 전용 사용자 생성
# /app/data 는 SQLite DB 가 들어갈 볼륨 자리 — 빈 볼륨은 이 폴더의 소유자(appuser)를 물려받는다
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /app/data && chown -R appuser:appuser /app

# 작업 디렉토리 설정
WORKDIR /app

# 의존성 먼저 설치 (코드만 바뀌면 이 단계는 캐시 재사용 → 빌드가 빨라짐)
COPY requirements.txt .
RUN pip install -r requirements.txt

# 실행에 필요한 파일만 복사한다 — 테스트·캐시·가상환경·DB 가 이미지에 들어가지 않는다
# (폴더 전체를 복사하는 COPY . . 는 SonarQube 가 보안 핫스팟으로 지적한다)
# 실제 데이터는 이미지가 아니라 /app/data 볼륨에 저장된다
COPY --chown=appuser:appuser main.py todo.json ./
COPY --chown=appuser:appuser templates ./templates

# 이후 명령은 appuser 권한으로 실행
USER appuser

EXPOSE 8000

# FastAPI 서버 실행
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]