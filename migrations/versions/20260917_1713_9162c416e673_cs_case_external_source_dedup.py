"""cs_case_external_source_dedup

Revision ID: 9162c416e673
Revises: 560dd2f4b2ba
Create Date: 2026-09-17 17:13:58.223658+09:00

쿠팡 상품별 문의(onlineInquiries) 연동 추가(services/cs_channel_sync_service.py,
integrations/malls/coupang_connector.py 참고)를 위해 cs_cases의 유니크 제약
uq_cs_case_external_inquiry를 (platform_id, external_inquiry_id)에서
(platform_id, external_source, external_inquiry_id)로 넓힌다.

이유: 콜센터 문의(callCenterInquiries)와 상품별 문의(onlineInquiries)는 서로
다른 쿠팡 API이며, 공식 문서에 inquiryId의 고유 범위가 명시돼 있지 않다 -
안전하게 "서로 다른 독립 ID 공간일 수 있다"고 가정한다. 기존 2컬럼 제약만으로는
두 소스의 inquiryId가 우연히 같을 때 한쪽 문의가 다른 쪽으로 오인돼(같은 케이스로
잘못 갱신되거나, 두 번째 케이스가 아예 생성되지 못하는) 실제 버그가 있었다
(models/cs_case.py, repositories/cs_case_repository.py 클래스/메서드 docstring
참고).

데이터 안전성:
- cs_cases는 이 라운드 이전까지 콜센터 문의(external_source="COUPANG_CALL_CENTER")
  단일 소스만 채널 동기화로 만들었다 - scripts/init_db.py/scripts/seed_dummy_data.py
  어디에도 CsCase 시딩이 없어(확인 완료) 기존 채널 문의 행은 전부 같은
  external_source 값을 쓴다. 즉 기존 (platform_id, external_inquiry_id) 조합은
  이미 사실상 (platform_id, external_source, external_inquiry_id)와 동일한
  유일성을 만족하므로, 인덱스 교체 자체가 기존 행을 위반시키지 않는다(백필/
  데이터 이관 불필요).
- 수기 생성 케이스(platform_id/external_source/external_inquiry_id 전부 NULL)는
  기존과 동일하게 NULL 컬럼끼리는 유니크 제약에 걸리지 않는다(SQLite/PostgreSQL
  표준 동작 - order_items의 동일 관례와 일치).

⚠️ downgrade()로 되돌리면 이 시점 이후 두 소스가 동일 inquiryId로 실제 병존
생성된 행이 있을 경우 원래의 2컬럼 유니크 제약이 위반돼 실패할 수 있다 - 이는
의도된 동작이다(정확히 이 마이그레이션이 막으려는 데이터 오염 상태로 되돌아가는
것을 downgrade가 그 시점에 다시 명시적으로 막아준다는 뜻). 격리 환경 검증
목적의 downgrade에서는 문제가 되지 않는다(운영 DB에는 적용하지 않는다).
이 실패를 데이터 삭제나 임의 병합으로 우회하지 않는다 - 교차 출처 동일 ID 행이
존재하면 운영자가 행 단위로 검토해 결정하기 전까지 downgrade는 중단된 상태로 둔다.

⚠️ 이 마이그레이션은 이 브랜치에서 격리 PostgreSQL로만 검증했다 - 운영 DB에는
적용하지 않았다(운영 배포는 별도 승인 절차를 거친다).
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9162c416e673"
down_revision: Union[str, None] = "560dd2f4b2ba"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("uq_cs_case_external_inquiry", table_name="cs_cases")
    op.create_index(
        "uq_cs_case_external_inquiry",
        "cs_cases",
        ["platform_id", "external_source", "external_inquiry_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_cs_case_external_inquiry", table_name="cs_cases")
    op.create_index(
        "uq_cs_case_external_inquiry", "cs_cases", ["platform_id", "external_inquiry_id"], unique=True
    )
