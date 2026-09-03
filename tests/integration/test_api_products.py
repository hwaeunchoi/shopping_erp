"""
tests/integration/test_api_products.py
----------------------------------------------
api/routers/products.py 통합 테스트: 상품/옵션 CRUD, 플랫폼매핑, 원가이력, RBAC.
"""


class TestProductCRUD:
    def test_create_get_update_product(self, client, auth_headers):
        created = client.post(
            "/api/products",
            json={"name": "통합테스트 상품", "category": "잡화", "base_price": 9900},
            headers=auth_headers,
        )
        assert created.status_code == 201
        product_id = created.json()["id"]

        got = client.get(f"/api/products/{product_id}", headers=auth_headers)
        assert got.status_code == 200
        assert got.json()["name"] == "통합테스트 상품"

        updated = client.patch(f"/api/products/{product_id}", json={"status": "DISCONTINUED"}, headers=auth_headers)
        assert updated.status_code == 200
        assert updated.json()["status"] == "DISCONTINUED"

    def test_update_missing_product_returns_404(self, client, auth_headers):
        resp = client.patch("/api/products/999999", json={"name": "x"}, headers=auth_headers)
        assert resp.status_code == 404

    def test_requires_authentication(self, client, seed_data):
        resp = client.get("/api/products")
        assert resp.status_code == 401

    def test_delete_product_removes_it_from_list_and_detail(self, client, auth_headers):
        created = client.post("/api/products", json={"name": "삭제될 상품"}, headers=auth_headers)
        product_id = created.json()["id"]

        deleted = client.delete(f"/api/products/{product_id}", headers=auth_headers)
        assert deleted.status_code == 204

        got = client.get(f"/api/products/{product_id}", headers=auth_headers)
        assert got.status_code == 404
        listed = client.get("/api/products", headers=auth_headers)
        assert product_id not in [p["id"] for p in listed.json()]

    def test_delete_missing_product_returns_404(self, client, auth_headers):
        resp = client.delete("/api/products/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_include_deleted_shows_soft_deleted_products(self, client, auth_headers):
        created = client.post("/api/products", json={"name": "삭제후조회 상품"}, headers=auth_headers)
        product_id = created.json()["id"]
        client.delete(f"/api/products/{product_id}", headers=auth_headers)

        default_list = client.get("/api/products", headers=auth_headers)
        assert product_id not in [p["id"] for p in default_list.json()]

        with_deleted = client.get("/api/products?include_deleted=true", headers=auth_headers)
        matches = [p for p in with_deleted.json() if p["id"] == product_id]
        assert len(matches) == 1
        assert matches[0]["is_deleted"] is True

    def test_restore_product_makes_it_visible_again(self, client, auth_headers):
        created = client.post("/api/products", json={"name": "복원될 상품"}, headers=auth_headers)
        product_id = created.json()["id"]
        client.delete(f"/api/products/{product_id}", headers=auth_headers)
        assert client.get(f"/api/products/{product_id}", headers=auth_headers).status_code == 404

        restored = client.post(f"/api/products/{product_id}/restore", headers=auth_headers)

        assert restored.status_code == 200
        assert restored.json()["is_deleted"] is False
        assert client.get(f"/api/products/{product_id}", headers=auth_headers).status_code == 200

    def test_duplicate_product_creates_new_product_with_copied_options(self, client, auth_headers):
        product = client.post(
            "/api/products", json={"name": "복제원본", "category": "잡화", "brand": "브랜드A"}, headers=auth_headers
        ).json()
        client.post(f"/api/products/{product['id']}/options", json={"sku_code": "DUP-API-SKU-1"}, headers=auth_headers)

        duplicated = client.post(f"/api/products/{product['id']}/duplicate", headers=auth_headers)

        assert duplicated.status_code == 201
        assert duplicated.json()["id"] != product["id"]
        assert duplicated.json()["name"] == "복제원본 (복사본)"
        assert duplicated.json()["brand"] == "브랜드A"
        dup_options = client.get(f"/api/products/{duplicated.json()['id']}/options", headers=auth_headers).json()
        assert len(dup_options) == 1
        assert dup_options[0]["sku_code"] != "DUP-API-SKU-1"


class TestProductDetail:
    def test_detail_includes_options_maps_images_and_stats(self, client, auth_headers, api_session_factory, seed_data):
        """상품 상세 화면에 필요한 기본정보/옵션/플랫폼매핑/이미지/통계를 한 번의
        호출로 모두 받을 수 있는지 검증한다."""
        from datetime import datetime, timezone

        from models.inventory import Inventory
        from models.order import Order, OrderItem

        product = client.post(
            "/api/products", json={"name": "상세조회 테스트 상품", "category": "잡화"}, headers=auth_headers
        ).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "DETAIL-SKU-001"}, headers=auth_headers
        ).json()
        client.post(
            f"/api/products/options/{option['id']}/platform-map",
            json={"platform_id": seed_data["platform_id"], "platform_option_id": "DETAIL-EXT-001"},
            headers=auth_headers,
        )
        client.post(
            f"/api/products/{product['id']}/images",
            json={"image_url": "https://img.example.com/detail.jpg"},
            headers=auth_headers,
        )
        client.post(
            f"/api/products/options/{option['id']}/cost-history",
            json={"cost_price": 4000, "effective_from": "2026-01-01T00:00:00Z"},
            headers=auth_headers,
        )

        db = api_session_factory()
        try:
            db.add(
                Inventory(
                    product_option_id=option["id"],
                    warehouse_id=seed_data["warehouse_id"],
                    sellable_stock=42,
                    reserved_stock=0,
                    safety_stock=5,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            order = Order(
                platform_id=seed_data["platform_id"],
                platform_order_no="ORD-DETAIL-STATS-1",
                status="DELIVERED",
                order_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
                total_amount=8000,
                discount_amount=0,
            )
            db.add(order)
            db.flush()
            db.add(
                OrderItem(
                    order_id=order.id, product_option_id=option["id"], quantity=3, unit_price=4000, line_amount=12000
                )
            )
            db.commit()
        finally:
            db.close()

        detail = client.get(f"/api/products/{product['id']}/detail", headers=auth_headers)

        assert detail.status_code == 200
        body = detail.json()
        assert body["name"] == "상세조회 테스트 상품"
        assert len(body["options"]) == 1
        option_detail = body["options"][0]
        assert option_detail["sku_code"] == "DETAIL-SKU-001"
        assert len(option_detail["platform_maps"]) == 1
        assert option_detail["platform_maps"][0]["platform_option_id"] == "DETAIL-EXT-001"
        assert option_detail["stats"]["total_quantity_sold"] == 3
        assert option_detail["stats"]["last_order_date"] is not None
        assert option_detail["stats"]["current_cost_price"] == 4000.0
        assert option_detail["stats"]["sellable_stock"] == 42
        assert len(body["images"]) == 1
        assert body["images"][0]["image_url"] == "https://img.example.com/detail.jpg"

    def test_detail_exposes_sibling_mapping_ids_sharing_origin_product(
        self, client, auth_headers, api_session_factory, seed_data
    ):
        """네이버는 원상품 하나에 스마트스토어/윈도우 등 복수 채널상품이 연결될 수
        있어, 서로 다른 ProductPlatformMap 행이 같은 platform_origin_product_id를
        공유할 수 있다(유니크 제약 없음 - platform_option_id와 다름). 이 경우
        상품상세 화면이 그 공유 관계를 sibling_mapping_ids로 알 수 있어야
        판매상태 변경이 다른 매핑에도 영향을 준다는 것을 화면에 보여줄 수 있다."""
        from models.product import ProductPlatformMap

        product = client.post(
            "/api/products", json={"name": "형제매핑 테스트 상품", "category": "잡화"}, headers=auth_headers
        ).json()
        option_a = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "SIBLING-SKU-A"}, headers=auth_headers
        ).json()
        option_b = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "SIBLING-SKU-B"}, headers=auth_headers
        ).json()
        mapping_a = client.post(
            f"/api/products/options/{option_a['id']}/platform-map",
            json={"platform_id": seed_data["platform_id"], "platform_option_id": "SIBLING-EXT-A"},
            headers=auth_headers,
        ).json()
        mapping_b = client.post(
            f"/api/products/options/{option_b['id']}/platform-map",
            json={"platform_id": seed_data["platform_id"], "platform_option_id": "SIBLING-EXT-B"},
            headers=auth_headers,
        ).json()

        db = api_session_factory()
        try:
            for mid in (mapping_a["id"], mapping_b["id"]):
                m = db.get(ProductPlatformMap, mid)
                m.platform_origin_product_id = "ORIGIN-SHARED-1"
            db.commit()
        finally:
            db.close()

        detail = client.get(f"/api/products/{product['id']}/detail", headers=auth_headers).json()
        maps_by_option = {o["sku_code"]: o["platform_maps"][0] for o in detail["options"]}

        assert maps_by_option["SIBLING-SKU-A"]["sibling_mapping_ids"] == [mapping_b["id"]]
        assert maps_by_option["SIBLING-SKU-B"]["sibling_mapping_ids"] == [mapping_a["id"]]

    def test_detail_missing_product_returns_404(self, client, auth_headers):
        resp = client.get("/api/products/999999/detail", headers=auth_headers)
        assert resp.status_code == 404


