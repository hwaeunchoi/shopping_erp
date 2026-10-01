"""
tests/unit/test_coupang_cs_inquiry_tz_regression.py
--------------------------------------------------------
회귀 테스트 - 실계정 콜센터 문의 수집 1회차에서 재현된 실제 운영 버그
(fix/coupang-call-center-inquiry-persistence):

    TypeError: can't compare offset-naive and offset-aware datetimes

재현 경로: `integrations.malls.coupang_connector._parse_coupang_datetime()`가
tz-aware(KST) datetime을 반환했는데, 이 값이 naive DateTime 컬럼
(models.cs_case.CsCase.last_customer_message_at)에 저장된 뒤 같은 세션에서
다시 읽히면 naive로 바뀐다 - 그 상태에서 새로 파싱한 tz-aware `inquiry_at`과
`services.cs_channel_sync_service.CsChannelSyncService._upsert_one()`이
`inquiry_at > existing.last_customer_message_at`로 비교하면서 터진다.

콜센터 문의는 상태(NONE/ANSWER/NO_ANSWER/TRANSFER) 4종을 모두 조회해 하나의
`raw_items` 목록으로 합치므로, 같은 inquiryId가 복수 상태 응답에 중복 포함되면
**한 번의 동기화 실행 안에서** 방금 생성한 case를 바로 다시 읽어 비교하는
경로를 탄다. 상품별 문의는 `answeredType=ALL` 단일 조회라 한 실행 안에서 같은
inquiryId가 중복될 수 없어 이 분기를 타지 않았을 뿐이며, 재동기화(다음 스케줄
실행)에서는 두 source 모두 동일하게 영향받는 문제였다.

수정: `_parse_coupang_datetime()`가 이 코드베이스의 공통 관례(naive UTC)에
맞춰 항상 naive UTC datetime을 반환하도록 정규화 경계에서 고쳤다
(integrations/malls/coupang_connector.py). 이 파일은 그 수정이 실제로 버그를
막는지, 그리고 상품별 문의/주문 수집에 회귀가 없는지를 검증한다.

합성 fixture만 사용한다 - 실제 고객 응답/문의 본문을 저장하거나 복사하지 않는다.
"""

from datetime import date, datetime, timezone

import httpx
import pytest

from integrations.malls.coupang_connector import COUPANG_API_BASE, CoupangConnector, _parse_coupang_datetime
from repositories.cs_case_repository import CsCaseRepository
from services.cs_channel_sync_service import (
    COUPANG_CALL_CENTER_SOURCE,
    COUPANG_PRODUCT_INQUIRY_SOURCE,
    CsChannelSyncService,
)
from services.settings_service import ApiCredentialService

VENDOR_ID = "A00012345"


@pytest.fixture(autouse=True)
def _enable_cs_inquiry_sync(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "cs_inquiry_sync_enabled", True)


def _register_credentials(db_session, platform):
    svc = ApiCredentialService(db_session)
    svc.upsert_credential("PLATFORM", platform.id, "access_key", "test-access-key")
    svc.upsert_credential("PLATFORM", platform.id, "secret_key", "test-secret-key")
    svc.upsert_credential("PLATFORM", platform.id, "vendor_id", VENDOR_ID)
    db_session.flush()


class TestParseCoupangDatetimeReturnsNaiveUtc:
    """_parse_coupang_datetime() 자체의 계약 - 항상 naive UTC를 반환한다(이
    코드베이스의 공통 관례, models.base.utcnow/services의 _now()와 동일)."""

    def test_offsetless_string_assumed_kst_converted_to_naive_utc(self):
        result = _parse_coupang_datetime("2026-09-30T09:00:00")  # KST 09:00
        assert result.tzinfo is None
        assert result == datetime(2026, 9, 30, 0, 0, 0)  # UTC 00:00 (KST-9h)

    def test_offset_string_respected_then_converted_to_naive_utc(self):
        result = _parse_coupang_datetime("2026-09-30T09:00:00-08:00")
        assert result.tzinfo is None
        assert result == datetime(2026, 9, 30, 17, 0, 0)  # UTC = local + 8h

    def test_missing_value_falls_back_to_now_naive_utc(self):
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        result = _parse_coupang_datetime(None)
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        assert result.tzinfo is None
        assert before <= result <= after

    def test_two_parsed_values_are_always_mutually_comparable(self):
        """회귀 방지 핵심 계약 - 두 번 호출한 결과를 직접 비교해도 TypeError가
        나지 않아야 한다(naive-naive 비교)."""
        a = _parse_coupang_datetime("2026-09-30T09:00:00")
        b = _parse_coupang_datetime("2026-09-30T10:00:00")
        assert b > a  # TypeError 없이 비교 가능해야 한다.


