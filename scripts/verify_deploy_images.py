"""
scripts/verify_deploy_images.py
---------------------------------------
배포 직전/직후 api·scheduler·web 후보 이미지의 revision 라벨을 검증한다
(2026-09-11 API 크래시 루프 사고 - migration은 새 candidate 이미지로
실행했는데 api 컨테이너는 구 이미지로 재기동되어, 운영 DB가 이미 API
이미지의 migration 스크립트에 없는 revision에 있던 사고 - 재발 방지).

이 스크립트가 막는 실수:
  - api와 scheduler가 서로 다른 커밋의 candidate 이미지를 가리키는 실수
    (한쪽만 새로 빌드/배포하고 다른 쪽을 깜빡 잊는 경우 - 둘 다 같은
    Dockerfile/migration 스크립트를 담고 있어야 하므로 반드시 같은 커밋
    이어야 한다).
  - backend(api/scheduler) candidate 이미지가 실제로 지금 배포하려는 git
    커밋(--expected-backend-revision, 생략 시 HEAD)으로 빌드된 게 맞는지
    확인 없이 그냥 진행하는 실수.
  - --expected-web-revision을 함께 지정했는데 web 이미지가 없거나, 라벨이
    없거나, 그 커밋이 아닌 경우를 모른 채 배포하는 실수.

이 스크립트가 하지 않는 것:
  - 실제 배포(컨테이너 recreate)나 운영 DB 변경은 전혀 하지 않는다 - 로컬에
    존재하는 이미지의 라벨만 읽기 전용으로 조회한다(docker image inspect).
  - web은 api/scheduler와 독립적으로 배포될 수 있으므로(정적 파일 서빙만
    하고 DB migration과 무관) backend와 다른 커밋이어도 그 자체를 ERROR로
    취급하지 않는다 - --expected-web-revision을 명시적으로 지정했을 때만
    "그 값과 일치하는지"를 검증한다(생략하면 기존처럼 참고 출력만 한다).

실행 예(backend와 web을 서로 다른 커밋으로 배포하는 일반적인 경우 -
프론트 변경이 없어 web은 이전 커밋 이미지를 그대로 쓰는 상황):
    python scripts/verify_deploy_images.py \\
        --api-image shopping_erp_candidate/api:20260910_041113-b5d9485 \\
        --scheduler-image shopping_erp_candidate/scheduler:20260910_041113-b5d9485 \\
        --web-image shopping_erp_candidate/web:20260909_054605-434a3d0 \\
        --expected-backend-revision b5d94851b6c44a9e52feea3f3dd95351c9573d93 \\
        --expected-web-revision 434a3d040259a022504299440474b85a6fc8dd64
    echo $?
    # 0 = ERROR 없음(배포 진행 가능)
    # 1 = ERROR 1건 이상(배포 중단 권장) - CI/배포 절차에서 그대로 게이트로 사용

--api-image/--scheduler-image/--web-image를 생략하면 각각 API_IMAGE/
SCHEDULER_IMAGE/WEB_IMAGE 환경변수(docker-compose.yml이 읽는 것과 동일한
이름)를 대신 읽는다 - 배포 스크립트에서 .env를 그대로 export한 뒤 인자 없이
호출할 수 있게 하기 위함이다.

--expected-backend-revision을 생략하면 이 저장소의 `git rev-parse HEAD`를
기준으로 삼는다(api/scheduler에만 적용 - web에는 자동 추론값을 쓰지 않는다,
web은 다른 커밋일 수 있으므로 명시적으로 지정했을 때만 검증한다).

Secret은 다루지 않는다 - 이미지 라벨(release_candidate_sha/build_timestamp)은
빌드 시 --label로 직접 붙인 값으로 비밀이 아니다. DATABASE_URL 등은 이
스크립트가 아예 열람하지 않는다.
"""

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LABEL_SHA = "release_candidate_sha"
REPO_ROOT = Path(__file__).resolve().parent.parent


class VerifyImagesError(Exception):
    """읽기 전용 조회 자체가 불가능할 때(docker 미설치, git 미설치 등)."""


