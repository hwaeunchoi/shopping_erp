"""
scripts/merge_split_product_groups.py
------------------------------------------
2026-07-07 스키마 오류로 생성된 AUTO 잔여 Product와, 2026-07-08 재설계 이후
정상 동기화가 새로 만든 Product가 같은 groupProductNo(platform_product_id)를
공유한 채 둘로 쪼개져 있는 그룹을 자동으로 병합한다.

이 스크립트가 하는 일은 그룹1~6을 수동으로 처리했던 절차(조회 -> 옵션 이동
-> Soft Delete -> Legacy Map 삭제 -> 검증)를 코드로 재현한 것이며, 그 이상도
이하도 하지 않는다:

1. 같은 (platform_id, platform_product_id)를 가진 옵션들이 소프트 삭제되지
   않은 서로 다른 Product에 걸쳐 있는 그룹을 찾는다.
2. 각 그룹에서 "유지(KEEP)" Product를 정한다 - 옵션을 더 많이 보유한 쪽을
   우선하고, 동률이면 더 최근에 생성된 쪽을 선택한다.
3. 아래 조건을 모두 만족해야만 "자동 병합 가능"으로 분류한다(하나라도
   어긋나면 자동 처리하지 않고 "수동 검토 필요"로만 분류):
   - 그룹의 Product가 정확히 2개
   - "제거될(LOSING)" Product가 이 그룹 말고 다른 groupProductNo의 옵션을
     함께 들고 있지 않음(즉 이 그룹 전용 껍데기)
   - KEEP/LOSING 선택이 모호하지 않음(옵션 수 동률 + 생성시각까지 동일한
     경우는 제외)
4. 자동 병합 가능한 그룹만 다음을 수행한다:
   - LOSING 쪽 옵션을 KEEP Product로 이동(product_options.product_id만 변경,
     product_option_id/PK는 절대 변경하지 않는다 -> order_items 영향 없음)
   - 이동한 옵션에 legacy 자기참조 매핑(platform_option_id ==
     platform_product_id, seller_product_code 공란)이 있으면 그 행만 id
     기준으로 삭제한다(신규 실데이터 매핑이 반드시 존재할 때만)
   - LOSING Product가 옵션 이동 후 완전히 비었을 때만 is_deleted=true 처리
     (다른 그룹의 옵션을 더 갖고 있다면 삭제하지 않는다)

멱등성: 이미 병합된 그룹(이제 Product가 1개뿐인 그룹)은 애초에 대상 목록에
잡히지 않으므로, 이 스크립트를 여러 번 실행해도 안전하다.

실행:
    python scripts/merge_split_product_groups.py --dry-run   (기본값, DB 미변경)
    python scripts/merge_split_product_groups.py --execute    (실제 반영)

각 그룹은 개별 SAVEPOINT(nested transaction)로 처리한다 - 한 그룹 처리 중
예외가 발생하면 그 그룹만 롤백하고 즉시 전체 실행을 중단한다(뒤에 남은
그룹은 처리하지 않는다 - 부분 실행 상태에서 계속 진행하지 않기 위함).
"""

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core.database import session_scope  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("merge_split_product_groups")


@dataclass
class OptionRow:
    option_id: int
    product_id: int
    sku_code: str
    map_ids: list[int] = field(default_factory=list)
    legacy_map_ids: list[int] = field(
        default_factory=list
    )  # 자기참조(platform_option_id==platform_product_id, seller_product_code 공란)


@dataclass
class GroupPlan:
    platform_id: int
    platform_product_id: str
    keep_product_id: int
    losing_product_id: int
    options_to_move: list[OptionRow]
    legacy_map_ids_to_delete: list[int]
    losing_product_fully_emptied: bool
    unsafe_reason: str | None = None

    @property
    def is_safe(self) -> bool:
        return self.unsafe_reason is None


def find_split_groups(db: Session) -> list[tuple[int, str]]:
    rows = db.execute(text("""
            select m.platform_id, m.platform_product_id
            from product_platform_map m
            join product_options po on po.id = m.product_option_id
            join products p on p.id = po.product_id
            where m.platform_product_id is not null and p.is_deleted = false
            group by m.platform_id, m.platform_product_id
            having count(distinct po.product_id) > 1
            order by m.platform_id, m.platform_product_id
            """)).all()
    return [(r[0], r[1]) for r in rows]


