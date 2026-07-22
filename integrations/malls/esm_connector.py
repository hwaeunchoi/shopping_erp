"""
integrations/malls/esm_connector.py
----------------------------------------
ESM(G마켓/옥션) 더미 커넥터 (Platform.connector_class == "EsmConnector"와 매핑).
"""

from datetime import date, datetime
from typing import Any

from integrations.malls.base_mall_connector import BaseMallConnector

_STATUS_TO_RAW = {
    "NEW": "PayComplete",
    "PREPARING": "ShipReady",
    "SHIPPING": "ShipComplete",
    "DELIVERED": "TradeComplete",
    "CANCELED": "TradeCancel",
}
_RAW_TO_STATUS = {raw: std for std, raw in _STATUS_TO_RAW.items()}


class EsmConnector(BaseMallConnector):
    platform_code = "esm"

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
        """ESM(G마켓/옥션) API 원본 응답 형식(더미)을 흉내 낸다."""
        raw_orders = []
        for seed in self._dummy_seed_orders(start_date, end_date):
            raw_orders.append(
                {
                    "OrderNo": f"E{seed['order_date']:%Y%m%d}{seed['seq']:04d}",
                    "OrderDate": seed["order_date"].isoformat(),
                    "OrderStatus": _STATUS_TO_RAW[seed["status"]],
                    "BuyerId": seed["customer_key"],
                    "BuyerName": seed["customer_name"],
                    "BuyerTel": seed["customer_phone"],
                    "ItemCode": seed["product_code"],
                    "OrderQty": seed["quantity"],
                    "UnitPrice": seed["unit_price"],
                }
            )
        return raw_orders

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        total_amount = round(raw["UnitPrice"] * raw["OrderQty"], 2)
        return {
            "platform_order_no": raw["OrderNo"],
            "order_date": datetime.fromisoformat(raw["OrderDate"]),
            "status": _RAW_TO_STATUS[raw["OrderStatus"]],
            "customer_key": raw["BuyerId"],
            "customer_name": raw["BuyerName"],
            "customer_phone": raw["BuyerTel"],
            "total_amount": total_amount,
            "discount_amount": 0.0,
            "items": [
                {"platform_option_id": raw["ItemCode"], "quantity": raw["OrderQty"], "unit_price": raw["UnitPrice"]}
            ],
        }
