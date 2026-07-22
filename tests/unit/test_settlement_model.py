"""
tests/unit/test_settlement_model.py
------------------------------------------
settlements.settlement_cycle 컬럼 길이 회귀 테스트.

SQLite는 VARCHAR 길이 제약을 강제하지 않아 실제 데이터 트렁케이션 오류를
로컬 테스트에서 재현할 수 없다(PostgreSQL에서만 발생, 실제로
StringDataRightTruncation을 겪은 적 있음). 대신 (1) 컬럼에 선언된 길이가
커넥터가 실제로 생성하는 최대 길이 이상인지, (2) 그 값을 SQLite에 실제로
저장/조회해도 잘리지 않는지를 검증해 회귀를 막는다.
"""

from datetime import date, datetime, timezone

from sqlalchemy import String

from models.settlement import Settlement
from repositories.settlement_repository import SettlementRepository


def test_settlement_cycle_column_length_covers_date_range_format(db_session, platform):
    """ "YYYY-MM-DD~YYYY-MM-DD" 형식(21자)이 컬럼 길이(30)를 넘지 않는지 확인한다."""
    column_type = Settlement.__table__.columns["settlement_cycle"].type
    assert isinstance(column_type, String)
    max_cycle_str = f"{date(2026, 12, 31).isoformat()}~{date(2027, 1, 14).isoformat()}"

    assert column_type.length is not None
    assert len(max_cycle_str) <= column_type.length

    settlement = Settlement(
        platform_id=platform.id,
        settlement_cycle=max_cycle_str,
        expected_amount=0,
        settled_amount=0,
        unsettled_amount=0,
        discrepancy_amount=0,
        status="SCHEDULED",
        created_at=datetime.now(timezone.utc),
    )
    SettlementRepository(db_session).add(settlement)
    db_session.flush()

    saved = SettlementRepository(db_session).get_by_id(settlement.id)
    assert saved is not None
    assert saved.settlement_cycle == max_cycle_str