def build_plan(db: Session, platform_id: int, platform_product_id: str) -> GroupPlan | None:
    # 이 그룹에 속한 (product_id, option_id, sku_code, created_at) 전체
    rows = db.execute(
        text("""
            select po.product_id, p.created_at, po.id, po.sku_code
            from product_platform_map m
            join product_options po on po.id = m.product_option_id
            join products p on p.id = po.product_id
            where m.platform_id = :platform_id and m.platform_product_id = :ppid and p.is_deleted = false
            """),
        {"platform_id": platform_id, "ppid": platform_product_id},
    ).all()

    products: dict[int, dict] = {}
    for product_id, created_at, option_id, _sku_code in rows:
        products.setdefault(product_id, {"created_at": created_at, "option_ids": set()})
        products[product_id]["option_ids"].add(option_id)

    if len(products) != 2:
        return GroupPlan(
            platform_id,
            platform_product_id,
            0,
            0,
            [],
            [],
            False,
            unsafe_reason=f"Product가 {len(products)}개(2개가 아님) - 자동 병합 대상 아님",
        )

    (pid_a, info_a), (pid_b, info_b) = sorted(products.items())
    count_a, count_b = len(info_a["option_ids"]), len(info_b["option_ids"])

    if count_a > count_b:
        keep_id, losing_id = pid_a, pid_b
    elif count_b > count_a:
        keep_id, losing_id = pid_b, pid_a
    elif info_a["created_at"] != info_b["created_at"]:
        keep_id, losing_id = (pid_a, pid_b) if info_a["created_at"] > info_b["created_at"] else (pid_b, pid_a)
    else:
        return GroupPlan(
            platform_id,
            platform_product_id,
            0,
            0,
            [],
            [],
            False,
            unsafe_reason="옵션 수와 생성시각이 완전히 동률이라 KEEP 판단 불가 - 수동 검토 필요",
        )

    losing_option_ids = products[losing_id]["option_ids"]

    # losing 쪽이 이 그룹 말고 다른 groupProductNo의 옵션도 갖고 있는지 확인
    other_group_check = db.execute(
        text("""
            select count(*) from product_options po
            left join product_platform_map m
              on m.product_option_id = po.id and m.platform_id = :platform_id and m.platform_product_id = :ppid
            where po.product_id = :losing_id and m.id is null
            """),
        {"platform_id": platform_id, "ppid": platform_product_id, "losing_id": losing_id},
    ).scalar_one()
    if other_group_check > 0:
        return GroupPlan(
            platform_id,
            platform_product_id,
            keep_id,
            losing_id,
            [],
            [],
            False,
            unsafe_reason=(
                f"LOSING product_id={losing_id}가 이 그룹 외 다른 옵션도 보유 - "
                f"소프트 삭제하면 그 옵션까지 사라지므로 수동 검토 필요"
            ),
        )

    # 이동 대상 옵션별 platform_map(정상/legacy) 상세
    # 원칙: 어떤 옵션의 legacy 자기참조 매핑도, "그 옵션에 삭제 후에도 최소 1개의
    # 정상(비-legacy) platform_map이 남는다"는 것이 SQL로 확인될 때만 삭제 대상에
    # 넣는다. 옵션이 legacy 행 하나만 가진 경우(정상 매핑이 전혀 없는 경우) 그
    # 옵션의 legacy 행은 절대 삭제하지 않는다 - 지우면 그 옵션이 플랫폼과 완전히
    # 연결이 끊겨 다음 동기화 때 중복 옵션이 새로 생기는 원인이 된다(2026-07-08
    # 대량 병합 때 실제로 발생했던 버그, 사후 복구함).
    options_to_move: list[OptionRow] = []
    legacy_map_ids_to_delete: list[int] = []
    for option_id in sorted(losing_option_ids):
        map_rows = db.execute(
            text("""
                select id, platform_option_id, platform_product_id, seller_product_code
                from product_platform_map where product_option_id = :oid
                """),
            {"oid": option_id},
        ).all()
        sku_code = db.execute(
            text("select sku_code from product_options where id = :oid"), {"oid": option_id}
        ).scalar_one()
        opt = OptionRow(option_id=option_id, product_id=losing_id, sku_code=sku_code)

        legacy_ids_for_option: list[int] = []
        real_ids_for_option: list[int] = []
        for map_id, poid, ppid, seller_code in map_rows:
            opt.map_ids.append(map_id)
            is_legacy = (poid == ppid) and (seller_code is None or seller_code == "")
            if is_legacy:
                legacy_ids_for_option.append(map_id)
            else:
                real_ids_for_option.append(map_id)

        if legacy_ids_for_option and real_ids_for_option:
            # 삭제 후에도 real_ids_for_option이 최소 1개 남는 것이 이미 확인됨 -> 삭제 대상에 포함
            opt.legacy_map_ids.extend(legacy_ids_for_option)
            legacy_map_ids_to_delete.extend(legacy_ids_for_option)
        # legacy_ids_for_option만 있고 real_ids_for_option이 없으면(정상 매핑이 전혀
        # 없는 옵션) 아무것도 삭제하지 않는다 - opt.legacy_map_ids는 빈 채로 둔다.

        options_to_move.append(opt)

    return GroupPlan(
        platform_id=platform_id,
        platform_product_id=platform_product_id,
        keep_product_id=keep_id,
        losing_product_id=losing_id,
        options_to_move=options_to_move,
        legacy_map_ids_to_delete=legacy_map_ids_to_delete,
        losing_product_fully_emptied=True,  # other_group_check==0이 위에서 이미 보장
    )


