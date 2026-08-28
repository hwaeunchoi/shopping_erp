"""
scripts/rotate_credential_key.py
------------------------------------
CREDENTIAL_ENCRYPTION_KEY 회전(재암호화) 도구.

기본은 dry-run(분석/검증만). 실제 재암호화는 --execute 플래그가 있을 때만
수행한다. 구키/신키는 CLI 인자로 직접 받지 않고 "그 값이 담긴 환경변수의
이름"만 받아 os.environ에서 읽는다(쉘 히스토리·프로세스 목록에 실제 값이
남지 않도록 하기 위함).

절차(방식 1 - 선 재암호화, 후 컨테이너 재기동. DEPLOYMENT.md 참고):
  1) 구키/신키 안전 기준 검증(core.crypto.validate_production_secret) 및
     구키!=신키 확인
  2) api_credentials 전체 조회(PostgreSQL에서는 SELECT ... FOR UPDATE로 행 잠금)
  3) 구키로 전 레코드 복호화 - 하나라도 실패하면 아무 것도 갱신하지 않고 중단
  4) dry-run(기본)이면 "N건 검증 통과"만 출력하고 종료(커밋 없음)
  5) --execute: 신키로 재암호화해 세션에 반영(아직 flush만, commit 전)
  6) commit 전에 신키로 방금 반영한 값을 다시 복호화해 원래 평문과 완전히
     일치하는지 재검증 - 실패하면 rollback하고 중단
  7) 전부 성공한 경우에만 commit, 처리 건수만 출력

core.crypto.build_fernet()이 앱과 공유하는 유일한 키 파생 함수다(raw secret
-> SHA-256 -> urlsafe base64 -> Fernet 키). 이 스크립트는 절대로
Fernet(raw_secret)을 직접 호출하지 않는다 - 그러면 기존 암호문과 호환이
깨진다. 마찬가지로 core.crypto.encrypt_value/decrypt_value(앱 전역 캐시된
_fernet(), 즉 현재 settings.credential_encryption_key 하나만 아는 함수)도
쓰지 않는다 - 이 스크립트는 구키·신키 두 개를 동시에 다뤄야 하기 때문이다.

실행 예:
    python scripts/rotate_credential_key.py \\
        --old-key-env OLD_CREDENTIAL_ENCRYPTION_KEY \\
        --new-key-env NEW_CREDENTIAL_ENCRYPTION_KEY
    python scripts/rotate_credential_key.py \\
        --old-key-env OLD_CREDENTIAL_ENCRYPTION_KEY \\
        --new-key-env NEW_CREDENTIAL_ENCRYPTION_KEY --execute

레거시(안전하지 않은) 구키에서 벗어나는 일회성 마이그레이션:
  기본 동작은 구키가 알려진 공개 데모 기본값(예: CHANGE_ME_IN_PRODUCTION)이면
  거부한다. 하지만 바로 그 데모 기본값에서 벗어나는 것이 이 도구의 존재
  이유인 경우(실제로 지금까지 그 값이 운영 키로 쓰이고 있었던 경우)가 있어,
  --allow-legacy-insecure-old-key를 명시했을 때만 "구키"에 한해 그 데모
  기본값 거부만 우회한다:
    python scripts/rotate_credential_key.py \\
        --old-key-env OLD_CREDENTIAL_ENCRYPTION_KEY \\
        --new-key-env NEW_CREDENTIAL_ENCRYPTION_KEY \\
        --allow-legacy-insecure-old-key --execute
  이 옵션이 있어도 신키(new key)는 여전히 기존 운영 안전 기준(데모 기본값
  거부·최소 길이 등)을 100% 그대로 통과해야 하고, 구키의 누락/빈 값 검증과
  "구키==신키" 거부, "구키로 전 레코드 복호화 안 되면 즉시 중단(DB 무변경)"
  가드도 전부 그대로 유지된다 - 오직 "구키가 공개 데모 기본값이라는 이유
  하나"만 우회한다. 다른 이유로 안전하지 않은 구키(너무 짧음 등)는 이
  옵션으로도 통과되지 않는다.

SQLite(단위 테스트) vs PostgreSQL(운영) 차이:
  - with_for_update()는 PostgreSQL에서만 실제로 행 잠금을 건다. SQLite는
    FOR UPDATE 구문을 지원하지 않아 SQLAlchemy가 조용히 생략한다(오류는
    나지 않는다) - 그래서 단위 테스트는 "잠금이 동시쓰기를 막는지"가 아니라
    "전체 복호화 검증 -> 재암호화 -> 재검증 -> commit/rollback" 로직 자체를
    검증하는 용도로만 SQLite를 쓴다.
  - 운영에서 실제 동시쓰기 방어가 필요하면 반드시 PostgreSQL 위에서 실행해야
    한다. 다만 이 저장소의 최종 운영 정책은 "회전 중 api/scheduler를 모두
    정지해 애초에 동시 쓰기 자체가 없게 한다"이므로(DEPLOYMENT.md 참고),
    행 잠금은 그 위에 얹는 추가 방어선일 뿐 유일한 방어수단은 아니다.
  - 운영 실행 전 `pg_dump -Fc`로 DB 백업을 반드시 먼저 받아야 한다(이 스크립트는
    백업을 대신하지 않는다).
"""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from core.crypto import InsecureSecretError, build_fernet, validate_production_secret  # noqa: E402
from core.database import SessionLocal  # noqa: E402
from models.system import ApiCredential  # noqa: E402


class RotationAborted(Exception):
    """재암호화를 중단해야 할 때 던진다. 메시지에 평문·암호문·키를 절대 담지 않는다."""