@dataclass(frozen=True)
class ImageInfo:
    ref: str
    image_id: Optional[str]
    revision_sha: Optional[str]
    exists: bool


def _run(cmd: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False, shell=False, cwd=cwd)
    except FileNotFoundError as exc:
        raise VerifyImagesError(f"명령을 실행할 수 없습니다: {cmd[0]} ({exc})") from exc
    return result.returncode, result.stdout, result.stderr


def inspect_image(ref: str) -> ImageInfo:
    """이미지 ID와 release_candidate_sha 라벨만 읽는다(다른 필드/Secret 미조회)."""
    rc, out, _stderr = _run(
        ["docker", "image", "inspect", "-f", f'{{{{.Id}}}}|{{{{index .Config.Labels "{LABEL_SHA}"}}}}', ref]
    )
    if rc != 0:
        return ImageInfo(ref=ref, image_id=None, revision_sha=None, exists=False)
    line = out.strip().splitlines()[0] if out.strip() else ""
    parts = line.split("|", 1)
    image_id = parts[0] if parts and parts[0] else None
    revision_sha = parts[1] if len(parts) > 1 and parts[1] and parts[1] != "<no value>" else None
    return ImageInfo(ref=ref, image_id=image_id, revision_sha=revision_sha, exists=True)


def git_head_sha() -> Optional[str]:
    rc, out, _stderr = _run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT)
    if rc != 0:
        return None
    return out.strip() or None


def verify(
    api_image: str,
    scheduler_image: str,
    expected_backend_revision: Optional[str],
    web_image: Optional[str] = None,
    expected_web_revision: Optional[str] = None,
) -> list[str]:
    """검증 결과 ERROR 메시지 목록을 반환한다(비어 있으면 통과).

    backend(api/scheduler)는 항상 서로 일치해야 하고, expected_backend_revision이
    주어지면 그 값과도 일치해야 한다.

    web은 api/scheduler와 독립 배포 가능하므로(정적 파일 서빙만 하고 DB
    migration과 무관) backend와 다른 커밋이어도 그 자체는 ERROR가 아니다 -
    web_image와 expected_web_revision을 "둘 다" 넘겼을 때만 web 자신의
    존재·라벨·기대값 일치를 별도로 검증한다(호출부가 web_image만 주고
    expected_web_revision을 생략하면 기존처럼 참고 출력 대상일 뿐 검증하지
    않는다 - _print_report가 담당)."""
    errors: list[str] = []

    api_info = inspect_image(api_image)
    scheduler_info = inspect_image(scheduler_image)

    if not api_info.exists:
        errors.append(f"api 이미지가 로컬에 없습니다: {api_image}")
    if not scheduler_info.exists:
        errors.append(f"scheduler 이미지가 로컬에 없습니다: {scheduler_image}")

    if api_info.exists and not api_info.revision_sha:
        errors.append(f"api 이미지에 {LABEL_SHA} 라벨이 없습니다: {api_image}")
    if scheduler_info.exists and not scheduler_info.revision_sha:
        errors.append(f"scheduler 이미지에 {LABEL_SHA} 라벨이 없습니다: {scheduler_image}")

    if api_info.revision_sha and scheduler_info.revision_sha:
        if api_info.revision_sha != scheduler_info.revision_sha:
            errors.append(
                "api/scheduler가 서로 다른 커밋을 가리킵니다: "
                f"api={api_info.revision_sha[:12]} scheduler={scheduler_info.revision_sha[:12]}"
            )
        elif expected_backend_revision and api_info.revision_sha != expected_backend_revision:
            errors.append(
                f"api/scheduler 이미지의 revision({api_info.revision_sha[:12]})이 "
                f"기대한 커밋({expected_backend_revision[:12]})과 다릅니다."
            )

    if expected_web_revision:
        if not web_image:
            errors.append("--expected-web-revision이 지정됐지만 --web-image가 없습니다.")
        else:
            web_info = inspect_image(web_image)
            if not web_info.exists:
                errors.append(f"web 이미지가 로컬에 없습니다: {web_image}")
            elif not web_info.revision_sha:
                errors.append(f"web 이미지에 {LABEL_SHA} 라벨이 없습니다: {web_image}")
            elif web_info.revision_sha != expected_web_revision:
                errors.append(
                    f"web 이미지의 revision({web_info.revision_sha[:12]})이 "
                    f"기대한 커밋({expected_web_revision[:12]})과 다릅니다."
                )

    return errors