def apply_plan(db: Session, plan: GroupPlan) -> None:
    option_ids = [o.option_id for o in plan.options_to_move]
    if option_ids:
        db.execute(
            text("UPDATE product_options SET product_id = :keep_id WHERE id = ANY(:option_ids)"),
            {"keep_id": plan.keep_product_id, "option_ids": option_ids},
        )
    if plan.legacy_map_ids_to_delete:
        db.execute(
            text("DELETE FROM product_platform_map WHERE id = ANY(:map_ids)"),
            {"map_ids": plan.legacy_map_ids_to_delete},
        )
    if plan.losing_product_fully_emptied:
        db.execute(
            text("UPDATE products SET is_deleted = true WHERE id = :losing_id"), {"losing_id": plan.losing_product_id}
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="DB를 변경하지 않고 계획만 출력한다(기본값)")
    mode.add_argument("--execute", action="store_true", help="실제로 UPDATE/DELETE를 실행한다")
    args = parser.parse_args()
    dry_run = not args.execute

    logger.info("=" * 70)
    logger.info("groupProductNo 분리 그룹 병합 - %s", "DRY-RUN(미실행)" if dry_run else "실제 실행")
    logger.info("=" * 70)

    with session_scope() as db:
        groups = find_split_groups(db)
        logger.info("분리된 그룹 총 %d개 발견", len(groups))

        safe_plans: list[GroupPlan] = []
        unsafe_plans: list[GroupPlan] = []

        for platform_id, platform_product_id in groups:
            plan = build_plan(db, platform_id, platform_product_id)
            if plan is None:
                continue
            if plan.is_safe:
                safe_plans.append(plan)
            else:
                unsafe_plans.append(plan)

        total_options_moved = sum(len(p.options_to_move) for p in safe_plans)
        total_legacy_maps_deleted = sum(len(p.legacy_map_ids_to_delete) for p in safe_plans)
        total_products_soft_deleted = sum(1 for p in safe_plans if p.losing_product_fully_emptied)
        all_moved_option_ids = [o.option_id for p in safe_plans for o in p.options_to_move]
        total_order_items_affected = (
            db.execute(
                text("select count(*) from order_items where product_option_id = ANY(:ids)"),
                {"ids": all_moved_option_ids},
            ).scalar_one()
            if all_moved_option_ids
            else 0
        )

        logger.info("")
        logger.info("자동 병합 가능(SAFE): %d개 그룹", len(safe_plans))
        logger.info("수동 검토 필요(UNSAFE): %d개 그룹", len(unsafe_plans))
        logger.info("")
        logger.info("[SAFE 그룹 집계]")
        logger.info("  이동될 ProductOption 수: %d", total_options_moved)
        logger.info("  Soft Delete될 Product 수: %d", total_products_soft_deleted)
        logger.info("  삭제될 legacy platform_map 수: %d", total_legacy_maps_deleted)
        logger.info("  영향받는(옵션ID 불변, product_id만 이동) order_item 수: %d", total_order_items_affected)
        logger.info("")
        logger.info("[UNSAFE 그룹 사유별 목록]")
        for p in unsafe_plans:
            logger.info(
                "  platform_id=%s platform_product_id=%s keep=%s losing=%s -> %s",
                p.platform_id,
                p.platform_product_id,
                p.keep_product_id or "-",
                p.losing_product_id or "-",
                p.unsafe_reason,
            )

        if dry_run:
            logger.info("")
            logger.info("DRY-RUN이므로 DB를 변경하지 않았습니다. --execute로 재실행하면 SAFE 그룹만 반영됩니다.")
            return 0

        logger.info("")
        logger.info("실제 반영을 시작합니다 (SAFE 그룹 %d개, 그룹당 SAVEPOINT)...", len(safe_plans))
        processed = 0
        for plan in safe_plans:
            try:
                with db.begin_nested():
                    apply_plan(db, plan)
                processed += 1
                logger.info(
                    "  [OK] platform_product_id=%s keep=%s losing=%s (옵션 %d개 이동, legacy map %d개 삭제)",
                    plan.platform_product_id,
                    plan.keep_product_id,
                    plan.losing_product_id,
                    len(plan.options_to_move),
                    len(plan.legacy_map_ids_to_delete),
                )
            except Exception:
                logger.exception(
                    "  [FAIL] platform_product_id=%s 처리 중 예외 발생 - 이 그룹만 롤백하고 실행을 중단합니다.",
                    plan.platform_product_id,
                )
                logger.info("지금까지 정상 처리된 그룹 %d개는 유지됩니다(각 그룹이 독립 SAVEPOINT).", processed)
                return 2
        logger.info("")
        logger.info("완료: %d개 그룹 병합 반영됨.", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
