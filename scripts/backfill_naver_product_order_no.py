"""
scripts/backfill_naver_product_order_no.py
------------------------------------------
네이버 스마트스토어의 과거 주문상품(OrderItem)에 대해, 상품주문번호
(platform_order_item_no, 네이버 productOrderId)가 비어(NULL) 있는 라인을
"확정 가능한 경우에만" 백필한다.

■ 안전 원칙 (반드시 준수)
- 기본은 dry-run(분석/보고만). 실제 기록은 --execute 플래그가 있을 때만 수행한다.
- 네이버 API는 읽기 전용(fetch_orders)만 호출한다. 상태변경 API(발주확인/송장/
  취소·교환·반품 처리 등)는 절대 호출하지 않는다.
- 기존에 값이 있는 라인은 절대 덮어쓰지 않는다(idempotent).
- 개인정보/전체 상품주문번호를 출력하지 않는다(상품주문번호는 마스킹).
- "확정 가능(모호하지 않음)"한 주문의 라인만 백필한다. 한 주문 안에서 같은
  SKU(product_option)가 2줄 이상이면 어떤 productOrderId가 어느 라인인지 알 수
  없으므로 그 주문의 해당 SKU 라인은 건드리지 않고 NULL로 남긴다.
- 실행 중 예외가 나면 트랜잭션 전체를 롤백한다(부분 반영 금지).

■ 매칭 규칙(주문 단위)
  네이버에서 다시 조회한 라인들을 (해석된 product_option_id) 기준으로 묶고,
  DB의 NULL 라인들도 같은 기준으로 묶는다. 어떤 product_option_id에 대해
  · 네이버 라인 1개 AND DB NULL 라인 1개  → 1:1 확정 → 그 productOrderId를 백필
  · 그 외(0개/2개 이상)                    → 모호 → 건너뜀(NULL 유지)
  이미 같은 주문에 그 productOrderId가 쓰였다면 충돌로 보고 건너뛴다.

실행 예시(프로젝트 루트):
    # 분석만(권장 첫 실행). 구간 미지정 시 기존 네이버 주문의 order_date 최소~최대 사용
    python scripts/backfill_naver_product_order_no.py
    python scripts/backfill_naver_product_order_no.py --start 2026-08-18 --end 2026-08-20
    # 실제 백필(확정 라인만 기록)
    python scripts/backfill_naver_product_order_no.py --start 2026-08-18 --end 2026-08-20 --execute
"""

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging  # noqa: E402
from core.database import session_scope  # noqa: E402
from integrations.malls import get_mall_connector  # noqa: E402
from models.order import Order, OrderItem  # noqa: E402
from models.platform import Platform  # noqa: E402
from repositories.product_repository import ProductPlatformMapRepository  # noqa: E402

NAVER_PLATFORM_CODE = "naver_smartstore"


def _mask(poin: str) -> str:
    """상품주문번호 마스킹: 끝 4자리만 노출(로그·리포트 노출 최소화)."""
    if not poin:
        return "(빈값)"
    tail = poin[-4:]
    return f"****{tail}"


def _resolve_window(db, platform_id, start_arg, end_arg):
    """--start/--end가 없으면 기존 네이버 주문의 order_date 최소~최대로 구간 결정."""
    if start_arg and end_arg:
        start = datetime.combine(start_arg, time.min, tzinfo=timezone.utc)
        end = datetime.combine(end_arg, time.max, tzinfo=timezone.utc)
        return start, end
    row = db.execute(
        select(func.min(Order.order_date), func.max(Order.order_date)).where(Order.platform_id == platform_id)
    ).one()
    if row[0] is None:
        return None, None
    return row[0], row[1]


def _iter_naver_lines(items):
    """정규화된 items에서 (platform_order_item_no, platform_option_id) 추출."""
    for it in items:
        yield it.get("platform_order_item_no"), it.get("platform_option_id")


