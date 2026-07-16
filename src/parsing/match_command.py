from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")

_TIME_PATTERN = re.compile(
    r"^(?:(?P<ampm>오전|오후)\s*)?"
    r"(?P<korean_hour>\d{1,2})\s*시"
    r"(?:\s*(?P<korean_minute>\d{1,2})\s*분)?"
    r"|^(?P<colon_hour>\d{1,2}):(?P<colon_minute>\d{2})"
)


class CommandParseError(ValueError):
    pass


class PastTimeError(CommandParseError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedMatchCommand:
    starts_at: datetime
    tier_deadline_at: datetime
    lobby_at: datetime
    mode: str | None


class MatchCommandParser:
    def __init__(
        self,
        *,
        tier_deadline_offset_minutes: int,
        lobby_offset_minutes: int,
    ) -> None:
        self._tier_deadline_offset = timedelta(minutes=tier_deadline_offset_minutes)
        self._lobby_offset = timedelta(minutes=lobby_offset_minutes)

    def parse(
        self,
        arguments: str,
        *,
        now: datetime,
        default_mode: str | None = None,
    ) -> ParsedMatchCommand:
        source = arguments.strip()
        normalized = unicodedata.normalize("NFKC", source)
        match = _TIME_PATTERN.match(normalized)
        if match is None:
            raise CommandParseError(
                "시간을 인식할 수 없습니다. 예: 오후6시20분, 18:20, 23시"
            )

        hour, minute = self._extract_time(match)
        mode = source[match.end() :].strip() or (default_mode.strip() if default_mode else None)

        now_kst = now.astimezone(KST)
        starts_at = datetime(
            now_kst.year,
            now_kst.month,
            now_kst.day,
            hour,
            minute,
            tzinfo=KST,
        )
        result = self._build(starts_at, mode)
        if starts_at <= now_kst:
            raise PastTimeError("입력한 시각이 이미 지났습니다.")
        return result

    def _build(self, starts_at: datetime, mode: str | None) -> ParsedMatchCommand:
        return ParsedMatchCommand(
            starts_at=starts_at,
            tier_deadline_at=starts_at - self._tier_deadline_offset,
            lobby_at=starts_at - self._lobby_offset,
            mode=mode,
        )

    @staticmethod
    def _extract_time(match: re.Match[str]) -> tuple[int, int]:
        if match.group("colon_hour") is not None:
            hour = int(match.group("colon_hour"))
            minute = int(match.group("colon_minute"))
            if not 0 <= hour <= 23:
                raise CommandParseError("24시간 형식의 시는 0~23이어야 합니다.")
        else:
            hour = int(match.group("korean_hour"))
            minute = int(match.group("korean_minute") or 0)
            ampm = match.group("ampm")
            if ampm:
                if not 1 <= hour <= 12:
                    raise CommandParseError("오전/오후 형식의 시는 1~12이어야 합니다.")
                hour %= 12
                if ampm == "오후":
                    hour += 12
            elif not 0 <= hour <= 23:
                raise CommandParseError("시는 0~23이어야 합니다.")

        if not 0 <= minute <= 59:
            raise CommandParseError("분은 0~59이어야 합니다.")
        return hour, minute