def _print_report(
    api_image: str,
    scheduler_image: str,
    web_image: Optional[str],
    expected_backend_revision: Optional[str],
    expected_web_revision: Optional[str],
    errors: list[str],
) -> None:
    api_info = inspect_image(api_image)
    scheduler_info = inspect_image(scheduler_image)
    print("=== candidate 이미지 revision 검증 ===")
    print(f"기대 backend 커밋: {expected_backend_revision[:12] + '...' if expected_backend_revision else '(미지정)'}")
    print(f"api        : {api_image}")
    print(f"  image_id : {api_info.image_id or '(없음)'}")
    print(f"  revision : {api_info.revision_sha[:12] + '...' if api_info.revision_sha else '(라벨 없음)'}")
    print(f"scheduler  : {scheduler_image}")
    print(f"  image_id : {scheduler_info.image_id or '(없음)'}")
    print(f"  revision : {scheduler_info.revision_sha[:12] + '...' if scheduler_info.revision_sha else '(라벨 없음)'}")
    if web_image:
        web_info = inspect_image(web_image)
        checked = " - 기대값과 검증됨" if expected_web_revision else " (backend와 달라도 ERROR 아님 - 참고용)"
        print(f"web        : {web_image}{checked}")
        print(f"  image_id : {web_info.image_id or '(없음)'}")
        print(f"  revision : {web_info.revision_sha[:12] + '...' if web_info.revision_sha else '(라벨 없음)'}")
        if expected_web_revision:
            print(f"  기대 web 커밋 : {expected_web_revision[:12]}...")
    if errors:
        print("\n[ERROR]")
        for e in errors:
            print(f"  - {e}")
    else:
        print("\n[PASS] 검증 통과, 배포 진행 가능.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="배포 직전 api/scheduler/web candidate 이미지 revision 검증(읽기 전용)"
    )
    parser.add_argument("--api-image", default=os.environ.get("API_IMAGE"), help="기본값: API_IMAGE 환경변수")
    parser.add_argument(
        "--scheduler-image", default=os.environ.get("SCHEDULER_IMAGE"), help="기본값: SCHEDULER_IMAGE 환경변수"
    )
    parser.add_argument("--web-image", default=os.environ.get("WEB_IMAGE"), help="기본값: WEB_IMAGE 환경변수(선택)")
    parser.add_argument(
        "--expected-backend-revision",
        default=None,
        help="이 값과 api/scheduler의 revision 라벨을 비교한다(기본값: 현재 저장소의 git HEAD)",
    )
    parser.add_argument(
        "--expected-web-revision",
        default=None,
        help="지정하면 web 이미지의 존재·라벨·revision 일치까지 검증한다(생략 시 web은 참고 출력만)",
    )
    args = parser.parse_args(argv)

    if not args.api_image or not args.scheduler_image:
        print("[ERROR] --api-image/--scheduler-image(또는 API_IMAGE/SCHEDULER_IMAGE 환경변수)가 필요합니다.")
        return 1

    try:
        expected_backend_revision = args.expected_backend_revision or git_head_sha()
        errors = verify(
            args.api_image,
            args.scheduler_image,
            expected_backend_revision,
            web_image=args.web_image,
            expected_web_revision=args.expected_web_revision,
        )
        _print_report(
            args.api_image,
            args.scheduler_image,
            args.web_image,
            expected_backend_revision,
            args.expected_web_revision,
            errors,
        )
    except VerifyImagesError as exc:
        print(f"[ERROR] {exc}")
        return 1

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
