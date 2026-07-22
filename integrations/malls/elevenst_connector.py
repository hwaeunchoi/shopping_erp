"""
integrations/malls/elevenst_connector.py
----------------------------------------------
11번가 더미 커넥터 (Platform.connector_class == "ElevenstConnector"와 매핑).
"""

from datetime import date, datetime
from typing import Any

from integrations.malls.base_mall_connector import BaseMallConnector

_STATUS_TO_RAW = {
    "NEW": "결제완료",
    "PREPARING": "상품준비중",
    "SHIPPING": "배송중",
    "DELIVERED": "구매확정",
    "CANCELED": "주문취소",
}
_RAW_TO_STATUS = {raw: std for std, raw in _STATUS_TO_RAW.items()}


class ElevenstConnector(BaseMallConnector):
    platform_code = "elevenst"

    def fetch_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        raw_orders = self._fetch_raw_orders(start_date, end_date)
        return [self._normalize(raw) for raw in raw_orders]

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        return self._dummy_single_order(platform_order_no)

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        return True

    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return self._dummy_settlements(start_date, end_date, cycle_days=14)

    def _fetch_raw_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """11번가 오픈API 원본 응답 형식(더미)을 흉내 낸다."""
        raw_orders = []
        for seed in self._dummy_seed_orders(start_date, end_date):
            raw_orders.append(
                {
                    "ordNo": f"S{seed['order_date']:%Y%m%d}{seed['seq']:04d}",
                    "ordDt": seed["order_date"].isoformat(),
                    "ordStatNm": _STATUS_TO_RAW[seed["status"]],
                    "byerId": seed["customer_key"],
                    "ordNm": seed["customer_name"],
                    "ordTelno": seed["customer_phone"],
                    "prdNo": seed["product_code"],
                    "ordQty": seed["quantity"],
                    "ordAmt": seed["unit_price"],
                }
            )
        return raw_orders

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        total_amount = round(raw["ordAmt"] * raw["ordQty"], 2)
        return {
            "platform_order_no": raw["ordNo"],
            "order_date": datetime.fromisoformat(raw["ordDt"]),
            "status": _RAW_TO_STATUS[raw["ordStatNm"]],
            "customer_key": raw["byerId"],
            "customer_name": raw["ordNm"],
            "customer_phone": raw["ordTelno"],
            "total_amount": total_amount,
            "discount_amount": 0.0,
            "items": [{"platform_option_id": raw["prdNo"], "quantity": raw["ordQty"], "unit_price": raw["ordAmt"]}],
        }
