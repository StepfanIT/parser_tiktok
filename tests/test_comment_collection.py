import json
import logging
import os
import random
import shutil
import sys
import types
import unittest
from dataclasses import replace
from pathlib import Path

playwright_module = types.ModuleType("playwright")
sync_api_module = types.ModuleType("playwright.sync_api")
sync_api_module.Browser = object
sync_api_module.BrowserContext = object
sync_api_module.Error = Exception
sync_api_module.Locator = object
sync_api_module.Page = object
sync_api_module.TimeoutError = TimeoutError
sync_api_module.sync_playwright = lambda: None
playwright_module.sync_api = sync_api_module
sys.modules.setdefault("playwright", playwright_module)
sys.modules.setdefault("playwright.sync_api", sync_api_module)

from app.config import load_app_config
from app.integrations.tiktok_client import TikTokPlaywrightClient
from app.models import (
    BrowserProviderConfig,
    OutgoingComment,
    RunAccountSummary,
    ScrapedComment,
    TikTokAccountConfig,
)
from app.repositories.account_repository import AccountRepository
from app.repositories.csv_repository import CSVRepository
from app.repositories.report_repository import ReportRepository
from app.services.send_policy import SendExecutionPolicy


class FakeResponse:
    def __init__(self, payload: dict, *, ok: bool = True, status: int = 200) -> None:
        self._payload = payload
        self.ok = ok
        self.status = status

    def json(self) -> dict:
        return self._payload


def build_client() -> TikTokPlaywrightClient:
    config = load_app_config()
    account = TikTokAccountConfig(
        name="main",
        storage_state_path=Path("data/accounts/test_storage_state.json"),
        user_data_dir=Path("data/accounts/test_user_data"),
        tiktok_username="brand_account",
    )
    return TikTokPlaywrightClient(config, logging.getLogger("test"), account)


