from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class RecruitmentCompleteCopy:
    start_heading: str = "내전 시작"
    tier_heading: str = "티어 작성"
    tier_instruction: str = "가능한 빠르게 아래 형식으로 작성해 주세요."
    tier_format_example: str = (
        "배틀태그\n"
        "탱커 / 딜러 / 힐러\n\n"
        "lemon#32146\n"
        "마4 / 마4! / 마4"
    )
    extra_notice: str = ""

    def normalized(self) -> RecruitmentCompleteCopy:
        values = {
            "start_heading": self.start_heading.strip(),
            "tier_heading": self.tier_heading.strip(),
            "tier_instruction": self.tier_instruction.strip(),
            "tier_format_example": self.tier_format_example.strip(),
            "extra_notice": self.extra_notice.strip(),
        }
        required = (
            "start_heading",
            "tier_heading",
            "tier_instruction",
            "tier_format_example",
        )
        if any(not values[field] for field in required):
            raise ValueError("필수 문구는 비워 둘 수 없습니다.")
        limits = {
            "start_heading": 50,
            "tier_heading": 50,
            "tier_instruction": 300,
            "tier_format_example": 800,
            "extra_notice": 500,
        }
        for field, maximum in limits.items():
            if len(values[field]) > maximum:
                raise ValueError(f"{field} 문구는 {maximum}자를 초과할 수 없습니다.")
        return RecruitmentCompleteCopy(**values)

    def to_mapping(self) -> dict[str, str]:
        normalized = self.normalized()
        return {
            "start_heading": normalized.start_heading,
            "tier_heading": normalized.tier_heading,
            "tier_instruction": normalized.tier_instruction,
            "tier_format_example": normalized.tier_format_example,
            "extra_notice": normalized.extra_notice,
        }


def recruitment_complete_copy_path(template_path: Path) -> Path:
    return template_path.with_name(f"{template_path.stem}.simple.yaml")


def load_recruitment_complete_copy(path: Path) -> RecruitmentCompleteCopy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RecruitmentCompleteCopy()
    if raw is None:
        return RecruitmentCompleteCopy()
    if not isinstance(raw, dict):
        raise ValueError("간단 편집 설정은 YAML 객체여야 합니다.")
    defaults = RecruitmentCompleteCopy()

    def text(field: str) -> str:
        value: Any = raw.get(field, getattr(defaults, field))
        if value is None:
            return ""
        return str(value)

    return RecruitmentCompleteCopy(
        start_heading=text("start_heading"),
        tier_heading=text("tier_heading"),
        tier_instruction=text("tier_instruction"),
        tier_format_example=text("tier_format_example"),
        extra_notice=text("extra_notice"),
    ).normalized()


def dump_recruitment_complete_copy(copy: RecruitmentCompleteCopy) -> str:
    return yaml.safe_dump(
        copy.to_mapping(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def build_recruitment_complete_template(copy: RecruitmentCompleteCopy) -> str:
    copy = copy.normalized()

    def literal(value: str) -> str:
        return "{{ " + json.dumps(value, ensure_ascii=False) + " }}"

    extra_notice = (
        f"\n\n{literal(copy.extra_notice)}" if copy.extra_notice else ""
    )
    return (
        f"**{literal(copy.start_heading)}** · {{{{ starts_at }}}} "
        f"({{{{ starts_in }}}})\n\n"
        f"**{literal(copy.tier_heading)}**\n"
        f"{{{{ tier_channel }}}}에 {literal(copy.tier_instruction)}\n\n"
        f"{literal(copy.tier_format_example)}\n\n"
        "**일정**\n"
        "**티어 작성 마감** · {{ tier_deadline }}\n"
        f"**대기실 입장** · {{{{ lobby_time }}}}"
        f"{extra_notice}\n\n"
        "🤝 {{ manner_notice }}\n"
    )
