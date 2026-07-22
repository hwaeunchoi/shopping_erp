"""
integrations/malls/coupang_connector.py
-------------------------------------------
쿠팡 더미 커넥터 (Platform.connector_class == "CoupangConnector"와 매핑).

쿠팡 오픈API의 원본 응답 형식(더미)은 네이버와 필드명이 다르다는 점을
보여주기 위해 별도로 흉내 낸다 (설계 원칙은 base_mall_connector.py 참고).
"""

from datetime import date, datetime
from typing import Any

from integrations.malls.base_mall_connector import BaseMallConnector

_STATUS_TO_RAW = {
    "NEW": "ACCEPT",
    "PREPARING": "INSTRUCT",
    "SHIPPING": "DEPARTURE",
    "DELIVERED": "FINAL_DELIVERY",
    "CANCELED": "CANCEL",
}
_RAW_TO_STATUS = {raw: std for std, raw in _STATUS_TO_RAW.items()}


class CoupangConnector(BaseMallConnector):
    platform_code = "coupang"

    def fetch_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        raw_orders = self._fetch_raw_orders(start_date, end_date)
        return [self._normalize(raw) for raw in raw_orders]

    def fetch_order_detail(self, platform_order_no: str) -> dict[str, Any]:
        return self._dummy_single_order(platform_order_no)

    def update_shipment(self, platform_order_no: str, carrier: str, tracking_no: str) -> bool:
        return True

    def fetch_settlements(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return self._dummy_settlements(start_date, end_date, cycle_days=15)

    def _fetch_raw_orders(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """쿠팡 오픈API 원본 응답 형식(더미)을 흉내 낸다."""
        raw_orders = []
        for seed in self._dummy_seed_orders(start_date, end_date):
            raw_orders.append(
                {
                    "orderId": f"C{seed['order_date']:%Y%m%d}{seed['seq']:04d}",
                    "orderedAt": seed["order_date"].isoformat(),
                    "status": _STATUS_TO_RAW[seed["status"]],
                    "receiverName": seed["customer_name"],
                    "receiverPhoneMasked": seed["customer_phone"],
                    "buyerId": seed["customer_key"],
                    "vendorItemCode": seed["product_code"],
                    "shippingCount": seed["quantity"],
                    "salesPrice": seed["unit_price"],
                }
            )
        return raw_orders

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        total_amount = round(raw["salesPrice"] * raw["shippingCount"], 2)
        return {
            "platform_order_no": raw["orderId"],
            "order_date": datetime.fromisoformat(raw["orderedAt"]),
            "status": _RAW_TO_STATUS[raw["status"]],
            "customer_key": raw["buyerId"],
            "customer_name": raw["receiverName"],
            "customer_phone": raw["receiverPhoneMasked"],
            "total_amount": total_amount,
            "discount_amount": 0.0,
            "items": [
                {
                    "platform_option_id": raw["vendorItemCode"],
                    "quantity": raw["shippingCount"],
                    "unit_price": raw["salesPrice"],
                }
            ],
        }