def _call_center_item(inquiry_id, order_id=None, status="requestAnswer", content="문의"):
    return {
        "inquiryId": inquiry_id,
        "inquiryStatus": "progress",
        "csPartnerCounselingStatus": status,
        "content": content,
        "inquiryAt": "2026-09-30T09:00:00",
        "buyerPhone": "010-0000-0000",
        "orderId": order_id,
    }


def _paged_response(items, current_page=1, total_pages=1):
    return httpx.Response(
        200,
        json={
            "code": 200,
            "message": "OK",
            "data": {
                "content": items,
                "pagination": {
                    "currentPage": current_page,
                    "totalPages": total_pages,
                    "totalElements": len(items),
                    "countPerPage": 30,
                },
            },
        },
    )


class TestCallCenterDuplicateAcrossStatusesRegression:
    """핵심 회귀 테스트 - 같은 inquiryId가 서로 다른 partnerCounselingStatus
    응답에 중복 포함돼도(실계정에서 실제로 관찰된 상황) 한 번의 동기화 실행
    안에서 TypeError 없이 안전하게 처리돼야 한다(최초 1건 생성, 재등장 시
    갱신 - 중복 case 생성 없음)."""

    def test_duplicate_inquiry_in_two_statuses_creates_exactly_one_case(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            status_param = dict(request.url.params).get("partnerCounselingStatus")
            if status_param in ("NONE", "ANSWER"):
                return _paged_response([_call_center_item(inquiry_id=555, status="answered")])
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = CsChannelSyncService(db_session).sync_inquiries(
            connector,
            platform.id,
            date(2026, 9, 30),
            date(2026, 9, 30),
            source=COUPANG_CALL_CENTER_SOURCE,
            max_pages=1,
            max_retries=0,
        )

        assert result["status"] == "SUCCESS"
        assert result["created"] == 1
        assert result["updated"] == 1
        assert result["failed"] == 0
        matches = [
            c
            for c in CsCaseRepository(db_session).list_all(limit=100)
            if c.external_source == COUPANG_CALL_CENTER_SOURCE and c.external_inquiry_id == "555"
        ]
        assert len(matches) == 1  # 중복 case 생성 없음.

    def test_same_inquiry_duplicated_three_times_still_yields_one_case(self, db_session, platform):
        """4개 상태 중 3개에 동일 inquiryId가 겹쳐도(극단값) 여전히 1건으로
        수렴해야 한다."""
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            status_param = dict(request.url.params).get("partnerCounselingStatus")
            if status_param in ("NONE", "ANSWER", "NO_ANSWER"):
                return _paged_response([_call_center_item(inquiry_id=777, status="answered")])
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = CsChannelSyncService(db_session).sync_inquiries(
            connector,
            platform.id,
            date(2026, 9, 30),
            date(2026, 9, 30),
            source=COUPANG_CALL_CENTER_SOURCE,
            max_pages=1,
            max_retries=0,
        )

        assert result["status"] == "SUCCESS"
        assert result["created"] == 1
        assert result["updated"] == 2
        assert result["failed"] == 0


class TestCallCenterOrderLinkingTypeVariants:
    """주문번호가 문자열/정수/None으로 들어오는 세 가지 입력 변형이 전부 동일하게
    안전히 처리되는지 확인한다(orderId는 공식 응답상 Number지만, 방어적으로
    문자열이 들어와도 str() 캐스팅이라 동일하게 동작해야 한다)."""

    @pytest.mark.parametrize("order_id_value", [30001, "30001"])
    def test_order_id_as_int_or_str_links_to_existing_order(self, db_session, platform, order_id_value):
        from models.order import Order

        order = Order(
            platform_id=platform.id,
            platform_order_no="30001",
            status="NEW",
            order_date=datetime(2026, 9, 1),
            total_amount=10000,
        )
        db_session.add(order)
        db_session.flush()
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            status_param = dict(request.url.params).get("partnerCounselingStatus")
            if status_param == "NONE":
                return _paged_response([_call_center_item(inquiry_id=1001, order_id=order_id_value)])
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = CsChannelSyncService(db_session).sync_inquiries(
            connector,
            platform.id,
            date(2026, 9, 30),
            date(2026, 9, 30),
            source=COUPANG_CALL_CENTER_SOURCE,
            max_pages=1,
            max_retries=0,
        )

        assert result["status"] == "SUCCESS"
        case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "1001")
        assert case is not None
        assert case.order_id == order.id

    def test_order_id_none_leaves_case_unlinked(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            status_param = dict(request.url.params).get("partnerCounselingStatus")
            if status_param == "NONE":
                return _paged_response([_call_center_item(inquiry_id=1002, order_id=None)])
            return _paged_response([])

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        result = CsChannelSyncService(db_session).sync_inquiries(
            connector,
            platform.id,
            date(2026, 9, 30),
            date(2026, 9, 30),
            source=COUPANG_CALL_CENTER_SOURCE,
            max_pages=1,
            max_retries=0,
        )

        assert result["status"] == "SUCCESS"
        case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "1002")
        assert case is not None
        assert case.order_id is None


