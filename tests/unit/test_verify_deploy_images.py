"""
tests/unit/test_verify_deploy_images.py
------------------------------------------------
scripts/verify_deploy_images.py 단위테스트. docker/git 서브프로세스는 항상
mock한다(실제 이미지/저장소를 건드리지 않는다) - 실제 candidate 이미지로
전 과정을 검증하는 것은 tests/integration/test_compose_image_pinning.py가
담당한다.
"""

from typing import Mapping, Optional
from unittest.mock import patch

from scripts import verify_deploy_images as vdi


def _fake_inspect(mapping: Mapping[str, Optional[tuple[str, Optional[str]]]]):
    """ref -> (image_id, revision_sha) 매핑, 없으면 이미지가 존재하지 않음."""

    def _run(cmd, cwd=None):
        ref = cmd[-1]
        if cmd[:2] == ["docker", "image"]:
            found = mapping.get(ref)
            if found is None:
                return 1, "", "no such image"
            image_id, sha = found
            label = sha if sha is not None else "<no value>"
            return 0, f"{image_id}|{label}\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    return _run


class TestInspectImage:
    def test_existing_image_with_label(self):
        with patch.object(vdi, "_run", _fake_inspect({"img:tag": ("sha256:aaa", "deadbeef")})):
            info = vdi.inspect_image("img:tag")
        assert info.exists is True
        assert info.image_id == "sha256:aaa"
        assert info.revision_sha == "deadbeef"

    def test_missing_image(self):
        with patch.object(vdi, "_run", _fake_inspect({})):
            info = vdi.inspect_image("img:missing")
        assert info.exists is False
        assert info.image_id is None
        assert info.revision_sha is None

    def test_existing_image_without_label(self):
        with patch.object(vdi, "_run", _fake_inspect({"img:nolabel": ("sha256:bbb", None)})):
            info = vdi.inspect_image("img:nolabel")
        assert info.exists is True
        assert info.image_id == "sha256:bbb"
        assert info.revision_sha is None


