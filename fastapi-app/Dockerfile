# Python 3.13 slim 베이스 이미지 (3.9는 2025-10 지원 종료, 최신 FastAPI는 Python 3.10 이상 필요)
FROM python:3.13-slim

# .pyc 생성 안 함 / 로그 즉시 출력 / pip 캐시·버전체크 끄기
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# root가 아닌 실행 전용 사용자 생성
RUN useradd --create-home --uid 10001 appuser \
 && mkdir /app && chown appuser:appuser /app

# 작업 디렉토리 설정
WORKDIR /app

# 의존성 먼저 설치 (코드만 바뀌면 이 단계는 캐시 재사용 → 빌드가 빨라짐)
COPY requirements.txt .
RUN pip install -r requirements.txt

# 소스 복사 (앱이 todo.json을 수정할 수 있도록 소유자를 appuser로 지정)
COPY --chown=appuser:appuser . .

# 이후 명령은 appuser 권한으로 실행
USER appuser

EXPOSE 8000

# FastAPI 서버 실행
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]