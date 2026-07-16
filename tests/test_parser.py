from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.parsing.match_command import (
    CommandParseError,
    MatchCommandParser,
    PastTimeError,
)


class MatchCommandParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = MatchCommandParser(
            tier_deadline_offset_minutes=30,
            lobby_offset_minutes=10,
        )
        self.now = datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)  # 12:00 KST

    def test_supported_formats(self) -> None:
        cases = {
            "오후6시20분 6ㄷ6클래식": (18, 20, "6ㄷ6클래식"),
            "오후2시20분": (14, 20, None),
            "18:20": (18, 20, None),
            "23시 6ㄷ6클래식": (23, 0, "6ㄷ6클래식"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                parsed = self.parser.parse(text, now=self.now)
                local = parsed.starts_at
                self.assertEqual((local.hour, local.minute, parsed.mode), expected)

    def test_mode_can_be_null_without_manager_default(self) -> None:
        parsed = self.parser.parse("18:20", now=self.now, default_mode=None)
        self.assertIsNone(parsed.mode)

    def test_manager_default_mode_is_used_when_omitted(self) -> None:
        parsed = self.parser.parse("18:20", now=self.now, default_mode="일반 내전")
        self.assertEqual(parsed.mode, "일반 내전")

    def test_past_time_is_rejected_without_next_day_proposal(self) -> None:
        with self.assertRaises(PastTimeError) as raised:
            self.parser.parse("11:30", now=self.now)
        self.assertEqual(str(raised.exception), "입력한 시각이 이미 지났습니다.")
        self.assertFalse(hasattr(raised.exception, "proposal"))

    def test_invalid_time_is_rejected(self) -> None:
        for text in ("24:00", "18:60", "오후13시", "시간없음"):
            with self.subTest(text=text), self.assertRaises(CommandParseError):
                self.parser.parse(text, now=self.now)