class TestOptionCRUD:
    def test_create_update_and_toggle_active(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "옵션테스트 상품"}, headers=auth_headers).json()

        created = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "INT-SKU-001", "color": "블랙"},
            headers=auth_headers,
        )
        assert created.status_code == 201
        option_id = created.json()["id"]
        assert created.json()["is_active"] is True

        updated = client.patch(f"/api/products/options/{option_id}", json={"size": "L"}, headers=auth_headers)
        assert updated.status_code == 200
        assert updated.json()["size"] == "L"

        deactivated = client.patch(
            f"/api/products/options/{option_id}/active", json={"is_active": False}, headers=auth_headers
        )
        assert deactivated.status_code == 200
        assert deactivated.json()["is_active"] is False

    def test_create_and_update_unit_cost_price(self, client, auth_headers):
        """단가(매입원가)는 이력 관리 없이 현재값만 등록/수정할 수 있다."""
        product = client.post("/api/products", json={"name": "단가테스트 상품"}, headers=auth_headers).json()

        created = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "COST-SKU-001", "unit_cost_price": 5000},
            headers=auth_headers,
        )
        assert created.status_code == 201
        assert created.json()["unit_cost_price"] == 5000.0

        updated = client.patch(
            f"/api/products/options/{created.json()['id']}", json={"unit_cost_price": 5500}, headers=auth_headers
        )
        assert updated.status_code == 200
        assert updated.json()["unit_cost_price"] == 5500.0

    def test_create_option_unknown_product_returns_404(self, client, auth_headers):
        resp = client.post("/api/products/999999/options", json={"sku_code": "X"}, headers=auth_headers)
        assert resp.status_code == 404

    def test_duplicate_sku_returns_409(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "중복SKU 상품"}, headers=auth_headers).json()
        client.post(f"/api/products/{product['id']}/options", json={"sku_code": "DUP-SKU"}, headers=auth_headers)

        resp = client.post(f"/api/products/{product['id']}/options", json={"sku_code": "DUP-SKU"}, headers=auth_headers)

        assert resp.status_code == 409

    def test_delete_option_without_orders_succeeds(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "옵션삭제 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "DELETE-API-SKU-1"}, headers=auth_headers
        ).json()

        resp = client.delete(f"/api/products/options/{option['id']}", headers=auth_headers)

        assert resp.status_code == 204
        remaining = client.get(f"/api/products/{product['id']}/options", headers=auth_headers).json()
        assert remaining == []

    def test_update_missing_option_returns_404(self, client, auth_headers):
        resp = client.patch("/api/products/options/999999", json={"size": "L"}, headers=auth_headers)
        assert resp.status_code == 404

    def test_update_option_missing_fields_keep_existing_values(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "부분수정 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "PARTIAL-SKU-001", "option_name": "블랙", "color": "레드", "unit_cost_price": 3000},
            headers=auth_headers,
        ).json()

        updated = client.patch(f"/api/products/options/{option['id']}", json={"size": "L"}, headers=auth_headers)

        assert updated.status_code == 200
        body = updated.json()
        assert body["size"] == "L"
        assert body["option_name"] == "블랙"  # 요청 본문에 없던 필드는 그대로 유지된다
        assert body["color"] == "레드"
        assert body["unit_cost_price"] == 3000.0
        assert body["sale_price"] is None

    def test_update_option_explicit_null_clears_field(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "NULL삭제 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "NULL-SKU-001", "option_name": "블랙", "barcode": "8801234567890"},
            headers=auth_headers,
        ).json()

        updated = client.patch(
            f"/api/products/options/{option['id']}", json={"option_name": None, "barcode": None}, headers=auth_headers
        )

        assert updated.status_code == 200
        body = updated.json()
        assert body["option_name"] is None
        assert body["barcode"] is None

    def test_update_option_blank_string_normalizes_to_null(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "공백정규화 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "BLANK-SKU-001", "option_name": "블랙", "color": "레드"},
            headers=auth_headers,
        ).json()

        updated = client.patch(
            f"/api/products/options/{option['id']}", json={"option_name": "", "color": "   "}, headers=auth_headers
        )

        assert updated.status_code == 200
        body = updated.json()
        assert body["option_name"] is None
        assert body["color"] is None

    def test_update_option_name_255_chars_succeeds(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "255자 성공 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "LEN255-SKU-001"}, headers=auth_headers
        ).json()

        updated = client.patch(
            f"/api/products/options/{option['id']}", json={"option_name": "A" * 255}, headers=auth_headers
        )

        assert updated.status_code == 200
        assert updated.json()["option_name"] == "A" * 255
        assert len(updated.json()["option_name"]) == 255

    def test_update_option_name_255_korean_chars_succeeds(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "한글255 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "LEN255-KR-SKU-001"}, headers=auth_headers
        ).json()

        updated = client.patch(
            f"/api/products/options/{option['id']}", json={"option_name": "가" * 255}, headers=auth_headers
        )

        assert updated.status_code == 200
        assert updated.json()["option_name"] == "가" * 255
        assert len(updated.json()["option_name"]) == 255

    def test_update_option_name_256_chars_returns_422_with_string_detail(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "256자 거부 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "LEN256-SKU-001"}, headers=auth_headers
        ).json()

        resp = client.patch(
            f"/api/products/options/{option['id']}", json={"option_name": "B" * 256}, headers=auth_headers
        )

        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], str)
        unchanged = client.get(f"/api/products/{product['id']}/options", headers=auth_headers).json()
        assert unchanged[0]["option_name"] is None  # 거부된 요청은 저장되지 않는다

    def test_update_option_negative_unit_cost_price_returns_422_with_string_detail(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "음수원가 거부 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "NEG-SKU-001", "unit_cost_price": 3000},
            headers=auth_headers,
        ).json()

        resp = client.patch(f"/api/products/options/{option['id']}", json={"unit_cost_price": -1}, headers=auth_headers)

        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], str)
        unchanged = client.get(f"/api/products/{product['id']}/options", headers=auth_headers).json()
        assert unchanged[0]["unit_cost_price"] == 3000.0  # 거부된 요청은 저장되지 않는다

    def test_update_option_zero_unit_cost_price_succeeds(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "원가0 성공 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "ZERO-SKU-001", "unit_cost_price": 3000},
            headers=auth_headers,
        ).json()

        updated = client.patch(
            f"/api/products/options/{option['id']}", json={"unit_cost_price": 0}, headers=auth_headers
        )

        assert updated.status_code == 200
        assert updated.json()["unit_cost_price"] == 0.0

    def test_update_option_barcode_set_and_clear(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "바코드 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "BARCODE-SKU-001"}, headers=auth_headers
        ).json()

        set_resp = client.patch(
            f"/api/products/options/{option['id']}", json={"barcode": "8801234567890"}, headers=auth_headers
        )
        assert set_resp.status_code == 200
        assert set_resp.json()["barcode"] == "8801234567890"

        clear_resp = client.patch(f"/api/products/options/{option['id']}", json={"barcode": None}, headers=auth_headers)
        assert clear_resp.status_code == 200
        assert clear_resp.json()["barcode"] is None

    def test_update_option_response_matches_persisted_value(self, client, auth_headers):
        """응답 body가 실제 DB 저장값과 일치하는지 - PATCH 응답과 별도 GET 조회 결과를 비교한다."""
        product = client.post("/api/products", json={"name": "응답일치 테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "SYNC-SKU-001"}, headers=auth_headers
        ).json()

        patch_resp = client.patch(
            f"/api/products/options/{option['id']}",
            json={"option_name": "블랙-수정", "color": "블랙"},
            headers=auth_headers,
        )
        refetched = client.get(f"/api/products/{product['id']}/options", headers=auth_headers).json()
        refetched_option = next(o for o in refetched if o["id"] == option["id"])

        assert patch_resp.json()["option_name"] == refetched_option["option_name"] == "블랙-수정"
        assert patch_resp.json()["color"] == refetched_option["color"] == "블랙"

    def test_create_option_name_255_chars_succeeds(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "등록255 성공 테스트 상품"}, headers=auth_headers).json()

        resp = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "CREATE-LEN255-SKU-001", "option_name": "C" * 255},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        assert resp.json()["option_name"] == "C" * 255
        assert len(resp.json()["option_name"]) == 255

    def test_create_option_name_256_chars_returns_422(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "등록256 거부 테스트 상품"}, headers=auth_headers).json()

        resp = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "CREATE-LEN256-SKU-001", "option_name": "D" * 256},
            headers=auth_headers,
        )

        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], str)

    def test_create_option_zero_unit_cost_price_succeeds(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "등록원가0 테스트 상품"}, headers=auth_headers).json()

        resp = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "CREATE-ZERO-SKU-001", "unit_cost_price": 0},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        assert resp.json()["unit_cost_price"] == 0.0

    def test_create_option_negative_unit_cost_price_returns_422(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "등록음수원가 테스트 상품"}, headers=auth_headers).json()

        resp = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "CREATE-NEG-SKU-001", "unit_cost_price": -500},
            headers=auth_headers,
        )

        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], str)

    def test_create_option_blank_string_normalizes_to_null(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "등록공백정규화 테스트 상품"}, headers=auth_headers).json()

        resp = client.post(
            f"/api/products/{product['id']}/options",
            json={"sku_code": "CREATE-BLANK-SKU-001", "option_name": "   ", "color": ""},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        assert resp.json()["option_name"] is None
        assert resp.json()["color"] is None

    def test_delete_missing_option_returns_404(self, client, auth_headers):
        resp = client.delete("/api/products/options/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_reorder_options_changes_display_order(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "순서변경 테스트 상품"}, headers=auth_headers).json()
        first = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "ORDER-API-SKU-1"}, headers=auth_headers
        ).json()
        second = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "ORDER-API-SKU-2"}, headers=auth_headers
        ).json()

        resp = client.patch(
            f"/api/products/{product['id']}/options/reorder",
            json={"option_ids": [second["id"], first["id"]]},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert [o["id"] for o in resp.json()] == [second["id"], first["id"]]


class TestPlatformMap:
    def test_create_list_and_delete(self, client, auth_headers, seed_data):
        product = client.post("/api/products", json={"name": "매핑테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "MAP-SKU-001"}, headers=auth_headers
        ).json()

        created = client.post(
            f"/api/products/options/{option['id']}/platform-map",
            json={"platform_id": seed_data["platform_id"], "platform_option_id": "EXT-001"},
            headers=auth_headers,
        )
        assert created.status_code == 201
        mapping_id = created.json()["id"]

        listed = client.get(f"/api/products/options/{option['id']}/platform-map", headers=auth_headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        deleted = client.delete(f"/api/products/platform-map/{mapping_id}", headers=auth_headers)
        assert deleted.status_code == 204

        listed_after = client.get(f"/api/products/options/{option['id']}/platform-map", headers=auth_headers)
        assert listed_after.json() == []

    def test_create_stores_display_name_and_seller_product_code(self, client, auth_headers, seed_data):
        """쇼핑몰 노출상품명/판매자상품코드는 SKU-플랫폼 매핑 단위로 저장된다(플랫폼마다 다를 수 있음)."""
        product = client.post("/api/products", json={"name": "노출명테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "DISPLAY-SKU-001"}, headers=auth_headers
        ).json()

        created = client.post(
            f"/api/products/options/{option['id']}/platform-map",
            json={
                "platform_id": seed_data["platform_id"],
                "platform_option_id": "EXT-DISPLAY-001",
                "display_name": "[특가] 테스트 상품 블랙",
                "seller_product_code": "SELLER-001",
            },
            headers=auth_headers,
        )

        assert created.status_code == 201
        assert created.json()["display_name"] == "[특가] 테스트 상품 블랙"
        assert created.json()["seller_product_code"] == "SELLER-001"

    def test_duplicate_mapping_returns_409(self, client, auth_headers, seed_data):
        product = client.post("/api/products", json={"name": "매핑중복 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "MAP-SKU-002"}, headers=auth_headers
        ).json()
        payload = {"platform_id": seed_data["platform_id"], "platform_option_id": "EXT-DUP"}
        client.post(f"/api/products/options/{option['id']}/platform-map", json=payload, headers=auth_headers)

        resp = client.post(f"/api/products/options/{option['id']}/platform-map", json=payload, headers=auth_headers)

        assert resp.status_code == 409

    def test_delete_missing_mapping_returns_404(self, client, auth_headers):
        resp = client.delete("/api/products/platform-map/999999", headers=auth_headers)
        assert resp.status_code == 404

    def test_update_mapping_changes_display_fields(self, client, auth_headers, seed_data):
        product = client.post("/api/products", json={"name": "매핑수정 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "MAP-UPDATE-SKU-001"}, headers=auth_headers
        ).json()
        created = client.post(
            f"/api/products/options/{option['id']}/platform-map",
            json={"platform_id": seed_data["platform_id"], "platform_option_id": "EXT-UPDATE-001"},
            headers=auth_headers,
        ).json()

        updated = client.patch(
            f"/api/products/platform-map/{created['id']}",
            json={"display_name": "수정된 노출명", "platform_product_id": "PRODNO-999"},
            headers=auth_headers,
        )

        assert updated.status_code == 200
        assert updated.json()["display_name"] == "수정된 노출명"
        assert updated.json()["platform_product_id"] == "PRODNO-999"

    def test_update_missing_mapping_returns_404(self, client, auth_headers):
        resp = client.patch("/api/products/platform-map/999999", json={"display_name": "x"}, headers=auth_headers)
        assert resp.status_code == 404


class TestCostHistory:
    def test_add_cost_closes_previous_and_lists_history(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "원가테스트 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "COST-SKU-001"}, headers=auth_headers
        ).json()

        first = client.post(
            f"/api/products/options/{option['id']}/cost-history",
            json={"cost_price": 1000, "effective_from": "2026-01-01T00:00:00Z"},
            headers=auth_headers,
        )
        assert first.status_code == 201
        assert first.json()["effective_to"] is None

        second = client.post(
            f"/api/products/options/{option['id']}/cost-history",
            json={"cost_price": 1200, "effective_from": "2026-03-01T00:00:00Z"},
            headers=auth_headers,
        )
        assert second.status_code == 201

        history = client.get(f"/api/products/options/{option['id']}/cost-history", headers=auth_headers)
        assert history.status_code == 200
        body = history.json()
        assert len(body) == 2
        assert body[0]["cost_price"] == 1200  # 최신순
        assert body[1]["effective_to"] is not None  # 첫 레코드는 닫혔어야 함

    def test_add_cost_unknown_option_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/products/options/999999/cost-history",
            json={"cost_price": 1000, "effective_from": "2026-01-01T00:00:00Z"},
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestProductImages:
    def test_create_list_and_thumbnail_flow(self, client, auth_headers):
        product = client.post("/api/products", json={"name": "이미지테스트 상품"}, headers=auth_headers).json()

        first = client.post(
            f"/api/products/{product['id']}/images",
            json={"image_url": "https://img.example.com/1.jpg"},
            headers=auth_headers,
        )
        assert first.status_code == 201
        assert first.json()["is_thumbnail"] is True

        second = client.post(
            f"/api/products/{product['id']}/images",
            json={"image_url": "https://img.example.com/2.jpg"},
            headers=auth_headers,
        )
        assert second.status_code == 201
        assert second.json()["is_thumbnail"] is False

        listed = client.get(f"/api/products/{product['id']}/images", headers=auth_headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 2

        set_thumb = client.patch(f"/api/products/images/{second.json()['id']}/thumbnail", headers=auth_headers)
        assert set_thumb.status_code == 200
        assert set_thumb.json()["is_thumbnail"] is True

        deleted = client.delete(f"/api/products/images/{first.json()['id']}", headers=auth_headers)
        assert deleted.status_code == 204
        remaining = client.get(f"/api/products/{product['id']}/images", headers=auth_headers).json()
        assert len(remaining) == 1
        assert remaining[0]["id"] == second.json()["id"]

    def test_create_image_unknown_product_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/products/999999/images", json={"image_url": "https://img.example.com/x.jpg"}, headers=auth_headers
        )
        assert resp.status_code == 404

    def test_delete_missing_image_returns_404(self, client, auth_headers):
        resp = client.delete("/api/products/images/999999", headers=auth_headers)
        assert resp.status_code == 404


class TestUnmatchedItems:
    def test_list_and_resolve_unmatched_item(self, client, auth_headers, api_session_factory, seed_data):
        """/unmatched-items가 /{product_id}와 경로 충돌 없이 정상 라우팅되는지도 함께 검증한다."""
        from models.product import UnmatchedPlatformItem

        db = api_session_factory()
        try:
            db.add(
                UnmatchedPlatformItem(
                    platform_id=seed_data["platform_id"],
                    platform_order_no="ORD-UNMATCHED-API-1",
                    platform_option_id="COUPANG-API-CODE-1",
                    product_name="미매칭 API 테스트 상품",
                    seller_product_code="SELLER-API-1",
                    quantity=1,
                    unit_price=1000,
                )
            )
            db.commit()
        finally:
            db.close()

        listed = client.get("/api/products/unmatched-items", headers=auth_headers)
        assert listed.status_code == 200
        matches = [i for i in listed.json() if i["platform_option_id"] == "COUPANG-API-CODE-1"]
        assert len(matches) == 1
        item_id = matches[0]["id"]

        product = client.post("/api/products", json={"name": "미매칭 연결 대상 상품"}, headers=auth_headers).json()
        option = client.post(
            f"/api/products/{product['id']}/options", json={"sku_code": "UNMATCHED-TARGET-SKU-1"}, headers=auth_headers
        ).json()

        resolved = client.post(
            f"/api/products/unmatched-items/{item_id}/resolve",
            json={"product_option_id": option["id"]},
            headers=auth_headers,
        )
        assert resolved.status_code == 200
        assert resolved.json()["product_option_id"] == option["id"]
        assert resolved.json()["platform_option_id"] == "COUPANG-API-CODE-1"

        listed_after = client.get("/api/products/unmatched-items", headers=auth_headers)
        assert item_id not in [i["id"] for i in listed_after.json()]

    def test_resolve_missing_unmatched_item_returns_404(self, client, auth_headers):
        resp = client.post(
            "/api/products/unmatched-items/999999/resolve", json={"product_option_id": 1}, headers=auth_headers
        )
        assert resp.status_code == 404


class TestNaverProductSync:
    def test_sync_from_naver_without_credentials_returns_409(self, client, auth_headers, api_session_factory):
        """실 API 키가 없으면 더미 상품으로 위장하지 않고 연결정보 오류(409)를 준다."""
        from models.platform import Platform

        db = api_session_factory()
        try:
            naver_platform = Platform(
                code="naver_smartstore",
                name="네이버 스마트스토어",
                connector_class="NaverSmartstoreConnector",
                settlement_cycle_days=7,
                is_active=True,
            )
            db.add(naver_platform)
            db.commit()
            naver_platform_id = naver_platform.id
        finally:
            db.close()

        resp = client.post(
            "/api/products/sync-from-naver", json={"platform_id": naver_platform_id}, headers=auth_headers
        )

        assert resp.status_code == 409
        assert resp.json()["error_code"] == "CREDENTIAL_MISSING"

    def test_sync_from_naver_unknown_platform_returns_404(self, client, auth_headers):
        resp = client.post("/api/products/sync-from-naver", json={"platform_id": 999999}, headers=auth_headers)
        assert resp.status_code == 404


class TestRBAC:
    def test_requires_product_manage_permission(self, client, api_session_factory, seed_data):
        from core.security import hash_password
        from models.user import Permission, Role, RolePermission, User

        db = api_session_factory()
        try:
            role = Role(name="NoProduct")
            db.add(role)
            db.flush()
            other_perm = db.query(Permission).filter_by(code="ORDER_VIEW").first()
            db.add(RolePermission(role_id=role.id, permission_id=other_perm.id))
            db.add(
                User(
                    username="noproduct",
                    password_hash=hash_password("pw123456"),
                    name="무권한",
                    role_id=role.id,
                    is_active=True,
                )
            )
            db.commit()
        finally:
            db.close()

        login = client.post("/api/auth/login", data={"username": "noproduct", "password": "pw123456"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        resp = client.post("/api/products", json={"name": "x"}, headers=headers)
        assert resp.status_code == 403