class TestResyncAcrossSeparateRunsRegression:
    """같은 버그의 더 넓은 형태 - 같은 source를 "별도의" 두 번째 실행(별도
    sync_inquiries() 호출)으로 재수집해도(스케줄러의 다음 15분 주기와 동일한
    상황) naive/aware 불일치 없이 최신 문의 시각으로 정상 갱신돼야 한다. 이
    분기는 상품별 문의/콜센터 문의 둘 다 동일한 위험이 있었다(둘 다
    `_upsert_one()`을 공유) - 두 source 모두 확인한다."""

    def test_call_center_resync_with_newer_content_updates_without_typeerror(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler_first(request: httpx.Request) -> httpx.Response:
            status_param = dict(request.url.params).get("partnerCounselingStatus")
            if status_param == "NONE":
                return _paged_response([_call_center_item(inquiry_id=2001, content="최초 문의")])
            return _paged_response([])

        client1 = httpx.Client(transport=httpx.MockTransport(handler_first), base_url=COUPANG_API_BASE)
        connector1 = CoupangConnector(session=db_session, platform_id=platform.id, http_client=client1)
        first = CsChannelSyncService(db_session).sync_inquiries(
            connector1,
            platform.id,
            date(2026, 9, 30),
            date(2026, 9, 30),
            source=COUPANG_CALL_CENTER_SOURCE,
            max_pages=1,
            max_retries=0,
        )
        assert first["status"] == "SUCCESS"
        assert first["created"] == 1

        def item_newer(inquiry_id, order_id=None, status="requestAnswer", content="문의"):
            item = _call_center_item(inquiry_id, order_id, status, content)
            item["inquiryAt"] = "2026-09-30T15:00:00"
            return item

        def handler_second(request: httpx.Request) -> httpx.Response:
            status_param = dict(request.url.params).get("partnerCounselingStatus")
            if status_param == "NONE":
                return _paged_response([item_newer(inquiry_id=2001, content="추가 문의")])
            return _paged_response([])

        client2 = httpx.Client(transport=httpx.MockTransport(handler_second), base_url=COUPANG_API_BASE)
        connector2 = CoupangConnector(session=db_session, platform_id=platform.id, http_client=client2)
        second = CsChannelSyncService(db_session).sync_inquiries(
            connector2,
            platform.id,
            date(2026, 9, 30),
            date(2026, 9, 30),
            source=COUPANG_CALL_CENTER_SOURCE,
            max_pages=1,
            max_retries=0,
        )

        assert second["status"] == "SUCCESS"
        assert second["created"] == 0
        assert second["updated"] == 1
        assert second["failed"] == 0
        case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "2001")
        assert case.last_customer_message_at == datetime(2026, 9, 30, 6, 0, 0)  # 15:00 KST -> 06:00 UTC


