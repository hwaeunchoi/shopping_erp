"""api 패키지: FastAPI 라우터 (Presentation 접점).

- main.py: FastAPI 앱 엔트리포인트 (`uvicorn api.main:app --reload`).
- deps.py: DB 세션 주입, JWT 인증, RBAC 권한 검사 공용 의존성.
- routers/: 도메인별 APIRouter. Service/Repository만 호출하고 SQLAlchemy
  쿼리를 직접 다루지 않는다.
"""
