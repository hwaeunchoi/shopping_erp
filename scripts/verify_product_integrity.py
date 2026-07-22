"""
scripts/verify_product_integrity.py
---------------------------------------
상품/옵션/플랫폼매핑 데이터 정합성 자동 검증 스크립트 (2026-07-08 상품 데이터
재설계 및 병합 작업 이후 도입).

과거(2026-07-07) 네이버 상품 API 스키마 오해로 인한 대량 오등록 사고와, 그 뒤를
이은 수동 병합 작업(6개 그룹/15개 AUTO product) 과정에서 사람이 매번 SQL로
직접 조사해야 했던 7가지 점검 항목을 자동화한다. 운영 중 정기적으로(또는 네이버
상품 동기화 직후) 실행해 같은 문제가 재발하는지 사람이 매번 조사하지 않아도
바로 알 수 있게 한다.

실행 방법:
    python scripts/verify_product_integrity.py
    echo $?
    # 0 = ERROR 없음(배포/계속 진행 가능, WARNING이 있어도 0)
    # 1 = ERROR 1건 이상 존재(배포 중단 권장) - CI/배포 파이프라인에서 그대로 게이트로 사용

판정 기준:
    - ERROR: 구조적으로 있어서는 안 되는 상태(유니크 제약 위반, groupProductNo
      분리, 참조 무결성 깨짐). 즉시 조사·수정 필요, 종료 코드에 반영됨.
    - WARNING: 지금 당장 장애를 일으키지는 않지만 성격이 다른 두 종류로 나뉜다.
      각 점검 함수의 note 필드에 어느 쪽인지 명시한다.
        * "기술부채" - 운영에 영향 없음, 별도 승인 후 여유 있게 정리하면 되는 것
        * "수동검토필요" - 자동 탐지/자동 병합의 사각지대에 있는 알려진 문제로,
          방치하면 향후 동기화 때 실제로 다시 중복이 생길 수 있어 사람이 판단해야
          하는 것(예: PlatformMap이 아예 없는 옵션은 groupProductNo 기반 자동
          병합 대상 자체가 될 수 없다)
    WARNING은 종료 코드에 영향을 주지 않는다(운영 배포를 막지 않는다) - 다만
    출력에서 항상 전부 나열되므로 운영자가 verify 실행 결과만 보고도 무엇이
    남아있는지 파악할 수 있다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from core.database import session_scope  # noqa: E402


class CheckResult:
    def __init__(self, name: str, level: str, count: int, details: list[str], note: str = "") -> None:
        self.name = name
        self.level = level  # "PASS" | "WARNING" | "ERROR"
        self.count = count
        self.details = details
        self.note = note  # WARNING일 때만: "기술부채" | "수동검토필요"


def check_duplicate_platform_option_id(db) -> CheckResult:
    """동일 (platform_id, platform_option_id) 조합이 2개 이상의 매핑 행을 갖는 경우.
    2026-07-08 마이그레이션으로 이 조합에 유니크 제약(uq_platform_option_id)이
    걸려 있어 신규 발생은 불가능해야 한다 - 그래도 방어적으로 점검한다."""
    rows = db.execute(text("""
            select platform_id, platform_option_id, count(*) as cnt
            from product_platform_map
            group by platform_id, platform_option_id
            having count(*) > 1
            """)).all()
    details = [f"platform_id={r[0]} platform_option_id={r[1]} 중복 {r[2]}건" for r in rows]
    level = "ERROR" if rows else "PASS"
    return CheckResult("동일 platform_option_id 중복", level, len(rows), details)


def check_split_group_products(db) -> CheckResult:
    """같은 platform_product_id(네이버 groupProductNo)를 가진 옵션들이 서로 다른
    (소프트 삭제되지 않은) Product로 쪼개져 있는 경우 - 2026-07-07 사고의 핵심 증상."""
    rows = db.execute(text("""
            select m.platform_id, m.platform_product_id, count(distinct po.product_id) as product_count,
                   array_agg(distinct po.product_id order by po.product_id) as product_ids
            from product_platform_map m
            join product_options po on po.id = m.product_option_id
            join products p on p.id = po.product_id
            where m.platform_product_id is not null and p.is_deleted = false
            group by m.platform_id, m.platform_product_id
            having count(distinct po.product_id) > 1
            """)).all()
    details = [
        f"platform_id={r[0]} platform_product_id={r[1]} -> product_id={list(r[3])} ({r[2]}개로 분리)" for r in rows
    ]
    level = "ERROR" if rows else "PASS"
    return CheckResult("동일 groupProductNo가 여러 Product로 분리", level, len(rows), details)


def check_deleted_product_with_options(db) -> CheckResult:
    """소프트 삭제된(is_deleted=true) Product인데 아직 옵션이 남아있는 경우 -
    병합 시 옵션을 다른 Product로 옮기지 않고 삭제만 한 실수를 잡아낸다."""
    rows = db.execute(text("""
            select p.id, p.name, count(po.id) as option_count
            from products p
            join product_options po on po.product_id = p.id
            where p.is_deleted = true
            group by p.id, p.name
            """)).all()
    details = [f"product_id={r[0]} name={r[1]!r} 옵션 {r[2]}건 잔존" for r in rows]
    level = "WARNING" if rows else "PASS"
    return CheckResult(
        "Soft Delete Product에 Option 잔존",
        level,
        len(rows),
        details,
        note="기술부채 - 개별 건마다 platform_map/order_item 연결 여부를 확인해 완전히 고립된 테스트/실수 데이터라면 삭제 가능",
    )


def check_legacy_self_referential_map(db) -> CheckResult:
    """platform_option_id == platform_product_id 이면서 seller_product_code가
    비어있는 자기참조 legacy 매핑 - 병합 전(리팩터링 이전) 방식으로 등록된
    미정리 잔여 데이터를 나타낸다.

    현재는 운영 장애를 일으키지 않지만(대부분 주문/재고 연결이 없는 고아
    데이터), "기술부채"로만 가볍게 보지 말아야 한다 - 2026-07-08 대량 병합
    작업에서 바로 이 패턴의 행을 지우는 로직 자체에 검증 누락이 있어 1,354건을
    잘못 삭제했다가 백업에서 복원한 사례가 있다. 향후 이 데이터를 정리(삭제)할
    때는 반드시 "삭제 후에도 해당 옵션에 정상 매핑이 최소 1개 남는지"를 SQL로
    재확인한 뒤에만 진행해야 한다(scripts/merge_split_product_groups.py의
    build_plan()이 그 기준을 구현한 참고 코드)."""
    rows = db.execute(text("""
            select id, product_option_id, platform_id, platform_option_id
            from product_platform_map
            where platform_option_id = platform_product_id
              and (seller_product_code is null or seller_product_code = '')
            order by id
            """)).all()
    details = [f"map_id={r[0]} option_id={r[1]} platform_id={r[2]} code={r[3]}" for r in rows]
    level = "WARNING" if rows else "PASS"
    return CheckResult(
        "Legacy 자기참조 platform_map 존재",
        level,
        len(rows),
        details,
        note=(
            "기술부채 - 지금은 운영 장애 없음. 단, 정리(삭제) 시 반드시 "
            "'삭제 후에도 해당 옵션에 정상 매핑이 1개 이상 남는지' 검증 후에만 삭제할 것 "
            "(과거 검증 없이 삭제해 1,354건을 복원한 사고 있음)"
        ),
    )


def check_auto_sku(db) -> CheckResult:
    """AUTO-{platform_id}-{code} 형식의 SKU - 2026-07-08 정책(SKU는 ERP 내부
    채번, SKU-{id:06d})부터는 신규 생성이 금지되었다. 기존 잔여분은 알려진
    상태(2,421개 완전 고아 + 79개 단독 정상, 별도 승인 후 정리 예정)이므로
    WARNING으로만 보고한다."""
    total = db.execute(text("select count(*) from product_options where sku_code like 'AUTO-%'")).scalar_one()
    level = "WARNING" if total > 0 else "PASS"
    details = [f"AUTO-* SKU 총 {total}건 (기존 잔여 데이터, 별도 승인 후 정리 예정)"] if total else []
    return CheckResult(
        "AUTO SKU 존재 여부",
        level,
        total,
        details,
        note="기술부채 - 운영 영향 없음(SKU는 매칭 로직에 전혀 사용되지 않는 ERP 내부 표시값일 뿐)",
    )


def check_option_without_platform_map(db) -> CheckResult:
    """어떤 플랫폼에도 매핑되지 않은 옵션 - 네이버 동기화로 생성된 옵션이라면
    있어서는 안 되지만, 사용자가 상품관리 화면에서 수동으로 만든 옵션은 정상적으로
    이 상태일 수 있다.

    WARNING 중 가장 중요한 항목이다: groupProductNo 기반 자동 분리 탐지
    (check_split_group_products)는 platform_map이 있는 옵션만 대상으로 하므로,
    이 항목에 걸리는 옵션은 애초에 자동 병합 대상이 될 수 없는 사각지대다.
    이런 옵션과 같은 물리 상품이 나중에 네이버 상품 동기화로 다시 들어오면,
    이 옵션과 매칭할 방법이 없어 새 ProductOption이 또 생성될 수 있다 - 즉
    "수동 검토가 필요한 known duplicate 후보"로 보고 사람이 판단해야 한다."""
    rows = db.execute(text("""
            select po.id, po.product_id, po.sku_code
            from product_options po
            left join product_platform_map m on m.product_option_id = po.id
            join products p on p.id = po.product_id
            where m.id is null and p.is_deleted = false
            order by po.id
            """)).all()
    details = [f"option_id={r[0]} product_id={r[1]} sku_code={r[2]}" for r in rows[:50]]
    if len(rows) > 50:
        details.append(f"... 외 {len(rows) - 50}건 더")
    level = "WARNING" if rows else "PASS"
    return CheckResult(
        "PlatformMap 없는 ProductOption",
        level,
        len(rows),
        details,
        note=(
            "수동검토필요 - groupProductNo 기반 자동 병합 대상이 될 수 없는 사각지대(known duplicate 후보). "
            "다음 네이버 상품 동기화 시 같은 물리 상품이 새 ProductOption으로 다시 생성될 수 있으므로 "
            "상품관리 화면에서 어느 옵션과 동일 상품인지 사람이 확인 후 수동 병합 필요"
        ),
    )


def check_orphan_platform_map(db) -> CheckResult:
    """존재하지 않는 product_option_id를 참조하는 platform_map 행 - FK 제약상
    구조적으로 불가능해야 하나, 데이터 이관/수동 SQL 실수 방지를 위해 점검한다."""
    rows = db.execute(text("""
            select m.id, m.product_option_id
            from product_platform_map m
            left join product_options po on po.id = m.product_option_id
            where po.id is null
            """)).all()
    details = [f"map_id={r[0]} 참조하는 product_option_id={r[1]} (존재하지 않음)" for r in rows]
    level = "ERROR" if rows else "PASS"
    return CheckResult("ProductOption을 참조하지 않는 PlatformMap(고아 행)", level, len(rows), details)


CHECKS = [
    check_duplicate_platform_option_id,
    check_split_group_products,
    check_deleted_product_with_options,
    check_legacy_self_referential_map,
    check_auto_sku,
    check_option_without_platform_map,
    check_orphan_platform_map,
]


def main() -> int:
    print("=" * 70)
    print("상품 데이터 정합성 검증 (verify_product_integrity)")
    print("=" * 70)

    results: list[CheckResult] = []
    with session_scope() as db:
        for check in CHECKS:
            results.append(check(db))

    for r in results:
        print(f"\n[{r.level}] {r.name} - 이상 {r.count}건")
        if r.note:
            print(f"    분류: {r.note}")
        for line in r.details[:20]:
            print(f"    - {line}")
        if len(r.details) > 20:
            print(f"    ... 외 {len(r.details) - 20}건 더 (위 요약 참고)")

    error_count = sum(1 for r in results if r.level == "ERROR")
    warning_count = sum(1 for r in results if r.level == "WARNING")
    pass_count = sum(1 for r in results if r.level == "PASS")

    print("\n" + "=" * 70)
    print(f"검사 항목: {len(results)}개 | PASS {pass_count} | WARNING {warning_count} | ERROR {error_count}")

    if error_count > 0:
        print("최종 결과: ERROR - 즉시 조사가 필요한 구조적 문제가 있습니다. (Exit Code 1)")
        print("수정 권장: 위 ERROR 항목의 map_id/product_id를 그룹별로 조사한 뒤,")
        print("          이번 병합 작업(그룹1~6)과 동일하게 '조회 -> 옵션 이동 -> 브라우저 확인")
        print("          -> Soft Delete -> Legacy Map 삭제 -> 최종 검증' 절차로 처리하세요.")
        print("=" * 70)
        return 1
    if warning_count > 0:
        print("최종 결과: ERROR 없음, WARNING만 존재 - 배포/운영 계속 진행 가능. (Exit Code 0)")
        print("=" * 70)
        return 0
    print("최종 결과: PASS - 이상 없음. (Exit Code 0)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
