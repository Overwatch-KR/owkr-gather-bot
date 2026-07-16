from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from jinja2 import Environment

from src.application.recruitment_complete_editor import (
    RecruitmentCompleteCopy,
    build_recruitment_complete_template,
    dump_recruitment_complete_copy,
    load_recruitment_complete_copy,
)


class RecruitmentCompleteEditorTest(unittest.TestCase):
    def test_generated_template_keeps_user_copy_literal(self) -> None:
        copy = RecruitmentCompleteCopy(
            tier_instruction="{{ user }}도 그대로 보여 주세요.",
            tier_format_example="{% for item in users %}개발 문법 아님{% endfor %}",
        )

        source = build_recruitment_complete_template(copy)
        rendered = Environment().from_string(source).render(
            starts_at="오후 8시",
            starts_in="1시간 후",
            user="@참가자",
            tier_channel="#내전-티어",
            tier_deadline="오후 7시 30분",
            lobby_time="오후 7시 50분",
            manner_notice="즐겁게 플레이해 주세요.",
        )

        self.assertIn("{{ user }}도 그대로 보여 주세요.", rendered)
        self.assertIn(
            "{% for item in users %}개발 문법 아님{% endfor %}",
            rendered,
        )

    def test_yaml_round_trip_preserves_multiline_copy(self) -> None:
        copy = RecruitmentCompleteCopy(
            tier_format_example="배틀태그\n탱커 / 딜러 / 힐러\n\n레몬#1234",
            extra_notice="첫 줄\n둘째 줄",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "copy.yaml"
            path.write_text(
                dump_recruitment_complete_copy(copy),
                encoding="utf-8",
            )

            loaded = load_recruitment_complete_copy(path)

        self.assertEqual(loaded, copy)

    def test_missing_yaml_uses_non_developer_defaults(self) -> None:
        with TemporaryDirectory() as directory:
            loaded = load_recruitment_complete_copy(
                Path(directory) / "missing.yaml"
            )

        self.assertEqual(loaded.start_heading, "내전 시작")
        self.assertIn("배틀태그", loaded.tier_format_example)
        self.assertNotIn("{{", loaded.tier_format_example)


if __name__ == "__main__":
    unittest.main()
