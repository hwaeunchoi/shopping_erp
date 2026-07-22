"""scheduler/jobs 패키지: 개별 배치 작업 정의.

각 모듈은 인자 없는 run() 함수 하나를 노출한다. run()은 자체적으로
core.database.session_scope()로 세션을 열고 커밋까지 책임지므로,
스케줄러(scheduler/scheduler.py)는 run()을 그대로 호출하기만 하면 된다.

- order_collect_job: 쇼핑몰 커넥터로 신규/변경 주문 수집 (OrderSyncService)
- ad_collect_job: 광고 플랫폼 캠페인/일별 성과 수집
- settlement_sync_job: 커넥터의 정산 내역으로 settlements 확인/갱신
- profit_calculation_job: 최근 N일 매출/손익 요약 재계산 (ProfitCalculationService)
- customer_stats_job: 전체 고객 캐시 통계 재계산 (CustomerStatsService)
- backup_job: DB 파일 백업 + 보관정책 적용
"""