class CommentCollectionTests(unittest.TestCase):
    def test_upsert_promotes_real_id_over_synthetic_dom_id(self) -> None:
        client = build_client()
        collected: dict[str, ScrapedComment] = {}

        dom_comment = ScrapedComment(
            video_url="https://example.com/video",
            comment_id="user123:0:Hello there",
            author_username="user123",
            author_display_name="User 123",
            text="Hello there",
        )
        api_comment = ScrapedComment(
            video_url="https://example.com/video",
            comment_id="987654321",
            author_username="user123",
            author_display_name="User 123",
            text="Hello there",
            likes=7,
            reply_author_usernames=("brand_account",),
        )

        client._upsert_scraped_comment(collected, dom_comment)
        client._upsert_scraped_comment(collected, api_comment)

        self.assertEqual(["987654321"], list(collected.keys()))
        self.assertEqual(7, collected["987654321"].likes)
        self.assertEqual(("brand_account",), collected["987654321"].reply_author_usernames)

    def test_upsert_keeps_distinct_real_ids_even_with_same_author_and_text(self) -> None:
        client = build_client()
        collected: dict[str, ScrapedComment] = {}

        first = ScrapedComment(
            video_url="https://example.com/video",
            comment_id="100",
            author_username="user123",
            author_display_name="User 123",
            text="Same text",
        )
        second = ScrapedComment(
            video_url="https://example.com/video",
            comment_id="101",
            author_username="user123",
            author_display_name="User 123",
            text="Same text",
        )

        client._upsert_scraped_comment(collected, first)
        client._upsert_scraped_comment(collected, second)

        self.assertEqual({"100", "101"}, set(collected.keys()))

    def test_load_app_config_uses_updated_daily_send_limits(self) -> None:
        config = load_app_config()

        self.assertEqual(120, config.send_behavior.daily_limit_min)
        self.assertEqual(150, config.send_behavior.daily_limit_max)

    def test_describe_response_marks_successful_publish(self) -> None:
        client = build_client()

        outcome = client._describe_response(
            FakeResponse({"status_code": 0, "status_msg": "ok"}, ok=True, status=200)
        )

        self.assertTrue(outcome.success)
        self.assertEqual("posted", outcome.status)
        self.assertEqual("status=0, message=ok", outcome.details)

    def test_describe_response_marks_rate_limited_publish(self) -> None:
        client = build_client()

        outcome = client._describe_response(
            FakeResponse({"status_code": 7, "status_msg": "Rate limit reached"}, ok=False, status=200)
        )

        self.assertFalse(outcome.success)
        self.assertEqual("rate_limited", outcome.status)

    def test_describe_response_marks_blocked_publish(self) -> None:
        client = build_client()

        outcome = client._describe_response(
            FakeResponse({"status_code": 9, "status_msg": "Account muted for policy violations"}, ok=False, status=200)
        )

        self.assertFalse(outcome.success)
        self.assertEqual("blocked", outcome.status)

    def test_browser_provider_config_reads_token_from_env(self) -> None:
        os_key = "TEST_DOLPHIN_TOKEN"
        previous = os.environ.get(os_key)
        os.environ[os_key] = "token-from-env"
        self.addCleanup(
            lambda: (os.environ.__setitem__(os_key, previous) if previous is not None else os.environ.pop(os_key, None))
        )

        config = BrowserProviderConfig(name="dolphin_anty", api_token_env=os_key)

        self.assertEqual("token-from-env", config.resolved_api_token())

    def test_send_policy_builds_batched_comments_with_randomized_delay(self) -> None:
        config = load_app_config()
        policy = SendExecutionPolicy(config)
        account = TikTokAccountConfig(
            name="main",
            storage_state_path=Path("data/accounts/test_storage_state.json"),
            user_data_dir=Path("data/accounts/test_user_data"),
            tiktok_username="brand_account",
        )
        state = policy.build_states([account], random.Random(7))[0]
        pending = [
            build_outgoing_comment(order=1),
            build_outgoing_comment(order=2),
            build_outgoing_comment(order=3),
        ]

        batch = policy.take_batch_for_account(pending, state, random.Random(9))

        self.assertTrue(batch)
        self.assertTrue(all(item.delay_seconds >= 2 for item in batch))
        self.assertLess(len(pending), 3)

    def test_report_repository_writes_json_report(self) -> None:
        config = load_app_config()
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        temp_root = config.logs_dir / "unit_test_reports"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(temp_root, ignore_errors=True))
        config = replace(
            config,
            logs_dir=temp_root,
            reports_dir=temp_root / "reports",
        )
        repository = ReportRepository(config)

        report_path = repository.write_run_report(
            action="health_check",
            summaries=[
                RunAccountSummary(
                    account_name="main",
                    provider_name="playwright_local",
                    health_status="passed",
                )
            ],
            notes=["unit test"],
        )

        self.assertTrue(report_path.exists())
        self.assertIn("run_report_health_check_", report_path.name)

    def test_account_repository_creates_nested_account_config_for_antidetect(self) -> None:
        config = load_app_config()
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        temp_accounts_dir = config.logs_dir / "unit_test_accounts_repo"
        shutil.rmtree(temp_accounts_dir, ignore_errors=True)
        temp_accounts_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(temp_accounts_dir, ignore_errors=True))
        repository = AccountRepository(replace(config, accounts_dir=temp_accounts_dir))

        config_path = repository.create_account_config(
            account_name="Creator 1",
            provider_name="dolphin_anty",
            profile_id="profile-123",
            api_url="http://127.0.0.1:3001",
        )

        self.assertEqual("account.json", config_path.name)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual("dolphin_anty", payload["browser_provider"]["name"])
        self.assertEqual("profile-123", payload["browser_provider"]["profile_id"])
        self.assertEqual("DOLPHIN_ANTY_TOKEN", payload["browser_provider"]["api_token_env"])
        self.assertIn("user_data", payload["user_data_dir"])

    def test_csv_repository_loads_target_username_and_text_variants(self) -> None:
        config = load_app_config()
        repository = CSVRepository(config)
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = config.logs_dir / "unit_test_csv_repo"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        csv_path = temp_dir / "outgoing.csv"
        csv_path.write_text(
            "\n".join(
                [
                    "video_url,comment_texts,target_username",
                    "https://example.com/video,\"Hi there|Hello again\",target_user",
                ]
            ),
            encoding="utf-8",
        )

        comments = repository.load_outgoing_comments(csv_path)

        self.assertEqual(1, len(comments))
        self.assertEqual("target_user", comments[0].target_username)
        self.assertEqual(("Hi there", "Hello again"), comments[0].text_variants)


def build_outgoing_comment(order: int) -> OutgoingComment:
    return OutgoingComment(
        order=order,
        video_url="https://example.com/video",
        text=f"comment {order}",
        delay_seconds=0,
    )


if __name__ == "__main__":
    unittest.main()