@dataclass
class RotationResult:
    total: int
    executed: bool


def rotate_credentials(
    db: Session, old_secret: str, new_secret: str, execute: bool, allow_legacy_insecure_old_key: bool = False
) -> RotationResult:
    """전체 api_credentials를 old_secret 기준으로 검증한 뒤 execute=True면
    new_secret으로 재암호화한다.

    이 함수는 commit/rollback을 스스로 결정하지 않는다 - 호출자가 트랜잭션
    경계를 정한다(테스트에서 커밋 여부를 직접 확인할 수 있도록 순수 함수로
    분리했다). 정상 반환(RotationAborted를 던지지 않음)했을 때만 호출자가
    commit해야 하며, dry-run(execute=False)일 때는 세션에 아무 변경도 만들지
    않는다(조회·메모리상 복호화 시도만 수행).

    allow_legacy_insecure_old_key=True는 구키에 한해서만 "공개 데모 기본값"
    거부를 우회한다(모듈 docstring 참고) - new key 검증에는 절대 전달하지
    않는다."""
    validate_production_secret("old key", old_secret, allow_demo_default=allow_legacy_insecure_old_key)
    validate_production_secret("new key", new_secret)
    if old_secret == new_secret:
        raise RotationAborted("구키와 신키가 동일합니다.")

    old_fernet = build_fernet(old_secret)
    new_fernet = build_fernet(new_secret)

    credentials = db.query(ApiCredential).order_by(ApiCredential.id).with_for_update().all()
    total = len(credentials)

    # 1차 검증: 구키로 전 레코드 복호화. 하나라도 실패하면 아무 것도 건드리지 않는다.
    plaintexts: dict[int, str] = {}
    for cred in credentials:
        try:
            plaintexts[cred.id] = old_fernet.decrypt(cred.key_value_encrypted.encode("utf-8")).decode("utf-8")
        except Exception as e:
            raise RotationAborted(
                f"구키로 복호화 실패(owner_type={cred.owner_type}, owner_id={cred.owner_id}, "
                f"key_name={cred.key_name}) - 아무 것도 변경하지 않았습니다."
            ) from e

    if not execute:
        return RotationResult(total=total, executed=False)

    # 신키로 재암호화해 세션에 반영(flush만, commit은 호출자 책임).
    for cred in credentials:
        cred.key_value_encrypted = new_fernet.encrypt(plaintexts[cred.id].encode("utf-8")).decode("utf-8")
    db.flush()

    # 2차 검증: 방금 반영한 신키 암호문을 다시 복호화해 원래 평문과 완전히 같은지 확인.
    for cred in credentials:
        try:
            reencrypted_plain = new_fernet.decrypt(cred.key_value_encrypted.encode("utf-8")).decode("utf-8")
        except Exception as e:
            raise RotationAborted(
                f"신키 재검증 실패(owner_type={cred.owner_type}, owner_id={cred.owner_id}, "
                f"key_name={cred.key_name}) - 롤백이 필요합니다."
            ) from e
        if reencrypted_plain != plaintexts[cred.id]:
            raise RotationAborted(
                f"신키 재검증 불일치(owner_type={cred.owner_type}, owner_id={cred.owner_id}, "
                f"key_name={cred.key_name}) - 롤백이 필요합니다."
            )

    return RotationResult(total=total, executed=True)


def _read_key_from_env(env_var_name: str) -> str:
    value = os.environ.get(env_var_name)
    if not value:
        raise InsecureSecretError(f"환경변수 {env_var_name}가 설정되어 있지 않습니다.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="CREDENTIAL_ENCRYPTION_KEY 회전(재암호화, 기본 dry-run)")
    parser.add_argument("--old-key-env", required=True, help="구키 값이 담긴 환경변수 이름")
    parser.add_argument("--new-key-env", required=True, help="신키 값이 담긴 환경변수 이름")
    parser.add_argument("--execute", action="store_true", help="실제 재암호화 수행(미지정 시 dry-run)")
    parser.add_argument(
        "--allow-legacy-insecure-old-key",
        action="store_true",
        help=(
            "구키가 공개 데모 기본값(예: CHANGE_ME_IN_PRODUCTION)이라도 진행을 허용한다 - "
            "안전하지 않은 기존 키에서 벗어나는 일회성 마이그레이션 전용 옵션이며, "
            "신키(new key)는 이 옵션과 무관하게 항상 기존 운영 안전 기준을 그대로 통과해야 한다."
        ),
    )
    args = parser.parse_args()

    try:
        old_secret = _read_key_from_env(args.old_key_env)
        new_secret = _read_key_from_env(args.new_key_env)
    except InsecureSecretError as e:
        print(f"[중단] {e}")
        return 1

    db = SessionLocal()
    try:
        try:
            result = rotate_credentials(
                db,
                old_secret,
                new_secret,
                execute=args.execute,
                allow_legacy_insecure_old_key=args.allow_legacy_insecure_old_key,
            )
        except (RotationAborted, InsecureSecretError) as e:
            db.rollback()
            print(f"[중단] {e}")
            return 1

        if not result.executed:
            db.rollback()  # dry-run은 조회·복호화만 했으므로 되돌릴 변경 자체가 없지만, 명시적으로 커밋하지 않는다.
            print(f"[DRY-RUN] 총 {result.total}건 - 구키 복호화 검증 통과. --execute로 실제 재암호화를 수행하세요.")
            return 0

        db.commit()
        print(f"[완료] 총 {result.total}건 재암호화 및 재검증 완료.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
