from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2.exceptions import SecurityError

from src.adapters.discord_adapter import (
    allowed_mentions_for,
    build_recruitment_embed,
)
from src.application.rendering import NotificationRenderer, RecruitmentTemplateRenderer
from src.domain.models import (
    NotificationKind,
    NotificationRecord,
    NotificationStatus,
)

from tests.helpers import make_config, make_session


RECRUITMENT_COMPLETE_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "recruitment_complete.txt"
)


class RenderingTest(unittest.TestCase):
    def test_completion_does_not_mention_participants(self) -> None:
        session = make_session()
        users = tuple(range(1, 11))
        notification = NotificationRecord(
            id=1,
            match_id=session.id,
            kind=NotificationKind.RECRUITMENT_COMPLETE,
            channel_id=session.announcement_channel_id,
            payload={"user_ids": list(users)},
            dedupe_key="complete",
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=session.created_at,
        )
        rendered = NotificationRenderer(
            make_config(), RECRUITMENT_COMPLETE_TEMPLATE
        ).render(notification, session)
        self.assertEqual(rendered.allowed_user_ids, ())
        self.assertNotIn("<@1>", rendered.content)
        self.assertNotIn("<@10>", rendered.content)
        self.assertNotIn("<@11>", rendered.content)

        allowed = allowed_mentions_for(rendered.allowed_user_ids).to_dict()
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))
        self.assertNotIn("users", allowed)

    def test_recruitment_template_omits_null_mode(self) -> None:
        template = Path(__file__).resolve().parents[1] / "templates" / "recruitment.txt"
        session = make_session()
        content = RecruitmentTemplateRenderer(make_config(), template).render(
            session, make_config().manager(session.manager_user_id)
        )
        embed = build_recruitment_embed(make_config(), content)
        self.assertNotIn("🎮 모드", [field.name for field in embed.fields])
        self.assertNotIn("@everyone", content)

    def test_recruitment_uses_native_discord_timestamps(self) -> None:
        template = Path(__file__).resolve().parents[1] / "templates" / "recruitment.txt"
        config = make_config()
        session = make_session()
        recruitment = RecruitmentTemplateRenderer(config, template).render(
            session, config.manager(session.manager_user_id)
        )
        embed = build_recruitment_embed(config, recruitment)
        embed_payload = embed.to_dict()
        embed_text = str(embed_payload)
        starts_at = int(session.starts_at.timestamp())
        tier_deadline_at = int(session.tier_deadline_at.timestamp())
        lobby_at = int(session.lobby_at.timestamp())
        self.assertEqual(embed.title, "내전 모집")
        self.assertIn(f"<t:{starts_at}:F>", embed_text)
        self.assertIn(f"<t:{starts_at}:R>", embed_text)
        self.assertIn(f"<t:{tier_deadline_at}:t>", embed_text)
        self.assertIn(f"<t:{lobby_at}:t>", embed_text)
        self.assertNotIn("7월", embed_text)
        self.assertIn(f"첫 **{session.participant_limit}명**까지 확정", embed.description)
        self.assertEqual(
            embed.footer.text,
            config.messages.manner_notice,
        )
        self.assertIn(f"<#{session.tier_channel_id}>", embed.description)

        notification = NotificationRecord(
            id=1,
            match_id=session.id,
            kind=NotificationKind.RECRUITMENT_COMPLETE,
            channel_id=session.announcement_channel_id,
            payload={"user_ids": list(range(1, 11))},
            dedupe_key="complete-time-display",
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=session.created_at,
        )
        completion = NotificationRenderer(
            config, RECRUITMENT_COMPLETE_TEMPLATE
        ).render(notification, session).content
        self.assertNotIn("내전 코드", completion)
        self.assertNotIn(session.match_code, completion)
        self.assertIn(f"<t:{starts_at}:F>", completion)
        self.assertIn(f"<t:{starts_at}:R>", completion)
        self.assertIn(f"**티어 작성 마감** · <t:{tier_deadline_at}:t>", completion)
        self.assertIn(f"**대기실 입장** · <t:{lobby_at}:t>", completion)
        self.assertIn("대기실 1번", completion)

    def test_lobby_reminder_uses_session_lobby_assignment(self) -> None:
        session = make_session()
        session.lobby_voice_channel_id = 106
        session.lobby_name = "대기실 2번"
        notification = NotificationRecord(
            id=1,
            match_id=session.id,
            kind=NotificationKind.LOBBY_REMINDER,
            channel_id=session.announcement_channel_id,
            payload={"user_ids": [1, 2]},
            dedupe_key="lobby-two",
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=session.lobby_at,
        )

        rendered = NotificationRenderer(
            make_config(), RECRUITMENT_COMPLETE_TEMPLATE
        ).render(notification, session)

        self.assertEqual(rendered.voice_channel_id, 106)
        self.assertIn("대기실 2번", rendered.content)
        self.assertNotIn(session.match_code, rendered.content)

    def test_tier_missing_reminder_mentions_only_payload_users(self) -> None:
        session = make_session()
        notification = NotificationRecord(
            id=1,
            match_id=session.id,
            kind=NotificationKind.TIER_MISSING_REMINDER,
            channel_id=session.announcement_channel_id,
            payload={"user_ids": [1, 2]},
            dedupe_key="tier-missing",
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=session.tier_deadline_at,
        )

        rendered = NotificationRenderer(
            make_config(), RECRUITMENT_COMPLETE_TEMPLATE
        ).render(notification, session)

        self.assertEqual(rendered.allowed_user_ids, (1, 2))
        self.assertIn("<@1> <@2>", rendered.content)
        self.assertIn(
            f"<#{session.tier_channel_id}>에 티어 작성이 완료되지 않았습니다. "
            "한번 더 체크 부탁드립니다.",
            rendered.content,
        )
        self.assertNotIn("관리자 안내에 따라", rendered.content)

    def test_substitute_notice_mentions_only_selected_user(self) -> None:
        session = make_session()
        notification = NotificationRecord(
            id=1,
            match_id=session.id,
            kind=NotificationKind.SUBSTITUTE_RECRUITED,
            channel_id=session.announcement_channel_id,
            payload={"user_ids": [20]},
            dedupe_key="substitute",
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=session.created_at,
        )

        rendered = NotificationRenderer(
            make_config(),
            RECRUITMENT_COMPLETE_TEMPLATE,
        ).render(notification, session)

        self.assertEqual(rendered.allowed_user_ids, (20,))
        self.assertIn("<@20>", rendered.content)
        self.assertIn("대타 대기열에 등록되었습니다", rendered.content)
        self.assertIn(f"<#{session.tier_channel_id}>", rendered.content)
        self.assertIn("답장으로 티어를 작성해 주세요", rendered.content)
        allowed = allowed_mentions_for(rendered.allowed_user_ids).to_dict()
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))

    def test_lobby_reminder_can_be_rebuilt_for_missing_users_only(self) -> None:
        session = make_session()
        notification = NotificationRecord(
            id=1,
            match_id=session.id,
            kind=NotificationKind.LOBBY_REMINDER,
            channel_id=session.announcement_channel_id,
            payload={"user_ids": [1, 2, 3]},
            dedupe_key="lobby",
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=session.lobby_at,
        )

        rendered = NotificationRenderer(
            make_config(), RECRUITMENT_COMPLETE_TEMPLATE
        ).render(notification, session)
        filtered = rendered.with_allowed_user_ids((2, 3))

        self.assertEqual(rendered.voice_channel_id, 105)
        self.assertEqual(filtered.allowed_user_ids, (2, 3))
        self.assertNotIn("<@1>", filtered.content)
        self.assertIn("<@2> <@3>", filtered.content)
        self.assertIn("대기실 1번", filtered.content)

    def test_tier_announcement_omits_public_match_code(self) -> None:
        session = make_session()
        notification = NotificationRecord(
            id=1,
            match_id=session.id,
            kind=NotificationKind.TIER_ANCHOR,
            channel_id=session.tier_channel_id,
            payload={"recreated": False},
            dedupe_key="tier-anchor",
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=session.created_at,
        )

        rendered = NotificationRenderer(
            make_config(), RECRUITMENT_COMPLETE_TEMPLATE
        ).render(notification, session)

        self.assertNotIn("`A7K2`", rendered.content)
        self.assertNotIn("내전 코드", rendered.content)
        self.assertIn("이 메시지에 답장", rendered.content)
        self.assertIn("**배틀태그 / 탱커 / 딜러 / 힐러**", rendered.content)
        self.assertIn("골5? / 실2 / 플3!", rendered.content)
        self.assertIn("`!` 자신 있거나 선호하는 포지션", rendered.content)
        self.assertIn("`?` 자신 없거나 거의 하지 않는 포지션", rendered.content)
        self.assertIn("`X` 마이크·브리핑이 어려우면 맨 끝", rendered.content)
        self.assertIn("현재 티어가 3단계 이상 낮으면", rendered.content)
        self.assertIn("부계정은 하나로 통합", rendered.content)
        self.assertIn(f"<@{session.manager_user_id}>", rendered.content)
        self.assertEqual(rendered.allowed_user_ids, ())
        allowed = allowed_mentions_for(rendered.allowed_user_ids).to_dict()
        self.assertNotIn("users", allowed)

    def test_recruitment_complete_template_replaces_custom_variables(self) -> None:
        session = make_session()
        notification = NotificationRecord(
            id=1,
            match_id=session.id,
            kind=NotificationKind.RECRUITMENT_COMPLETE,
            channel_id=session.announcement_channel_id,
            payload={"user_ids": [1, 2]},
            dedupe_key="custom-complete",
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=session.created_at,
        )
        with TemporaryDirectory() as directory:
            template = Path(directory) / "complete.txt"
            template.write_text(
                "{{ user }} | {{ tier_channel }} | {{ tier_deadline }} | "
                "{{ lobby_time }} | {{ participant_count }}명",
                encoding="utf-8",
            )
            rendered = NotificationRenderer(make_config(), template).render(
                notification, session
            )

        self.assertNotIn("<@1>", rendered.content)
        self.assertNotIn("<@2>", rendered.content)
        self.assertIn(f"<#{session.tier_channel_id}>", rendered.content)
        self.assertIn("| 2명", rendered.content)
        self.assertEqual(rendered.allowed_user_ids, ())

    def test_recruitment_complete_template_reloads_after_file_edit(self) -> None:
        session = make_session()
        notification = NotificationRecord(
            id=1,
            match_id=session.id,
            kind=NotificationKind.RECRUITMENT_COMPLETE,
            channel_id=session.announcement_channel_id,
            payload={"user_ids": [1, 2]},
            dedupe_key="reload-complete",
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=session.created_at,
        )
        with TemporaryDirectory() as directory:
            template = Path(directory) / "complete.txt"
            template.write_text("첫 문구 {{ user }}", encoding="utf-8")
            renderer = NotificationRenderer(make_config(), template)
            first = renderer.render(notification, session)
            template.write_text("수정 문구 {{ tier_channel }}", encoding="utf-8")
            second = renderer.render(notification, session)

        self.assertEqual(first.content, "첫 문구")
        self.assertIn(f"수정 문구 <#{session.tier_channel_id}>", second.content)

    def test_recruitment_complete_preview_uses_sample_schedule(self) -> None:
        now = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)
        rendered = NotificationRenderer(
            make_config(),
            RECRUITMENT_COMPLETE_TEMPLATE,
        ).render_recruitment_complete_preview(
            user_ids=(200,),
            now=now,
        )
        starts_at = now + timedelta(hours=1)

        self.assertNotIn("<@200>", rendered.content)
        self.assertIn(f"<t:{int(starts_at.timestamp())}:F>", rendered.content)
        self.assertIn("<#103>", rendered.content)
        self.assertEqual(rendered.allowed_user_ids, ())

    def test_recruitment_complete_template_rejects_unsafe_object_access(self) -> None:
        with self.assertRaises(SecurityError):
            NotificationRenderer.validate_recruitment_complete_template(
                "{{ cycler.__init__.__globals__ }}",
                make_config(),
            )

    def test_recruitment_role_is_the_only_allowed_role_mention(self) -> None:
        allowed = allowed_mentions_for([], [999]).to_dict()

        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))
        self.assertEqual([int(role_id) for role_id in allowed["roles"]], [999])

    def test_template_mentions_are_present_as_text_but_never_allowed_to_ping(self) -> None:
        template = Path(__file__).resolve().parents[1] / "templates" / "recruitment.txt"
        config = make_config()
        config = replace(
            config,
            messages=replace(
                config.messages,
                participation_notice="@everyone @here <@&999> ✅ 반응",
                manner_notice="@everyone <@&999> 매너 게임",
            ),
        )
        content = RecruitmentTemplateRenderer(config, template).render(
            make_session(), config.manager(200)
        )
        self.assertIn("@everyone", content)
        self.assertIn("@here", content)
        self.assertIn("<@&999>", content)

        allowed = allowed_mentions_for([]).to_dict()
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))
        self.assertNotIn("users", allowed.get("parse", []))

    def test_tier_window_upper_bound_is_exclusive(self) -> None:
        session = make_session(completion_notified_at=make_session().created_at)
        self.assertTrue(
            session.accepts_tier_activity_at(session.tier_deadline_at - timedelta(microseconds=1))
        )
        self.assertFalse(session.accepts_tier_activity_at(session.tier_deadline_at))
