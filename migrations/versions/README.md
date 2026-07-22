# migrations/versions

Alembic이 생성하는 마이그레이션 리비전 파일들이 이 폴더에 쌓입니다.

초기 마이그레이션(`..._initial_schema_50_tables.py`, 50개 테이블 전체)이
이미 생성되어 있고, 아래 항목을 검증했습니다 (자세한 내용은 루트
`README.md`의 "Alembic 검증" 절 참고):

- `alembic upgrade head` — 빈 DB에서 50개 테이블 전부 생성 확인
- `alembic check` — 현재 models와 마이그레이션 간 diff 없음 확인
- `alembic downgrade base` → `alembic upgrade head` — 완전한 롤백/재적용 확인

이후 모델을 수정할 때마다 아래 순서로 새 리비전을 추가합니다.
```
alembic revision --autogenerate -m "설명"
alembic upgrade head
```

**주의**: `alembic.ini`는 ASCII 문자만 사용합니다. Alembic 1.13.x가 설정
파일을 `encoding="locale"`(Python PEP 597)로 읽는데, 이는 `PYTHONUTF8`
설정과 무관하게 OS 로케일 코드페이지(한글 Windows의 경우 cp949)를 그대로
사용합니다. `alembic.ini`에 UTF-8로 저장된 비ASCII 문자(한글 등)가 있으면
`UnicodeDecodeError`로 모든 alembic 명령이 실패합니다 — 이 저장소에서
실제로 발견한 문제입니다.