def backfill(start_arg, end_arg, execute: bool) -> int:
    setup_logging()
    logger = logging.getLogger(__name__)
    with session_scope() as db:
        platform = db.execute(select(Platform).where(Platform.code == NAVER_PLATFORM_CODE)).scalar_one_or_none()
        if platform is None:
            print("[중단] 네이버 스마트스토어 플랫폼(code=naver_smartstore)이 없습니다.")
            return 2

        start, end = _resolve_window(db, platform.id, start_arg, end_arg)
        if start is None:
            print("[중단] 네이버 주문이 없어 조회 구간을 정할 수 없습니다.")
            return 2
        print(f"[정보] 조회 구간: {start.date()} ~ {end.date()} (네이버 읽기 전용)")

        # 1) 네이버에서 read-only 재조회 → 주문번호별 (poin, option_id) 라인 수집
        connector = get_mall_connector(platform.connector_class, session=db, platform_id=platform.id)
        try:
            raw_orders = connector.fetch_orders(start, end)
        except Exception as exc:  # noqa: BLE001 - PII 없이 오류 유형만 보고
            print(f"[중단] 네이버 주문 재조회 실패: {type(exc).__name__}")
            return 3
        naver_lines_by_order = defaultdict(list)
        for ro in raw_orders:
            ono = ro.get("platform_order_no")
            for poin, opt_id in _iter_naver_lines(ro.get("items", [])):
                naver_lines_by_order[ono].append((poin, opt_id))
        print(f"[정보] 네이버 재조회 주문 수={len(naver_lines_by_order)}")

        map_repo = ProductPlatformMapRepository(db)
        option_cache: dict[str, int | None] = {}

        def resolve_option(opt_id):
            if opt_id in option_cache:
                return option_cache[opt_id]
            m = map_repo.get_by_option_id(platform.id, opt_id) if opt_id else None
            option_cache[opt_id] = m.product_option_id if m else None
            return option_cache[opt_id]

        # 2) DB의 네이버 주문 중 NULL 라인이 있는 주문만 대상으로 분석.
        #    분석 대상은 재조회와 동일한 [start, end] 구간의 주문으로 한정한다
        #    (구간 밖의 과거/더미 주문을 백필 대상에서 배제하기 위함).
        orders = (
            db.execute(
                select(Order)
                .where(Order.platform_id == platform.id, Order.order_date >= start, Order.order_date <= end)
                .order_by(Order.id)
            )
            .scalars()
            .all()
        )

        stat = {
            "orders_target": 0,  # NULL 라인 보유 주문 수
            "items_null": 0,  # 검사한 NULL 라인 수
            "planned": 0,  # 백필 예정 라인 수
            "already_set": 0,  # 이미 값이 있는 라인 수
            "ambiguous": 0,  # 모호로 남긴 NULL 라인 수
            "conflict": 0,  # poin 충돌로 건너뛴 수
            "dup_naver": 0,  # 네이버 재조회 시 동일 option에 중복 라인
            "not_refetched": 0,  # 재조회 구간에 없어 매칭 불가한 주문 수
            "orders_clean": 0,  # 모든 NULL 라인이 백필 예정인 주문 수(확정)
            "orders_ambiguous": 0,  # 모호/미매칭 라인이 하나라도 있는 주문 수
        }
        planned_updates = []  # (OrderItem, poin)

        for order in orders:
            items = db.execute(select(OrderItem).where(OrderItem.order_id == order.id)).scalars().all()
            null_lines = [i for i in items if i.platform_order_item_no is None]
            stat["already_set"] += sum(1 for i in items if i.platform_order_item_no is not None)
            if not null_lines:
                continue
            stat["orders_target"] += 1
            stat["items_null"] += len(null_lines)

            naver_lines = naver_lines_by_order.get(order.platform_order_no)
            if not naver_lines:
                stat["not_refetched"] += 1
                stat["ambiguous"] += len(null_lines)
                stat["orders_ambiguous"] += 1
                continue
            order_ambiguous = False

            # option_id 기준 그룹핑(네이버/DB)
            naver_by_opt = defaultdict(list)
            for poin, opt_id in naver_lines:
                rid = resolve_option(opt_id)
                if rid is not None and poin:
                    naver_by_opt[rid].append(poin)
            db_null_by_opt = defaultdict(list)
            for line in null_lines:
                db_null_by_opt[line.product_option_id].append(line)
            existing_poins = {i.platform_order_item_no for i in items if i.platform_order_item_no is not None}

            for opt_id, lines in db_null_by_opt.items():
                poins = naver_by_opt.get(opt_id, [])
                if len(lines) == 1 and len(poins) == 1:
                    poin = poins[0]
                    if poin in existing_poins:
                        stat["conflict"] += 1
                        order_ambiguous = True
                        continue
                    planned_updates.append((lines[0], poin))
                    existing_poins.add(poin)
                    stat["planned"] += 1
                else:
                    if len(poins) > 1:
                        stat["dup_naver"] += 1
                    stat["ambiguous"] += len(lines)
                    order_ambiguous = True

            if order_ambiguous:
                stat["orders_ambiguous"] += 1
            else:
                stat["orders_clean"] += 1

        # 3) 보고
        print("\n===== 백필 분석 결과 (PII 없음, 상품주문번호 마스킹) =====")
        print(f"  대상 Order 수(NULL 라인 보유) : {stat['orders_target']}")
        print(f"    ├ 확정 주문(모두 백필가능)   : {stat['orders_clean']}")
        print(f"    └ 모호/미매칭 포함 주문      : {stat['orders_ambiguous']}")
        print(f"  검사한 OrderItem(NULL) 수      : {stat['items_null']}")
        print(f"  백필 예정 수                   : {stat['planned']}")
        print(f"  이미 값 있는 수                : {stat['already_set']}")
        print(f"  매칭 불가(모호) 수             : {stat['ambiguous']}")
        print(f"  충돌 수                        : {stat['conflict']}")
        print(f"  네이버 동일옵션 중복 수        : {stat['dup_naver']}")
        print(f"  재조회 구간 밖(매칭불가) 주문  : {stat['not_refetched']}")
        if planned_updates:
            print("\n  [백필 예정 상세(마스킹)]")
            for line, poin in planned_updates:
                print(f"    order_id={line.order_id} item_id={line.id} <- {_mask(poin)}")

        if not execute:
            print("\n[DRY-RUN] --execute 가 없으므로 아무것도 기록하지 않았습니다.")
            logger.info(
                "네이버 상품주문번호 백필 dry-run: 대상주문=%d NULL라인=%d 예정=%d 모호=%d 충돌=%d",
                stat["orders_target"],
                stat["items_null"],
                stat["planned"],
                stat["ambiguous"],
                stat["conflict"],
            )
            return 0

        if stat["conflict"] > 0:
            print("\n[중단] 충돌이 발견되어 실제 백필을 하지 않습니다(먼저 원인 점검 필요).")
            return 4
        if not planned_updates:
            print("\n[정보] 백필 대상이 없습니다. 변경 없이 종료합니다.")
            return 0

        # 4) 실제 기록(트랜잭션; 예외 시 session_scope가 롤백)
        for line, poin in planned_updates:
            if line.platform_order_item_no is None:  # 재확인(idempotent)
                line.platform_order_item_no = poin
        db.flush()
        print(f"\n[완료] {len(planned_updates)}건 백필했습니다.")
        logger.info("네이버 상품주문번호 백필 실행: 반영=%d", len(planned_updates))
        return 0


def _parse_date(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    parser = argparse.ArgumentParser(description="네이버 상품주문번호 백필(기본 dry-run)")
    parser.add_argument("--start", type=_parse_date, default=None, help="조회 시작일 YYYY-MM-DD")
    parser.add_argument("--end", type=_parse_date, default=None, help="조회 종료일 YYYY-MM-DD")
    parser.add_argument("--execute", action="store_true", help="실제 백필 수행(미지정 시 dry-run)")
    args = parser.parse_args()
    if bool(args.start) ^ bool(args.end):
        print("[중단] --start 와 --end 는 함께 지정해야 합니다.")
        return 2
    if args.execute and not (args.start and args.end):
        # 안전장치: 실제 백필은 반드시 명시적 구간에서만. 구간 자동확장으로
        # 과거/더미 주문까지 건드리는 사고를 방지한다.
        print("[중단] --execute 는 --start/--end 를 반드시 함께 지정해야 합니다(구간 자동확장 금지).")
        return 2
    return backfill(args.start, args.end, args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
