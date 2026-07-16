from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from owkr_gather_bot.adapters.discord_adapter import allowed_mentions_for
from owkr_gather_bot.application.rendering import NotificationRenderer, RecruitmentTemplateRenderer
from owkr_gather_bot.domain.models import (
    NotificationKind,
    NotificationRecord,
    NotificationStatus,
)

from tests.helpers import make_config, make_session


class RenderingTest(unittest.TestCase):
    def test_completion_allows_only_first_ten_users(self) -> None:
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
        rendered = NotificationRenderer(make_config()).render(notification, session)
        self.assertEqual(rendered.allowed_user_ids, users)
        self.assertIn("<@10>", rendered.content)
        self.assertNotIn("<@11>", rendered.content)

        allowed = allowed_mentions_for(rendered.allowed_user_ids).to_dict()
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))
        self.assertEqual([int(user_id) for user_id in allowed["users"]], list(users))

    def test_recruitment_template_omits_null_mode(self) -> None:
        template = Path(__file__).resolve().parents[1] / "templates" / "recruitment.txt"
        session = make_session()
        content = RecruitmentTemplateRenderer(make_config(), template).render(
            session, make_config().manager(session.manager_user_id)
        )
        self.assertNotIn("모드:", content)
        self.assertNotIn("@everyone", content)

    def test_tier_window_upper_bound_is_exclusive(self) -> None:
        session = make_session(completion_notified_at=make_session().created_at)
        self.assertTrue(
            session.accepts_tier_activity_at(session.tier_deadline_at - timedelta(microseconds=1))
        )
        self.assertFalse(session.accepts_tier_activity_at(session.tier_deadline_at))
