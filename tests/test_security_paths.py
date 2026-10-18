"""P0-A: 세션 ID 경로 조작 차단 회귀 테스트.

모든 파괴적 재현은 TemporaryDirectory 안에서만 한다. 실제 sessions/,
roundtable_memory/, Profile 파일은 절대 건드리지 않는다.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable
from triagent.security.paths import (
    UnsafePathError,
    UnsafeSessionIdError,
    is_canonical_session_id,
    is_valid_session_id,
    safe_child,
    validate_session_id,
)


#: 실제 sessions/ 폴더를 조사해서 확인한 기존 정상 형식.
EXISTING_SESSION_IDS = [
    "20260712_173325_471441",
    "20260724_180805_659055",
    "20260719_151737_042579",
]

TRAVERSAL_IDS = [
    "../victim",
    "..\\victim",
    "../../etc/passwd",
    "..\\..\\Windows\\System32",
    "C:\\Windows\\System32\\drivers",
    "C:/Users/victim",
    "/etc/passwd",
    "\\\\server\\share",
    "D:relative",
    "%2e%2e%2fvictim",
    "%2E%2E%5Cvictim",
    "..%252fvictim",
    "sess/../../outside",
    "sess\\..\\outside",
    ".",
    "..",
    "...",
    "~",
    "~/victim",
    "con",
    "NUL",
    "a" * 65,
    "",
    "   ",
    "with space",
    "semi;colon",
    "null\x00byte",
]


class SessionIdValidationTests(unittest.TestCase):
    def test_existing_session_ids_remain_valid(self):
        """기존 세션을 못 읽게 되면 안 된다."""
        for session_id in EXISTING_SESSION_IDS:
            with self.subTest(session_id=session_id):
                self.assertEqual(validate_session_id(session_id), session_id)
                self.assertTrue(is_valid_session_id(session_id))
                self.assertTrue(is_canonical_session_id(session_id))

    def test_server_generated_ids_are_canonical(self):
        for _ in range(5):
            self.assertTrue(is_canonical_session_id(roundtable.new_session_id()))

    def test_traversal_and_absolute_paths_are_rejected(self):
        for candidate in TRAVERSAL_IDS:
            with self.subTest(candidate=candidate):
                with self.assertRaises(UnsafeSessionIdError):
                    validate_session_id(candidate)
                self.assertFalse(is_valid_session_id(candidate))

    def test_non_string_ids_are_rejected(self):
        for candidate in (None, 12345, [], {}, object()):
            with self.subTest(candidate=candidate):
                with self.assertRaises(UnsafeSessionIdError):
                    validate_session_id(candidate)

    def test_canonical_only_rejects_legacy_shaped_names(self):
        self.assertTrue(is_valid_session_id("legacy-session-1"))
        with self.assertRaises(UnsafeSessionIdError):
            validate_session_id("legacy-session-1", canonical_only=True)


class SafeChildTests(unittest.TestCase):
    def test_resolved_path_must_stay_inside_parent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "allowed"
            parent.mkdir()
            self.assertEqual(
                safe_child(parent, "file.json"), (parent / "file.json").resolve()
            )
            for bad in ("..", "../escape", "..\\escape", "C:\\Windows", "a/b"):
                with self.subTest(bad=bad), self.assertRaises(UnsafePathError):
                    safe_child(parent, bad)

    def test_empty_parts_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(UnsafePathError):
                safe_child(Path(temp_dir))
            with self.assertRaises(UnsafePathError):
                safe_child(Path(temp_dir), "")


class SessionPathEntryPointTests(unittest.TestCase):
    """모든 경로 진입점이 같은 보호를 받는지 확인한다."""

    ENTRY_POINTS = (
        "session_json_path",
        "session_transcript_path",
        "session_memory_dir",
        "session_profile_path",
        "session_roles_path",
        "session_agent_profile_dir",
        "checkpoint_dir",
    )

    def test_every_path_helper_rejects_traversal(self):
        for name in self.ENTRY_POINTS:
            helper = getattr(roundtable, name)
            for candidate in ("../victim", "..\\victim", "C:\\Windows", "%2e%2e%2fx"):
                with self.subTest(helper=name, candidate=candidate):
                    with self.assertRaises(UnsafePathError):
                        helper(candidate)

    def test_agent_profile_path_rejects_unknown_agent(self):
        with self.assertRaises(UnsafePathError):
            roundtable.session_agent_profile_path("20260712_173325_471441", "../evil")

    def test_helpers_accept_existing_ids(self):
        session_id = EXISTING_SESSION_IDS[0]
        self.assertTrue(
            str(roundtable.session_json_path(session_id)).endswith(f"{session_id}.json")
        )
        self.assertTrue(
            roundtable.session_memory_dir(session_id).is_relative_to(
                roundtable.MEMORY_DIR.resolve()
            )
        )


class DestructiveOperationTests(unittest.TestCase):
    """삭제·저장이 허용 폴더 밖에 영향을 주지 않는지 — 전부 임시 폴더에서."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.sessions_dir = root / "sessions"
        self.memory_dir = root / "memory"
        self.outside = root / "victim"
        self.sessions_dir.mkdir()
        self.memory_dir.mkdir()
        self.outside.mkdir()
        (self.outside / "precious.txt").write_text("keep me", encoding="utf-8")

        self._patches = [
            patch.object(roundtable, "SESSIONS_DIR", self.sessions_dir),
            patch.object(roundtable, "MEMORY_DIR", self.memory_dir),
        ]
        for item in self._patches:
            item.start()
        self.original_state = roundtable.STATE
        roundtable.STATE = roundtable.new_state()

    def tearDown(self):
        roundtable.STATE = self.original_state
        for item in self._patches:
            item.stop()
        self._tmp.cleanup()

    def test_delete_session_cannot_reach_outside_directories(self):
        for candidate in ("../victim", "..\\victim", str(self.outside)):
            with self.subTest(candidate=candidate):
                with self.assertRaises(UnsafeSessionIdError):
                    roundtable.delete_session(candidate)
        self.assertTrue((self.outside / "precious.txt").is_file())
        self.assertTrue(self.outside.is_dir())

    def test_delete_session_removes_only_the_named_session(self):
        session_id = "20260101_010101_000001"
        (self.sessions_dir / f"{session_id}.json").write_text("{}", encoding="utf-8")
        (self.sessions_dir / f"{session_id}.md").write_text("x", encoding="utf-8")
        (self.memory_dir / session_id).mkdir()
        keeper = "20260101_010101_000002"
        (self.sessions_dir / f"{keeper}.json").write_text("{}", encoding="utf-8")

        result = roundtable.delete_session(session_id)

        self.assertTrue(result["success"])
        self.assertFalse((self.sessions_dir / f"{session_id}.json").exists())
        self.assertFalse((self.memory_dir / session_id).exists())
        self.assertTrue((self.sessions_dir / f"{keeper}.json").exists())
        self.assertTrue((self.outside / "precious.txt").is_file())

    def test_profile_save_cannot_write_outside_memory_dir(self):
        with self.assertRaises(UnsafeSessionIdError):
            roundtable.save_profile_content("payload", "../victim")
        self.assertFalse((self.outside / "Profile.md").exists())
        self.assertEqual(
            (self.outside / "precious.txt").read_text(encoding="utf-8"), "keep me"
        )

    def test_session_metadata_update_cannot_write_outside(self):
        with self.assertRaises(UnsafeSessionIdError):
            roundtable.update_session_metadata("../victim", "tag", True, False)
        with self.assertRaises(UnsafeSessionIdError):
            roundtable.rename_session("..\\victim", "새 이름")
        self.assertEqual(list(self.outside.iterdir()), [self.outside / "precious.txt"])

    def test_clone_and_load_reject_bad_ids(self):
        # load_session은 조용히 None (호출부가 404를 낸다).
        self.assertIsNone(roundtable.load_session("../victim"))
        # 상태를 바꾸는 API는 예외 -> HTTP 400.
        with self.assertRaises(UnsafeSessionIdError):
            roundtable.clone_session("../victim")
        with self.assertRaises(UnsafeSessionIdError):
            roundtable.activate_session("../victim")

    def test_list_sessions_skips_odd_names_without_deleting_them(self):
        # 세션 ID로 쓸 수 없는 이름(점 포함). 자동으로 지우거나 옮기면 안 된다.
        odd = self.sessions_dir / "back.up.json"
        odd.write_text(json.dumps({"id": "back.up"}), encoding="utf-8")
        good_id = "20260101_010101_000003"
        (self.sessions_dir / f"{good_id}.json").write_text(
            json.dumps({"id": good_id, "created_at": "2026-01-01"}), encoding="utf-8"
        )
        roundtable._SKIPPED_SESSION_WARNINGS.clear()

        summaries = roundtable.list_sessions()

        ids = [item["id"] for item in summaries]
        self.assertIn(good_id, ids)
        self.assertNotIn("back.up", ids)
        # 자동 삭제/이동하지 않는다.
        self.assertTrue(odd.is_file())

    def test_corrupt_session_does_not_break_the_whole_list(self):
        broken_id = "20260101_010101_000004"
        (self.sessions_dir / f"{broken_id}.json").write_text("{not json", encoding="utf-8")
        good_id = "20260101_010101_000005"
        (self.sessions_dir / f"{good_id}.json").write_text(
            json.dumps({"id": good_id, "created_at": "2026-01-02"}), encoding="utf-8"
        )
        roundtable._SKIPPED_SESSION_WARNINGS.clear()

        summaries = roundtable.list_sessions()

        ids = [item["id"] for item in summaries]
        self.assertIn(good_id, ids)
        self.assertIn(broken_id, ids)
        broken = next(item for item in summaries if item["id"] == broken_id)
        self.assertTrue(broken["corrupt"])
        # 손상된 파일도 지우지 않는다.
        self.assertTrue((self.sessions_dir / f"{broken_id}.json").is_file())


if __name__ == "__main__":
    unittest.main()
