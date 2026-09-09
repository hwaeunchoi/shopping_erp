"""backfill_missing_permissions

릴리스 후보 통합 검증(요구사항 9번)에서 발견한 실제 배포 결함 수정.

scripts/init_db.py의 seed_roles_and_permissions()는 roles 테이블에 행이
하나라도 있으면 통째로 skip한다(신규 설치 1회성 시딩 스크립트 설계) - 그
결과 이미 운영 중인 DB(roles가 이미 존재)는 이후 단계(5-B/6단계 등)에서
scripts/init_db.py의 DEFAULT_PERMISSIONS에 새로 추가된 권한 코드
(CS_VIEW/CS_MANAGE/CS_ASSIGN/CS_CLOSE/CS_REPLY_SUBMIT/CS_PII_DETAIL/
OPERATIONS_RETRY/OPERATIONS_UNKNOWN_RESOLVE)가 `alembic upgrade head &&
python scripts/init_db.py`를 다시 실행해도 영원히 permissions/role_permissions
테이블에 추가되지 않는다 - Admin 계정조차 이 권한들을 갖지 못한 채로 남는다.

이 마이그레이션은 딱 두 가지만 한다.
1) DEFAULT_PERMISSIONS(이 마이그레이션 작성 시점 기준 전체 스냅샷)에 있는
   코드 중 permissions 테이블에 아직 없는 것만 삽입한다.
2) ROLE_PERMISSION_MAP에 있는 (역할명, 권한코드) 조합 중, 그 역할이 실제로
   존재하고 아직 매핑이 없는 것만 role_permissions에 삽입한다.

roles 테이블이 완전히 비어 있으면(진짜 신규 설치 - alembic upgrade head가
scripts/init_db.py보다 먼저 실행되므로 이 마이그레이션 시점엔 아직 아무
역할도 없을 수 있다) 아무 것도 하지 않는다 - 그 직후 실행되는
seed_roles_and_permissions()가 현재 DEFAULT_PERMISSIONS 전체로 처음부터
역할·권한·매핑을 만들 것이므로, 여기서 미리 permissions만 부분 삽입해두면
그 함수가 같은 code로 다시 INSERT를 시도할 때 permissions.code UNIQUE
제약 위반이 난다(반드시 피해야 하는 충돌).

기존 사용자·역할·다른 권한 매핑은 전혀 건드리지 않는다(UPDATE/DELETE 없음 -
INSERT만, 그것도 이미 있으면 건너뛴다).

downgrade()는 의도적으로 no-op이다 - 이 마이그레이션이 "새로 추가한" 행과
"원래 있었던" 행을 구분할 표식이 없어(멱등 INSERT라 이미 있던 행은 건드리지
않았다), 되돌릴 때 무엇을 지워야 안전한지 알 수 없다. 권한 추가는 되돌려도
안전 이득이 없고(더 열린 권한을 없애는 것뿐), 그 사이 신규 권한에 의존해
역할이 바뀌었을 수도 있다. 운영 롤백이 필요하면 downgrade가 아니라 배포
직전 DB 백업 복원을 사용해야 한다(release candidate 완료 보고서 5번 참고).

Revision ID: 0b2fdb82091c
Revises: 7a1f2c9de6b3
Create Date: 2026-09-09 10:30:00.000000+09:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0b2fdb82091c"
down_revision: Union[str, None] = "7a1f2c9de6b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# scripts/init_db.py의 DEFAULT_PERMISSIONS/ROLE_PERMISSION_MAP과 이 마이그레이션
# 작성 시점 기준으로 동일한 스냅샷이다 - 마이그레이션은 나중에 그 두 값이 바뀌어도
# 항상 같은 결과를 내야 하므로 import하지 않고 여기 얼려서 그대로 둔다(다른
# 마이그레이션이 이후 신규 권한을 또 추가하면 그때는 새 마이그레이션에서 그
# 시점의 스냅샷으로 다시 이 패턴을 반복한다).
_DEFAULT_PERMISSIONS: list[tuple[str, str, str]] = [
    ("DASHBOARD_VIEW", "대시보드 조회", "대시보드"),
    ("ORDER_VIEW", "주문 조회", "주문관리"),
    ("ORDER_EDIT", "주문 수정", "주문관리"),
    ("SHIPMENT_VIEW", "배송 조회", "배송관리"),
    ("EXCHANGE_RETURN_MANAGE", "교환/반품/취소 관리", "교환/반품"),
    ("SETTLEMENT_VIEW", "정산 조회", "정산관리"),
    ("AD_MANAGE", "광고 관리", "광고관리"),
    ("COST_MANAGE", "비용 관리", "비용관리"),
    ("PRODUCT_MANAGE", "상품 관리", "상품관리"),
    ("SUPPLIER_MANAGE", "공급처 관리", "공급처관리"),
    ("INVENTORY_VIEW", "재고 조회", "재고관리"),
    ("ANALYTICS_VIEW", "매출/손익 분석 조회", "매출/손익분석"),
    ("REPORT_VIEW", "보고서 조회/생성", "보고서"),
    ("NOTIFICATION_VIEW", "알림센터 조회", "알림센터"),
    ("SYSTEM_MONITOR_VIEW", "시스템 모니터링 조회", "시스템모니터링"),
    ("SETTINGS_MANAGE", "환경설정 관리", "설정"),
    ("AUDIT_LOG_VIEW", "감사로그 조회", "설정"),
    ("CS_VIEW", "CS 케이스 조회", "CS관리"),
    ("CS_MANAGE", "CS 케이스 생성/수정(메모·답변초안)", "CS관리"),
    ("CS_ASSIGN", "CS 담당자 배정", "CS관리"),
    ("CS_CLOSE", "CS 종결/재오픈", "CS관리"),
    ("CS_REPLY_SUBMIT", "CS 외부 채널 답변 접수", "CS관리"),
    ("CS_PII_DETAIL", "CS 개인정보 상세 조회", "CS관리"),
    ("OPERATIONS_RETRY", "통합 실패 작업함 선택 재처리", "운영대시보드"),
    ("OPERATIONS_UNKNOWN_RESOLVE", "통합 실패 작업함 UNKNOWN 수동 해소", "운영대시보드"),
]

_ROLE_PERMISSION_MAP: dict[str, list[str]] = {
    "Admin": [code for code, _, _ in _DEFAULT_PERMISSIONS],
    "Manager": [code for code, _, _ in _DEFAULT_PERMISSIONS if code not in ("SETTINGS_MANAGE", "AUDIT_LOG_VIEW")],
    "Viewer": [code for code, _, _ in _DEFAULT_PERMISSIONS if code.endswith("_VIEW")],
}


def upgrade() -> None:
    bind = op.get_bind()

    role_rows = bind.execute(sa.text("SELECT id, name FROM roles")).fetchall()
    if not role_rows:
        # 진짜 신규 설치 - scripts/init_db.py의 seed_roles_and_permissions()가
        # 곧이어 현재 DEFAULT_PERMISSIONS 전체로 처음부터 시딩한다. 여기서 먼저
        # permissions만 부분 삽입하면 그 함수의 INSERT가 UNIQUE 제약을 위반한다.
        return
    role_id_by_name = {name: role_id for role_id, name in role_rows}

    existing_codes = {
        row[0] for row in bind.execute(sa.text("SELECT code FROM permissions")).fetchall()
    }
    permissions_table = sa.table(
        "permissions",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("menu_group", sa.String),
    )
    missing = [
        {"code": code, "name": name, "menu_group": menu_group}
        for code, name, menu_group in _DEFAULT_PERMISSIONS
        if code not in existing_codes
    ]
    if missing:
        op.bulk_insert(permissions_table, missing)

    permission_id_by_code = {
        row[0]: row[1] for row in bind.execute(sa.text("SELECT code, id FROM permissions")).fetchall()
    }
    existing_pairs = {
        (row[0], row[1])
        for row in bind.execute(sa.text("SELECT role_id, permission_id FROM role_permissions")).fetchall()
    }
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission_id", sa.Integer),
    )
    new_mappings = []
    for role_name, codes in _ROLE_PERMISSION_MAP.items():
        role_id = role_id_by_name.get(role_name)
        if role_id is None:
            continue  # 이 운영 DB가 표준 3역할(Admin/Manager/Viewer) 이름을 바꿨을 수 있다 - 억지로 만들지 않는다.
        for code in codes:
            permission_id = permission_id_by_code.get(code)
            if permission_id is None:
                continue
            if (role_id, permission_id) in existing_pairs:
                continue
            new_mappings.append({"role_id": role_id, "permission_id": permission_id})
    if new_mappings:
        op.bulk_insert(role_permissions_table, new_mappings)


def downgrade() -> None:
    # 의도적 no-op - 위 모듈 docstring "downgrade()는 의도적으로 no-op이다" 참고.
    pass
