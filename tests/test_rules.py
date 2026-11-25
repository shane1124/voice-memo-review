"""Task 2 单元测试：显化规则库、4 套无私货内置模板、透明 Prompt 拼装与路径安全校验。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from voice_review.rules import RuleStudio


class TestRuleStudio(unittest.TestCase):
    def test_builtin_templates_clean_and_resettable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = RuleStudio(Path(tmp) / "rules")
            studio.ensure_initialized()
            templates = studio.list_templates()
            ids = {t.id for t in templates}
            self.assertEqual(ids, {"general_review", "meeting_minutes", "sales_communication", "solo_memo"})
            self.assertEqual(studio.get_template().id, "general_review")

            # 确保 4 套模板完全干净，不含微信机器人或私有定制链路
            for t in templates:
                self.assertTrue(t.is_builtin)
                for forbidden in ("hermes", "clawbot", "微信快报", "私人业务"):
                    self.assertNotIn(forbidden.lower(), t.body.lower())

            # 修改后持久化且可重置回内置默认
            studio.save_template(
                "general_review",
                name="改版模板",
                description="我的定制说明",
                body="自定义分析正文",
                is_default=True,
            )
            self.assertEqual(studio.get_template("general_review").body, "自定义分析正文")
            restored = studio.reset_builtin_template("general_review")
            self.assertNotEqual(restored.body, "自定义分析正文")
            self.assertIn("核心摘要", restored.body)

    def test_custom_template_default_switch_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = RuleStudio(Path(tmp) / "rules")
            studio.ensure_initialized()
            created = studio.save_template(
                "interview_notes",
                name="面试评估",
                description="候选人面试结构化打分",
                body="请按技术深度、沟通表达、风险点三段输出。",
                is_default=True,
            )
            self.assertTrue(created.is_default)
            self.assertFalse(created.is_builtin)
            self.assertEqual(studio.get_template().id, "interview_notes")
            self.assertTrue(studio.delete_template("interview_notes"))
            self.assertEqual(studio.get_template().id, "general_review")

    def test_compose_analysis_prompt_transparency_and_persona_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = RuleStudio(Path(tmp) / "rules")
            studio.ensure_initialized()
            p1 = studio.compose_analysis_prompt(
                template_id="meeting_minutes",
                title="产品周会",
                recorded_at="2026-11-25T10:00:00+08:00",
                duration_sec=1800,
                speakers_summary="- SPEAKER_00 -> 张三（约 15.0 分钟）",
                transcript_text="[00:00:01] 张三：今天主要对齐下周发布节奏。",
            )
            self.assertIn("产品周会", p1)
            self.assertNotIn("用户背景说明", p1)
            self.assertIn("[00:00:01] 张三", p1)

            studio.save_persona("我是 B 端产品经理，重点关注交付风险与客户真实预算。")
            studio.save_glossary("SaaS API SLA")
            self.assertEqual(studio.get_glossary(), "SaaS API SLA")
            p2 = studio.compose_analysis_prompt(
                template_id="meeting_minutes",
                title="产品周会",
                recorded_at="2026-11-25T10:00:00+08:00",
                duration_sec=1800,
                speakers_summary="- SPEAKER_00 -> 张三",
                transcript_text="[00:00:01] 张三：开始开会",
            )
            self.assertIn("用户背景说明", p2)
            self.assertIn("B 端产品经理", p2)

    def test_template_id_path_traversal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            studio = RuleStudio(Path(tmp) / "rules")
            for bad in ("../evil", "a/b", "BAD_UPPER", "", " "):
                with self.assertRaises(ValueError):
                    studio.save_template(bad, name="x", description="x", body="x")


if __name__ == "__main__":
    unittest.main()
