"""
services/fulfillment_carrier.py
------------------------------------
출고(피킹/검수/포장) 화면에서 쓰는 내부 택배사 코드 목록 + 송장번호
정규화/검증.

integrations.malls.carrier_codes와는 목적이 다르다 - 그 모듈은 "이 내부
코드를 이 채널 API에 그대로 보내도 되는가"(공식 문서로 확인된 것만)를
관리하고, 이 모듈은 "창고가 어느 택배사에 물건을 넘겼는가"라는 사업적
사실을 기록하기 위한 목록이다. 두 목적을 섞지 않는다 - 예를 들어 한진택배는
여기(INTERNAL_CARRIER_CODES)에는 있지만 아직 어느 채널의 공식 코드도
확인되지 않았으므로 carrier_codes._CARRIER_CODE_MAP에는 없다. 그 결과
한진택배로 송장을 등록하는 것 자체는 가능하지만(창고 업무 기록), 그 배송을
채널로 전송하려 하면(ShipmentDispatchService) integrations.malls.
carrier_codes.normalize_carrier_code()가 UnknownCarrierError로 안전하게
막는다 - 이 모듈이 추가로 채널 코드를 추측해서 채워 넣지 않는다.

송장번호 정규화 정책(공백/하이픈): 택배사 라벨을 손으로 옮겨 적거나 스캐너로
읽을 때 "123-456-789"와 "123 456 789"와 "123456789"가 같은 물리적 송장을
가리키는 경우가 흔하다 - 그래서 저장/중복비교 모두 공백·하이픈을 제거한
정규화 값 하나만 쓴다(원문을 별도 보관하지 않는다 - 애초에 하이픈 유무는
의미 정보가 아니라 표기 방식일 뿐이다). 그 외 문자(제어문자 포함)는 정규화로
지우지 않고 거부한다 - 조용히 잘라내면 서로 다른 두 송장이 우연히 같은 값으로
겹칠 수 있다.
"""

import re

# (내부 표준 코드, 화면 표시명). CJ_LOGISTICS는 integrations.malls.carrier_codes에도
# 등록되어 있어 채널 전송이 가능하다 - 나머지는 아직 어느 채널 코드도 공식 확인되지
# 않아 창고 기록(송장 등록)까지만 가능하고 채널 전송 시 명시적으로 차단된다.
INTERNAL_CARRIERS: list[tuple[str, str]] = [
    ("CJ_LOGISTICS", "CJ대한통운"),
    ("HANJIN", "한진택배"),
    ("LOTTE", "롯데택배"),
    ("LOGEN", "로젠택배"),
    ("EPOST", "우체국택배"),
    ("OTHER", "기타/직접입력"),
]
INTERNAL_CARRIER_CODES = frozenset(code for code, _ in INTERNAL_CARRIERS)

MAX_TRACKING_NO_LENGTH = 50  # models.order.Shipment.tracking_no 컬럼 폭(String(50))과 일치.
# 제어문자(0x00-0x1F, 0x7F) 차단 - 화면/로그에 그대로 표시되므로 개행/탭 등으로
# 표시가 깨지거나 인젝션에 악용되는 것을 막는다.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_OR_HYPHEN_RE = re.compile(r"[\s\-]+")


class InvalidCarrierError(ValueError):
    """INTERNAL_CARRIER_CODES에 없는 택배사 코드."""

    def __init__(self, carrier: str) -> None:
        super().__init__(f"허용되지 않은 택배사 코드입니다: {carrier}")


class InvalidTrackingNoError(ValueError):
    """빈 값/제어문자 포함/길이 초과 등 형식이 유효하지 않은 송장번호."""


def validate_carrier(carrier: str) -> str:
    if carrier not in INTERNAL_CARRIER_CODES:
        raise InvalidCarrierError(carrier)
    return carrier


def normalize_tracking_no(raw: str) -> str:
    """공백/하이픈을 제거한 정규화 값을 돌려준다 - 저장·중복비교 모두 이 값을 쓴다.

    빈 문자열(정규화 후 포함)/제어문자 포함/길이 초과는 거부한다. 앞자리 0은
    문자열이라 그대로 보존된다(숫자로 변환하지 않으므로 정보 손실이 없다)."""
    if _CONTROL_CHAR_RE.search(raw):
        raise InvalidTrackingNoError("송장번호에 제어 문자를 포함할 수 없습니다.")
    normalized = _WHITESPACE_OR_HYPHEN_RE.sub("", raw)
    if not normalized:
        raise InvalidTrackingNoError("송장번호를 입력하세요.")
    if len(normalized) > MAX_TRACKING_NO_LENGTH:
        raise InvalidTrackingNoError(f"송장번호가 너무 깁니다(최대 {MAX_TRACKING_NO_LENGTH}자).")
    return normalized
