"""
tests/unit/test_settings_service.py
-----------------------------------------
설정(Settings) 화면 Service 계층 단위 테스트: 사용자/역할/권한/
API Credential(암호화)/시스템설정.
"""

from models.user import Permission, Role
from services.settings_service import ApiCredentialService, RoleService, SystemSettingService, UserManagementService


class TestUserManagementService:
    def test_create_user_hashes_password(self, db_session):
        role = Role(name="Viewer")
        db_session.add(role)
        db_session.flush()

        service = UserManagementService(db_session)
        user = service.create_user(username="tester", password="plain-pw-123", name="테스터", role_id=role.id)

        assert user.password_hash != "plain-pw-123"
        assert user in service.list_users()

    def test_update_user_changes_role_and_deactivates(self, db_session):
        role_a = Role(name="Viewer")
        role_b = Role(name="Manager")
        db_session.add_all([role_a, role_b])
        db_session.flush()
        service = UserManagementService(db_session)
        user = service.create_user(username="u2", password="pw", name="사용자2", role_id=role_a.id)

        updated = service.update_user(user.id, role_id=role_b.id, is_active=False)

        assert updated is not None
        assert updated.role_id == role_b.id
        assert updated.is_active is False

    def test_update_user_returns_none_for_missing_id(self, db_session):
        service = UserManagementService(db_session)
        assert service.update_user(999999) is None


class TestRoleService:
    def test_create_role_and_set_permissions(self, db_session):
        db_session.add_all(
            [Permission(code="ORDER_VIEW", name="주문 조회"), Permission(code="AD_MANAGE", name="광고 관리")]
        )
        db_session.flush()
        service = RoleService(db_session)
        role = service.create_role("Custom", "커스텀 역할")

        codes = service.set_role_permissions(role.id, ["ORDER_VIEW", "AD_MANAGE"])

        assert set(codes) == {"ORDER_VIEW", "AD_MANAGE"}
        assert set(service.get_role_permission_codes(role.id)) == {"ORDER_VIEW", "AD_MANAGE"}

    def test_set_role_permissions_replaces_not_appends(self, db_session):
        db_session.add_all(
            [Permission(code="ORDER_VIEW", name="주문 조회"), Permission(code="AD_MANAGE", name="광고 관리")]
        )
        db_session.flush()
        service = RoleService(db_session)
        role = service.create_role("Custom2")
        service.set_role_permissions(role.id, ["ORDER_VIEW", "AD_MANAGE"])

        codes = service.set_role_permissions(role.id, ["ORDER_VIEW"])

        assert codes == ["ORDER_VIEW"]

    def test_set_role_permissions_ignores_unknown_codes(self, db_session):
        db_session.add(Permission(code="ORDER_VIEW", name="주문 조회"))
        db_session.flush()
        service = RoleService(db_session)
        role = service.create_role("Custom3")

        codes = service.set_role_permissions(role.id, ["ORDER_VIEW", "NOT_A_REAL_CODE"])

        assert codes == ["ORDER_VIEW"]


class TestApiCredentialService:
    def test_upsert_stores_encrypted_value_not_plaintext(self, db_session):
        service = ApiCredentialService(db_session)

        credential = service.upsert_credential(
            owner_type="PLATFORM", owner_id=1, key_name="client_secret", plain_value="my-plain-secret"
        )

        assert credential.key_value_encrypted != "my-plain-secret"

    def test_list_masked_never_exposes_full_plaintext(self, db_session):
        service = ApiCredentialService(db_session)
        service.upsert_credential(
            owner_type="PLATFORM", owner_id=1, key_name="client_secret", plain_value="my-plain-secret"
        )

        items = service.list_masked(owner_type="PLATFORM", owner_id=1)

        assert len(items) == 1
        assert "my-plain-secret" not in items[0]["masked_value"]
        assert items[0]["masked_value"].endswith("cret")

    def test_upsert_same_owner_and_key_updates_instead_of_duplicating(self, db_session):
        service = ApiCredentialService(db_session)
        service.upsert_credential(owner_type="PLATFORM", owner_id=1, key_name="client_secret", plain_value="first")
        service.upsert_credential(owner_type="PLATFORM", owner_id=1, key_name="client_secret", plain_value="second")

        items = service.list_masked(owner_type="PLATFORM", owner_id=1)

        assert len(items) == 1
        assert items[0]["masked_value"].endswith("cond")

    def test_delete_credential(self, db_session):
        service = ApiCredentialService(db_session)
        credential = service.upsert_credential(
            owner_type="PLATFORM", owner_id=2, key_name="client_id", plain_value="abc"
        )

        assert service.delete_credential(credential.id) is True
        assert service.list_masked(owner_type="PLATFORM", owner_id=2) == []

    def test_delete_credential_returns_false_for_missing_id(self, db_session):
        service = ApiCredentialService(db_session)
        assert service.delete_credential(999999) is False


class TestSystemSettingService:
    def test_upsert_creates_then_updates_same_key(self, db_session):
        service = SystemSettingService(db_session)
        first = service.upsert_setting("BACKUP", "retention_days", "30")
        second = service.upsert_setting("BACKUP", "retention_days", "60")

        assert first.id == second.id
        assert second.value == "60"

    def test_list_settings_filters_by_category(self, db_session):
        service = SystemSettingService(db_session)
        service.upsert_setting("BACKUP", "retention_days", "30")
        service.upsert_setting("REPORT", "default_format", "PDF")

        backup_only = service.list_settings("BACKUP")

        assert len(backup_only) == 1
        assert backup_only[0].category == "BACKUP"
