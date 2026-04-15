from dataclasses import dataclass
from pathlib import Path

from app.models import SendBehaviorConfig


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    data_dir: Path
    accounts_dir: Path
    comments_dir: Path
    exports_dir: Path
    logs_dir: Path
    default_account_path: Path
    default_outgoing_comments_csv: Path
    send_behavior: SendBehaviorConfig
    default_comment_delay_seconds: int = 9
    default_scrape_scroll_rounds: int = 8
    default_scroll_pause_seconds: float = 1.5
    navigation_timeout_ms: int = 60_000
    browser_action_timeout_ms: int = 20_000


def load_app_config() -> AppConfig:
    project_root = Path(__file__).resolve().parent.parent
    data_dir = project_root / "data"
    accounts_dir = data_dir / "accounts"
    comments_dir = data_dir / "comments"
    exports_dir = project_root / "exports"
    logs_dir = project_root / "logs"

    return AppConfig(
        project_root=project_root,
        data_dir=data_dir,
        accounts_dir=accounts_dir,
        comments_dir=comments_dir,
        exports_dir=exports_dir,
        logs_dir=logs_dir,
        default_account_path=accounts_dir / "main_account.json",
        default_outgoing_comments_csv=comments_dir / "outgoing_comments.csv",
        send_behavior=SendBehaviorConfig(
            daily_limit_min=60,
            daily_limit_max=80,
            hourly_limit_min=12,
            hourly_limit_max=18,
            batch_size_min=5,
            batch_size_max=12,
            batch_pause_min_seconds=180,
            batch_pause_max_seconds=540,
            comment_delay_choices=(3, 5, 7, 9, 11),
        ),
    )