class TestCrossSourceSameNumericIdStillCoexist:
    """다른 source에서 동일 숫자 ID를 써도 충돌하지 않는지 - 이번 수정과 무관하게
    유지돼야 하는 기존 계약(models.cs_case.CsCase docstring 참고)을 call-center
    fetch 경로로도 재확인한다."""

    def test_call_center_and_product_inquiry_same_numeric_id_coexist(self, db_session, platform):
        _register_credentials(db_session, platform)

        def cc_handler(request: httpx.Request) -> httpx.Response:
            status_param = dict(request.url.params).get("partnerCounselingStatus")
            if status_param == "NONE":
                return _paged_response([_call_center_item(inquiry_id=9999, content="콜센터 쪽")])
            return _paged_response([])

        client_cc = httpx.Client(transport=httpx.MockTransport(cc_handler), base_url=COUPANG_API_BASE)
        connector_cc = CoupangConnector(session=db_session, platform_id=platform.id, http_client=client_cc)
        result_cc = CsChannelSyncService(db_session).sync_inquiries(
            connector_cc,
            platform.id,
            date(2026, 9, 30),
            date(2026, 9, 30),
            source=COUPANG_CALL_CENTER_SOURCE,
            max_pages=1,
            max_retries=0,
        )
        assert result_cc["status"] == "SUCCESS"

        def pi_handler(request: httpx.Request) -> httpx.Response:
            return _paged_response(
                [
                    {
                        "inquiryId": 9999,
                        "productId": 1,
                        "sellerProductId": 1,
                        "content": "상품별 쪽",
                        "inquiryAt": "2026-09-30T09:00:00",
                        "orderIds": [],
                        "commentDtoList": [],
                    }
                ]
            )

        client_pi = httpx.Client(transport=httpx.MockTransport(pi_handler), base_url=COUPANG_API_BASE)
        connector_pi = CoupangConnector(session=db_session, platform_id=platform.id, http_client=client_pi)
        result_pi = CsChannelSyncService(db_session).sync_inquiries(
            connector_pi,
            platform.id,
            date(2026, 9, 30),
            date(2026, 9, 30),
            source=COUPANG_PRODUCT_INQUIRY_SOURCE,
            max_pages=1,
            max_retries=0,
        )
        assert result_pi["status"] == "SUCCESS"

        cc_case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_CALL_CENTER_SOURCE, "9999")
        pi_case = CsCaseRepository(db_session).get_by_external(platform.id, COUPANG_PRODUCT_INQUIRY_SOURCE, "9999")
        assert cc_case is not None and pi_case is not None
        assert cc_case.id != pi_case.id


class TestOrderDateNormalizationRegression:
    """fetch_orders()의 order_date도 같은 _parse_coupang_datetime()을 쓰므로
    naive UTC로 정규화되는지 함께 확인한다(이 경로는 실제 TypeError가 보고되지
    않았지만, 같은 함수를 공유해 같은 위험에 노출돼 있었다 - 이번 수정으로 함께
    바로잡힌다)."""

    def test_fetch_orders_order_date_is_naive_utc(self, db_session, platform):
        _register_credentials(db_session, platform)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "shipmentBoxId": 1,
                            "orderId": 500001,
                            "orderedAt": "2026-09-30T09:00:00",
                            "status": "ACCEPT",
                            "orderer": {"name": "x", "safeNumber": "010-0000-0000"},
                            "receiver": {},
                            "orderItems": [],
                        }
                    ],
                    "nextToken": "",
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=COUPANG_API_BASE)
        connector = CoupangConnector(session=db_session, platform_id=platform.id, http_client=http_client)

        # fetch_orders()의 날짜 창 루프는 start<end(배타적)라 동일 날짜 한 점으로는
        # 요청이 전혀 나가지 않는다(주문 수집은 항상 다일 구간으로 호출되는 기존
        # 관례) - 이 테스트는 단지 order_date 정규화만 보고 싶으므로 1일 범위로 넓힌다.
        orders = connector.fetch_orders(date(2026, 9, 29), date(2026, 9, 30))

        assert orders[0]["order_date"].tzinfo is None
        assert orders[0]["order_date"] == datetime(2026, 9, 30, 0, 0, 0)