class TestVerifyBackend:
    def test_matching_revisions_pass(self):
        mapping = {"api:1": ("sha256:aaa", "commit123"), "sched:1": ("sha256:aaa", "commit123")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify("api:1", "sched:1", expected_backend_revision="commit123")
        assert errors == []

    def test_mismatched_revisions_between_api_and_scheduler_fails(self):
        mapping = {"api:1": ("sha256:aaa", "commit_new"), "sched:1": ("sha256:bbb", "commit_old")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify("api:1", "sched:1", expected_backend_revision=None)
        assert len(errors) == 1
        assert "다른 커밋" in errors[0]

    def test_matching_but_not_expected_revision_fails(self):
        mapping = {"api:1": ("sha256:aaa", "commit_x"), "sched:1": ("sha256:aaa", "commit_x")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify("api:1", "sched:1", expected_backend_revision="commit_y")
        assert len(errors) == 1
        assert "기대한 커밋" in errors[0]

    def test_missing_api_image_fails(self):
        mapping = {"sched:1": ("sha256:aaa", "commit_x")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify("api:missing", "sched:1", expected_backend_revision=None)
        assert any("api 이미지가 로컬에 없습니다" in e for e in errors)

    def test_missing_scheduler_image_fails(self):
        mapping = {"api:1": ("sha256:aaa", "commit_x")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify("api:1", "sched:missing", expected_backend_revision=None)
        assert any("scheduler 이미지가 로컬에 없습니다" in e for e in errors)

    def test_missing_revision_label_on_api_fails(self):
        mapping = {"api:1": ("sha256:aaa", None), "sched:1": ("sha256:bbb", "commit_x")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify("api:1", "sched:1", expected_backend_revision=None)
        assert any("api 이미지에" in e and "라벨이 없습니다" in e for e in errors)

    def test_both_missing_images_reports_both_errors(self):
        with patch.object(vdi, "_run", _fake_inspect({})):
            errors = vdi.verify("api:missing", "sched:missing", expected_backend_revision=None)
        assert len(errors) == 2


class TestVerifyWebRevision:
    """web은 api/scheduler와 독립 배포 가능하다 - expected_web_revision을
    명시적으로 넘겼을 때만 검증 대상이 된다."""

    _BACKEND_OK = {"api:1": ("sha256:aaa", "commit_x"), "sched:1": ("sha256:aaa", "commit_x")}

    def test_web_not_checked_when_expected_web_revision_omitted(self):
        mapping = {**self._BACKEND_OK, "web:old": ("sha256:www", "some_other_commit")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify("api:1", "sched:1", "commit_x", web_image="web:old", expected_web_revision=None)
        assert errors == []  # backend와 달라도 web을 검증하지 않았으므로 통과.

    def test_web_matching_expected_revision_passes(self):
        mapping = {**self._BACKEND_OK, "web:1": ("sha256:www", "web_commit")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify("api:1", "sched:1", "commit_x", web_image="web:1", expected_web_revision="web_commit")
        assert errors == []

    def test_web_differs_from_backend_but_matches_own_expected_is_allowed(self):
        """backend는 새 커밋, web은 프론트 변경이 없어 이전 커밋 이미지를
        그대로 쓰는 일반적인 시나리오 - 서로 달라도 각자 기대값과 맞으면
        허용해야 한다."""
        mapping = {**self._BACKEND_OK, "web:old": ("sha256:www", "old_web_commit")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify(
                "api:1",
                "sched:1",
                expected_backend_revision="commit_x",
                web_image="web:old",
                expected_web_revision="old_web_commit",
            )
        assert errors == []

    def test_web_mismatched_expected_revision_fails(self):
        mapping = {**self._BACKEND_OK, "web:1": ("sha256:www", "web_commit_actual")}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify(
                "api:1", "sched:1", "commit_x", web_image="web:1", expected_web_revision="web_commit_expected"
            )
        assert len(errors) == 1
        assert "web 이미지의 revision" in errors[0]

    def test_web_missing_label_fails(self):
        mapping = {**self._BACKEND_OK, "web:1": ("sha256:www", None)}
        with patch.object(vdi, "_run", _fake_inspect(mapping)):
            errors = vdi.verify("api:1", "sched:1", "commit_x", web_image="web:1", expected_web_revision="anything")
        assert any("web 이미지에" in e and "라벨이 없습니다" in e for e in errors)

    def test_web_missing_image_fails(self):
        with patch.object(vdi, "_run", _fake_inspect(self._BACKEND_OK)):
            errors = vdi.verify(
                "api:1", "sched:1", "commit_x", web_image="web:missing", expected_web_revision="anything"
            )
        assert any("web 이미지가 로컬에 없습니다" in e for e in errors)

    def test_expected_web_revision_without_web_image_fails(self):
        with patch.object(vdi, "_run", _fake_inspect(self._BACKEND_OK)):
            errors = vdi.verify("api:1", "sched:1", "commit_x", web_image=None, expected_web_revision="anything")
        assert any("--web-image가 없습니다" in e for e in errors)


class TestMainCli:
    def test_missing_required_args_returns_1(self, capsys):
        rc = vdi.main([])
        assert rc == 1
        assert "필요합니다" in capsys.readouterr().out

    def test_pass_path_returns_0(self, monkeypatch, capsys):
        mapping = {"api:1": ("sha256:aaa", "commit_x"), "sched:1": ("sha256:aaa", "commit_x")}
        monkeypatch.setattr(vdi, "_run", _fake_inspect(mapping))
        monkeypatch.setattr(vdi, "git_head_sha", lambda: "commit_x")
        rc = vdi.main(["--api-image", "api:1", "--scheduler-image", "sched:1"])
        assert rc == 0
        assert "[PASS]" in capsys.readouterr().out

    def test_fail_path_returns_1(self, monkeypatch, capsys):
        mapping = {"api:1": ("sha256:aaa", "commit_new"), "sched:1": ("sha256:bbb", "commit_old")}
        monkeypatch.setattr(vdi, "_run", _fake_inspect(mapping))
        monkeypatch.setattr(vdi, "git_head_sha", lambda: "commit_new")
        rc = vdi.main(["--api-image", "api:1", "--scheduler-image", "sched:1"])
        assert rc == 1
        assert "[ERROR]" in capsys.readouterr().out

    def test_reads_env_vars_when_flags_omitted(self, monkeypatch, capsys):
        mapping = {"envapi:1": ("sha256:aaa", "commit_x"), "envsched:1": ("sha256:aaa", "commit_x")}
        monkeypatch.setenv("API_IMAGE", "envapi:1")
        monkeypatch.setenv("SCHEDULER_IMAGE", "envsched:1")
        monkeypatch.setattr(vdi, "_run", _fake_inspect(mapping))
        monkeypatch.setattr(vdi, "git_head_sha", lambda: "commit_x")
        rc = vdi.main([])
        assert rc == 0

    def test_cli_expected_backend_and_web_revision_pass(self, monkeypatch, capsys):
        mapping = {
            "api:1": ("sha256:aaa", "backend_commit"),
            "sched:1": ("sha256:aaa", "backend_commit"),
            "web:1": ("sha256:www", "web_commit"),
        }
        monkeypatch.setattr(vdi, "_run", _fake_inspect(mapping))
        rc = vdi.main(
            [
                "--api-image",
                "api:1",
                "--scheduler-image",
                "sched:1",
                "--web-image",
                "web:1",
                "--expected-backend-revision",
                "backend_commit",
                "--expected-web-revision",
                "web_commit",
            ]
        )
        assert rc == 0
        assert "[PASS]" in capsys.readouterr().out

    def test_cli_web_revision_mismatch_returns_1(self, monkeypatch, capsys):
        mapping = {
            "api:1": ("sha256:aaa", "backend_commit"),
            "sched:1": ("sha256:aaa", "backend_commit"),
            "web:1": ("sha256:www", "actual_web_commit"),
        }
        monkeypatch.setattr(vdi, "_run", _fake_inspect(mapping))
        rc = vdi.main(
            [
                "--api-image",
                "api:1",
                "--scheduler-image",
                "sched:1",
                "--web-image",
                "web:1",
                "--expected-backend-revision",
                "backend_commit",
                "--expected-web-revision",
                "expected_web_commit",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert "[ERROR]" in out
        assert "web 이미지의 revision" in out
