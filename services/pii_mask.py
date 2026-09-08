"""
services/pii_mask.py
-----------------------
CS 케이스 화면에서 고객 이름/전화번호를 최소 표시하기 위한 마스킹 유틸.

이 코드베이스에는 고객 개인정보(전화/주소)를 마스킹하는 기존 패턴이 없었다
(services.settings_service._mask()는 API Credential 평문을 마지막 4자리만
보여주는 용도로만 쓰인다 - 목적은 다르지만 "일부만 노출"이라는 방식은 동일하게
따른다). 상세 전체 개인정보(mask 없는 원본)는 여기서 만들지 않는다 - 필요한
권한(CS_PII_VIEW)이 있는 호출자에게는 API 라우터가 이 함수를 거치지 않고
원본 필드를 그대로 반환한다(services/cs_case_service.py는 항상 마스킹된
값만 반환하고, api/routers/cs_cases.py가 권한에 따라 원본 필드를 추가로
포함할지 결정한다)."""


def mask_name(name: str) -> str:
    """이름의 첫 글자만 보이고 나머지는 마스킹한다(예: 홍길동 -> 홍**)."""
    if not name:
        return name
    if len(name) <= 1:
        return "*"
    return name[0] + "*" * (len(name) - 1)


def mask_phone(phone: str) -> str:
    """전화번호는 마지막 4자리만 노출한다(구분자는 그대로 유지, 숫자만 마스킹)."""
    if not phone:
        return phone
    digit_positions = [i for i, ch in enumerate(phone) if ch.isdigit()]
    if len(digit_positions) <= 4:
        return "".join("*" if ch.isdigit() else ch for ch in phone)
    reveal_from = digit_positions[-4]
    return "".join((ch if (not ch.isdigit() or i >= reveal_from) else "*") for i, ch in enumerate(phone))
