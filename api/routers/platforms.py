"""
api/routers/platforms.py
----------------------------
쇼핑몰 플랫폼 기준정보 조회. 특정 권한 없이 로그인한 사용자면 조회 가능하다
(다른 화면에서 플랫폼 선택 드롭다운 등으로 공통 참조하는 기준정보이기 때문).
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db
from integrations.malls import MALL_CONNECTORS, SUPPORTED_CONNECTORS
from repositories.extra_repository import IntegrationStatusRepository
from repositories.platform_repository import PlatformRepository

router = APIRouter(prefix="/api/platforms", tags=["platforms"], dependencies=[Depends(get_current_user)])

# capability matrix 컬럼 - integrations.malls.base_mall_connector.BaseMallConnector의
# supports_* 클래스 속성과 정확히 같은 이름(접두어만 뗀 것)이어야 한다. 새 capability가
# 커넥터에 추가되면 여기도 함께 추가해야 화면에 반영된다(자동 introspection이 아니라
# 화이트리스트로 관리 - 커넥터에 실수로 붙은 임시/내부용 속성이 매트릭스에 새 줄로
# 새어나가지 않도록).
CAPABILITY_KEYS = [
    "product_create",
    "product_option_create",
    "product_info_update",
    "inventory_update",
    "sale_status_update",
    "shipment_submit",
    "cancellation_sync",
    "return_sync",
    "exchange_sync",
    "cancellation_lookup_by_order",
    "settlement_sync",
    "settlement_detail_sync",
]


class PlatformOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    connector_class: str
    settlement_cycle_days: Optional[int]
    is_active: bool


@router.get(
    "",
    response_model=list[PlatformOut],
    summary="쇼핑몰 플랫폼 목록 조회",
    description="활성 상태인 쇼핑몰 플랫폼(네이버 스마트스토어, 쿠팡 등) 기준정보를 조회한다.",
)
def list_platforms(db: Session = Depends(get_db)) -> list:
    return PlatformRepository(db).list_active()


class PlatformCapabilityOut(BaseModel):
    id: int
    code: str
    name: str
    connector_class: str
    is_active: bool
    # SUPPORTED_CONNECTORS(integrations.malls) 소속 여부 - 공식 API 계약이 실제로
    # 확인·검증된 채널인지를 뜻한다. False면 capabilities는 "개별 항목을 확인해봤더니
    # 전부 미지원"이 아니라 "아직 아무 것도 확인되지 않음"이므로 의도적으로 None을
    # 반환한다(capabilities={전부 false}로 표현하면 마치 하나하나 검증한 것처럼
    # 오인시킬 수 있다).
    official_contract_verified: bool
    capabilities: Optional[dict[str, bool]]
    last_success_at: Optional[datetime]
    last_error_at: Optional[datetime]
    last_error_message: Optional[str]


@router.get(
    "/capability-matrix",
    response_model=list[PlatformCapabilityOut],
    summary="플랫폼별 capability matrix 조회(운영 설정 화면용)",
    description="활성/비활성 여부와 무관하게 등록된 모든 플랫폼을 반환한다. 공식 API 계약이 아직 "
    "검증되지 않은 채널(예: 11번가·ESM·카카오쇼핑)은 capabilities가 null이다 - 개별 기능을 "
    "점검해서 모두 false로 나온 것이 아니라, 애초에 실 연동 자체가 없다는 뜻이다.",
)
def get_capability_matrix(db: Session = Depends(get_db)) -> list[PlatformCapabilityOut]:
    platforms = PlatformRepository(db).list_all()
    status_repo = IntegrationStatusRepository(db)
    results = []
    for platform in platforms:
        connector_cls = MALL_CONNECTORS.get(platform.connector_class)
        verified = connector_cls is not None and connector_cls in SUPPORTED_CONNECTORS
        capabilities = (
            {key: bool(getattr(connector_cls, f"supports_{key}", False)) for key in CAPABILITY_KEYS}
            if verified
            else None
        )
        status = status_repo.get_by_type_and_code("MALL", platform.code)
        results.append(
            PlatformCapabilityOut(
                id=platform.id,
                code=platform.code,
                name=platform.name,
                connector_class=platform.connector_class,
                is_active=platform.is_active,
                official_contract_verified=verified,
                capabilities=capabilities,
                last_success_at=status.last_success_at if status else None,
                last_error_at=status.last_error_at if status else None,
                last_error_message=status.last_error_message if status else None,
            )
        )
    return results
