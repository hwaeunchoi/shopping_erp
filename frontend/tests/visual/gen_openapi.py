"""백엔드 OpenAPI 스키마를 파일로 내보낸다(모의 API 서버 입력용).

앱 객체를 import 해 app.openapi() 만 호출한다. DB 조회·네트워크 호출·외부 API 호출을 하지 않는다.
사용: venv\\Scripts\\python frontend\\tests\\visual\\gen_openapi.py <출력 json 경로>
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from api.main import app  # noqa: E402

out = Path(sys.argv[1])
out.write_text(json.dumps(app.openapi(), ensure_ascii=False), encoding="utf-8")
print(len(app.openapi()["paths"]), "paths ->", out)
