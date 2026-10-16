"""통합 테스트 "러너" 이미지 사전 점검 helper.

tests/integration/*_pg.py 중 RUNNER_IMAGE(shopping_erp-api:latest)를 쓰는 테스트는 그 이미지가 로컬에 이미 있다고
가정하고 `docker run`을 실행한다. clean 환경(예: GitHub runner)에는 이미지가 없고, 없으면 docker가 Docker Hub에서
pull을 시도해 "pull access denied"(exit 125)라는 알아보기 어려운 오류로 실패한다. 개발 PC에 예전에 빌드한 이미지가
남아 있으면 이 가정이 우연히 성립해 문제가 가려진다.

이 모듈은 이미지가 없을 때 실행 전에 원인과 해결 명령을 알려 주는 메시지를 만든다. conftest.py의 autouse fixture가
사용한다. 외부 호출 의존(shutil.which, subprocess.run)은 인자로 주입할 수 있어 단위 테스트에서 대체한다.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from typing import Any

BUILD_COMMAND = "docker build -t shopping_erp-api:latest ."


def runner_image_problem(
    image: str, *, which: Callable[[str], str | None] = shutil.which, run: Callable[..., Any] = subprocess.run
) -> str | None:
    """이미지가 없어서 테스트를 실행할 수 없으면 사용자용 메시지를, 점검할 수 없거나 문제가 없으면 None을 돌려준다.

    Docker CLI가 없거나 데몬에 연결할 수 없는 환경은 각 테스트 파일이 이미 스스로 skip하므로 여기서는 건드리지 않는다.
    """
    if which("docker") is None:
        return None
    try:
        info = run(["docker", "info"], capture_output=True, timeout=10)
        if info.returncode != 0:
            return None
        inspect = run(["docker", "image", "inspect", image], capture_output=True, timeout=30)
    except Exception:
        return None
    if inspect.returncode == 0:
        return None
    return (
        f"통합 테스트 러너 이미지 '{image}'가 로컬에 없습니다. 이 이미지는 테스트가 자동으로 만들지 않으므로 먼저 "
        f"`{BUILD_COMMAND}`로 빌드해야 합니다(CI의 backend job은 Pytest 전에 같은 명령을 실행합니다). "
        "이미지가 없으면 docker run이 Docker Hub에서 pull을 시도해 'pull access denied'로 실패합니다."
    )
