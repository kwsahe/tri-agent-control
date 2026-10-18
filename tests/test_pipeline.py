"""고정 역할 코딩 파이프라인 회귀 테스트.

모든 파일 조작은 TemporaryDirectory 안에서만 한다. 실제 모델 CLI는 부르지 않는다.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isolation  # noqa: F401  (실제 데이터 폴더 격리 — 반드시 roundtable보다 먼저)
import roundtable

SESSION_ID = "20260101_000000_000030"
ALL = list(roundtable.AGENT_ORDER)


class PipelineStepsTests(unittest.TestCase):
    def test_planning_is_a_single_read_only_claude_turn_that_asks_for_approval(self):
        steps = roundtable.steps_for_mode("coding", ALL)
        self.assertEqual(len(steps), 1)
        agent, phase, instruction, cli_mode = steps[0]
        self.assertEqual((agent, phase, cli_mode), ("claude", roundtable.CONFIRM_PHASE, "discussion"))
        self.assertIn(roundtable.APPROVAL_TOKEN, instruction)

    def test_cycle_is_implement_test_review_in_fixed_order(self):
        steps = roundtable.goal_coding_steps(ALL)
        self.assertEqual(
            [(s[0], s[1], s[3]) for s in steps],
            [("claude", "구현", "coding"),
             ("antigravity", "테스트", "coding"),
             ("codex", roundtable.REVIEW_PHASE, "discussion")],
        )
        # 시작 모델 순서를 바꿔도 파이프라인 순서는 고정이다.
        self.assertEqual(roundtable.goal_coding_steps(["codex", "antigravity", "claude"]), steps)

    def test_disabled_agents_drop_their_steps(self):
        steps = roundtable.goal_coding_steps(["claude", "codex"])
        self.assertEqual([s[1] for s in steps], ["구현", roundtable.REVIEW_PHASE])

    def test_fixed_roles_map_onto_the_role_catalog(self):
        roles = roundtable.pipeline_agent_roles()
        self.assertEqual(roles, {"codex": "reviewer", "antigravity": "qa", "claude": ""})
        self.assertFalse(roundtable.ROLE_CATALOG["reviewer"]["can_write"])
        self.assertIn("tests/**", roundtable.ROLE_CATALOG["qa"]["patterns"])
        self.assertEqual(
            roundtable.pipeline_roles_label(ALL),
            "Claude Code=구현, Antigravity=테스트, Codex=검토",
        )

    def test_coding_mode_never_delegates_with_call_agent(self):
        for phase in ("구현", "테스트", roundtable.REVIEW_PHASE, roundtable.CONFIRM_PHASE):
            self.assertFalse(roundtable.allow_agent_calls("coding", phase))
        self.assertTrue(roundtable.allow_agent_calls("discussion", "개입 답변"))


if __name__ == "__main__":
    unittest.main()
