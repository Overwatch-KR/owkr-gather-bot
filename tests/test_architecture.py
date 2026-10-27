"""업무 로직이 외부 구현을 직접 가져오지 못하게 의존성 경계를 검사한다."""

import ast
from importlib.util import resolve_name
from pathlib import Path

from src.adapters.discord.commands import GatherCog

ROOT = Path(__file__).resolve().parents[1] / "src"


def test_core_dependencies_point_to_contracts():
    forbidden = {
        "domain": (
            "src.application",
            "src.ports",
            "src.adapters",
            "src.infrastructure",
            "src.config",
        ),
        "ports": (
            "src.application",
            "src.adapters",
            "src.infrastructure",
            "src.config",
        ),
        "application": ("src.adapters", "src.infrastructure", "src.config"),
    }
    violations = []
    for layer, prefixes in forbidden.items():
        for path in (ROOT / layer).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        package = ".".join(path.parent.relative_to(ROOT.parent).parts)
                        module = resolve_name("." * node.level + module, package)
                    modules = [
                        module,
                        *(f"{module}.{alias.name}" for alias in node.names),
                    ]
                elif isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                else:
                    continue
                for module in modules:
                    if module.startswith(prefixes) or module.split(".")[0] in {
                        "discord",
                        "aiosqlite",
                    }:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: {module}"
                        )
    assert not violations, "\n".join(violations)


def test_event_handlers_are_registered_after_separating_the_cog():
    # 일반 mixin으로 옮긴 listener가 Discord 등록에서 빠지면 이벤트가 조용히 유실된다.
    names = {event for event, _ in GatherCog.__cog_listeners__}
    assert names == {
        "on_raw_reaction_add",
        "on_raw_reaction_remove",
        "on_message",
        "on_raw_message_edit",
        "on_raw_message_delete",
        "on_raw_bulk_message_delete",
    }
