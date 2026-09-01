"""
integrations/malls/carrier_codes.py
-----------------------------------------
택배사 코드 정규화 - 내부 표준 코드를 채널(쇼핑몰)별 deliveryCompanyCode로 변환한다.

임의로 코드를 추정하지 않는다(운영 Secret 작업과 동일한 원칙). 이 파일에
등록된 코드만 공식 개발자 문서/문서에 준하는 자료로 확인한 값이다:
- CJGLS(CJ대한통운): 네이버 커머스 API 공식 문서 예시, 쿠팡 Open API 문서
  ("도서산간 배송" 예시), 카카오 쇼핑 Open API 택배사 코드 조회 문서 세 곳
  모두에서 동일 코드로 확인됨(2026-09 조회).
확인되지 않은 택배사는 절대 추측해서 채워 넣지 않는다 - UnknownCarrierError를
던져 안전하게 막는다(잘못된 코드로 실제 API를 호출해 반려/오류가 나는 것보다,
아예 호출하지 않는 편이 안전하다).
"""

# 내부 표준 코드 -> {platform_code: 채널 deliveryCompanyCode}
# 공식 문서로 교차 확인된 것만 등록한다. 새 택배사를 추가할 때는 반드시 해당
# 플랫폼의 공식 개발자 문서(또는 문서 내 택배사 코드 조회 API 응답)를 근거로 남길 것.
_CARRIER_CODE_MAP: dict[str, dict[str, str]] = {"CJ_LOGISTICS": {"naver_smartstore": "CJGLS", "coupang": "CJGLS"}}


class UnknownCarrierError(ValueError):
    """공식 문서로 확인되지 않은 택배사/플랫폼 조합 - 절대 추측하지 않고 거부한다."""

    def __init__(self, internal_carrier_code: str, platform_code: str) -> None:
        self.internal_carrier_code = internal_carrier_code
        self.platform_code = platform_code
        super().__init__(
            f"'{internal_carrier_code}' 택배사는 '{platform_code}' 채널용 코드가 아직 확인되지 않았습니다."
        )


def normalize_carrier_code(internal_carrier_code: str, platform_code: str) -> str:
    """내부 표준 택배사 코드를 채널별 deliveryCompanyCode로 변환한다.

    등록되지 않은 조합은 UnknownCarrierError를 던진다(더미/추측 폴백 없음)."""
    by_platform = _CARRIER_CODE_MAP.get(internal_carrier_code)
    if by_platform is None or platform_code not in by_platform:
        raise UnknownCarrierError(internal_carrier_code, platform_code)
    return by_platform[platform_code]


def supported_carriers(platform_code: str) -> list[str]:
    """해당 채널에 대해 코드가 확인된 내부 택배사 코드 목록(빈 목록일 수 있음)."""
    return sorted(code for code, by_platform in _CARRIER_CODE_MAP.items() if platform_code in by_platform)
