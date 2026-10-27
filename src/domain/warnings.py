from dataclasses import dataclass
from enum import StrEnum


class WarningReason(StrEnum):
    NO_SHOW = "노쇼"
    BAD_MANNERS = "비매너"


@dataclass(frozen=True, slots=True)
class WarningSummary:
    no_show: int = 0
    bad_manners: int = 0

    @property
    def total(self) -> int:
        return self.no_show + self.bad_manners
