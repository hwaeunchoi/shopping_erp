"""
integrations/malls/kakao_shopping_connector.py
-----------------------------------------------------
카카오쇼핑 더미 커넥터 (Platform.connector_class == "KakaoShoppingConnector"와 매핑).
"""

from datetime import date, datetime
from typing import Any

from integrations.malls.base_mall_connector import BaseMallConnector

_STATUS_TO_RAW = {
    "NEW": "paid",
    "PREPARING": "ready",
    "SHIPPING": "shipping",
    "DELIVERED": "delivered",
    "CANCELED": "canceled",
}
_RAW_TO_STATUS = {raw: std for std, raw in _STATUS_TO_RAW.items()}


class KakaoShoppingConnector(BaseMallConnector):
    platform_code = "kakao_shopping"

    def fetch_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        raw_orders = self._fetch_raw_orders(start_date, end_date)
        return [self._normalize(raw) for raw in raw_orders]

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        return self._dummy_single_order(platform_order_no)

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        return True

    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return self._dummy_settlements(start_date, end_date, cycle_days=7)

    def _fetch_raw_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """카카오쇼핑 API 원본 응답 형식(더미)을 흉내 낸다."""
        raw_orders = []
        for seed in self._dummy_seed_orders(start_date, end_date):
            raw_orders.append(
                {
                    "order_id": f"K{seed['order_date']:%Y%m%d}{seed['seq']:04d}",
                    "ordered_at": seed["order_date"].isoformat(),
                    "status": _STATUS_TO_RAW[seed["status"]],
                    "buyer_uuid": seed["customer_key"],
                    "buyer_name": seed["customer_name"],
                    "buyer_phone": seed["customer_phone"],
                    "item_id": seed["product_code"],
                    "amount": seed["quantity"],
                    "unit_price": seed["unit_price"],
                }
            )
        return raw_orders

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        total_amount = round(raw["unit_price"] * raw["amount"], 2)
        return {
            "platform_order_no": raw["order_id"],
            "order_date": datetime.fromisoformat(raw["ordered_at"]),
            "status": _RAW_TO_STATUS[raw["status"]],
            "customer_key": raw["buyer_uuid"],
            "customer_name": raw["buyer_name"],
            "customer_phone": raw["buyer_phone"],
            "total_amount": total_amount,
            "discount_amount": 0.0,
            "items": [
                {"platform_option_id": raw["item_id"], "quantity": raw["amount"], "unit_price": raw["unit_price"]}
            ],
        }
