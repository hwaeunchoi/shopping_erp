"""
tests/integration/_coupang_tz_compat_runner.py
--------------------------------------------------
실제(격리) PostgreSQL 위에서 돌아간다 - SQLite는 timezone 처리 방식이
psycopg3와 달라(SQLite는 Python datetime을 그대로 직렬화/역직렬화하므로
"DB가 tz-aware 값을 어떻게 저장하는지"를 실증할 수 없다) 이 질문에는 쓸 수
없다.

fix/coupang-call-center-inquiry-persistence 병합 전 호환성 감사:
`_parse_coupang_datetime()`이 기존에 반환하던 tz-aware(KST) datetime을
timezone 없는 `DateTime` 컬럼에 실제로 어떻게 저장했는지(KST 벽시각을 그대로
naive로 저장했는지, 아니면 UTC로 환산한 뒤 naive로 저장했는지)를 운영과 동일한
PostgreSQL 16 + SQLAlchemy + psycopg3 조합으로 직접 실증한다.

시나리오:
1) legacy_kst_write_vs_new_utc_write_same_instant - "구식" 방식(tz-aware KST
   datetime)과 "신규" 방식(naive UTC datetime)으로 **같은 실제 순간**을 각각
   써서, DB에 저장되는 raw 텍스트가 동일한지 확인한다.
2) boundary_comparisons - 8시간59분/9시간/9시간1분 경계에서 비교 연산이
   기대대로 동작하는지(naive-naive 비교, TypeError 없음).
3) same_session_flush_then_reread - _upsert_one()이 실제로 타는 경로(같은
   세션에서 flush 후 다시 조회)를 재현해, tz-aware 입력이 남아있었다면 여전히
   TypeError가 나는지(수정 후에는 발생하지 않아야 함) 확인한다.
4) offset_included_vs_excluded - 응답에 오프셋이 포함된 경우/없는 경우 모두
   동일한 실제 순간이면 동일한 저장값이 나오는지 확인한다.

결과를 JSON 한 줄로 표준출력 마지막 줄에 출력한다. 실제 쿠팡 API는 호출하지
않는다(합성 fixture만 사용). 고객 데이터는 전혀 관여하지 않는다.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

db_host = os.environ["TZCOMPAT_DB_HOST"]
db_user = os.environ["TZCOMPAT_DB_USER"]
db_password = os.environ["TZCOMPAT_DB_PASSWORD"]
db_name = os.environ["TZCOMPAT_DB_NAME"]

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from integrations.malls.coupang_connector import _parse_coupang_datetime  # noqa: E402
from models.base import Base  # noqa: E402
from models.cs_case import CsCase  # noqa: E402
from models.platform import Platform  # noqa: E402

DB_URL = f"postgresql+psycopg://{db_user}:{db_password}@{db_host}:5432/{db_name}"
_KST = timezone(timedelta(hours=9))


def _legacy_parse(value: str) -> datetime:
    """수정 전 _parse_coupang_datetime()의 동작 그대로 - tz-aware(KST 또는
    명시된 오프셋) datetime을 반환한다(비교용으로 이 러너 안에서만 재현,
    운영 코드는 건드리지 않는다)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_KST)
    return parsed


def main() -> None:
    engine = create_engine(DB_URL, future=True)
    Base.metadata.create_all(engine)
    results: dict = {}

    with Session(engine) as session:
        platform = Platform(
            code="coupang", name="c", connector_class="CoupangConnector", settlement_cycle_days=15, is_active=True
        )
        session.add(platform)
        session.flush()
        platform_id = platform.id
        session.commit()

    # --- 1) 구식(tz-aware KST) vs 신규(naive UTC) 방식으로 같은 실제 순간 저장 ---
    same_instant_str = "2026-09-30T14:00:00"  # KST 벽시각으로 해석
    legacy_value = _legacy_parse(same_instant_str)  # tz-aware KST
    new_value = _parse_coupang_datetime(same_instant_str)  # naive UTC (수정 후 동작)

    with Session(engine) as session:
        case_legacy = CsCase(
            platform_id=platform_id,
            external_inquiry_id="legacy-1",
            external_source="COUPANG_CALL_CENTER",
            inquiry_type="ETC",
            priority="NORMAL",
            status="OPEN",
            customer_message="synthetic",
            last_customer_message_at=legacy_value,
        )
        session.add(case_legacy)
        session.flush()
        legacy_id = case_legacy.id
        session.commit()

    with Session(engine) as session:
        case_new = CsCase(
            platform_id=platform_id,
            external_inquiry_id="new-1",
            external_source="COUPANG_CALL_CENTER",
            inquiry_type="ETC",
            priority="NORMAL",
            status="OPEN",
            customer_message="synthetic",
            last_customer_message_at=new_value,
        )
        session.add(case_new)
        session.flush()
        new_id = case_new.id
        session.commit()

    with Session(engine) as session:
        legacy_raw = session.execute(
            text("SELECT last_customer_message_at::text FROM cs_cases WHERE id = :id"), {"id": legacy_id}
        ).scalar()
        new_raw = session.execute(
            text("SELECT last_customer_message_at::text FROM cs_cases WHERE id = :id"), {"id": new_id}
        ).scalar()
    results["legacy_write_raw_stored"] = legacy_raw
    results["new_write_raw_stored"] = new_raw
    results["legacy_and_new_writes_identical"] = legacy_raw == new_raw

    # --- 2) 8h59m / 9h / 9h1m 경계 비교 ---
    base = _parse_coupang_datetime("2026-09-30T00:00:00")
    for label, delta_minutes in [("8h59m", 8 * 60 + 59), ("9h00m", 9 * 60), ("9h01m", 9 * 60 + 1)]:
        later_str_kst_wallclock = (datetime(2026, 9, 30, 9, 0, 0) + timedelta(minutes=delta_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        later = _parse_coupang_datetime(later_str_kst_wallclock)
        try:
            cmp_ok = later > base
            results[f"boundary_{label}_comparable"] = True
            results[f"boundary_{label}_is_later"] = cmp_ok
        except TypeError:
            results[f"boundary_{label}_comparable"] = False

    # --- 3) 같은 세션 flush -> 재조회 경로(_upsert_one()이 실제로 타는 경로) ---
    with Session(engine) as session:
        dup_case = CsCase(
            platform_id=platform_id,
            external_inquiry_id="dup-reread",
            external_source="COUPANG_CALL_CENTER",
            inquiry_type="ETC",
            priority="NORMAL",
            status="OPEN",
            customer_message="synthetic",
            last_customer_message_at=_parse_coupang_datetime("2026-09-30T09:00:00"),
        )
        session.add(dup_case)
        session.flush()
        try:
            from sqlalchemy import select

            reread = session.execute(select(CsCase).where(CsCase.id == dup_case.id)).scalar_one()
            newer = _parse_coupang_datetime("2026-09-30T15:00:00")
            existing_value = reread.last_customer_message_at
            _ = existing_value is None or newer > existing_value
            results["same_session_reread_compare_ok"] = True
        except TypeError:
            results["same_session_reread_compare_ok"] = False
        session.rollback()

    # --- 4) 오프셋 포함/미포함 응답이 같은 실제 순간이면 동일 저장값인지 ---
    offsetless = _parse_coupang_datetime("2026-09-30T09:00:00")  # KST로 간주
    with_offset = _parse_coupang_datetime("2026-09-30T09:00:00+09:00")  # 명시적 KST 오프셋
    results["offset_included_vs_excluded_identical"] = offsetless == with_offset

    print("TZCOMPAT_RESULT_JSON:" + json.dumps(results, default=str))


if __name__ == "__main__":
    main()
