from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterable

from app.config import AppConfig
from app.models import OutgoingComment, ScrapedComment


class CSVRepository:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def export_scraped_comments(
        self,
        comments: Iterable[ScrapedComment],
        output_path: Path | None = None,
    ) -> Path:
        target_path = output_path or self._build_default_export_path()
        if not target_path.is_absolute():
            target_path = self._config.project_root / target_path

        target_path.parent.mkdir(parents=True, exist_ok=True)

        with target_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
                    "comment_id",
                    "author_username",
                    "author_display_name",
                    "text",
                    "likes",
                    "published_at",
                ],
            )
            writer.writeheader()
            for comment in comments:
                writer.writerow(
                    {
                        "comment_id": comment.comment_id,
                        "author_username": comment.author_username,
                        "author_display_name": comment.author_display_name,
                        "text": comment.text,
                        "likes": comment.likes if comment.likes is not None else "",
                        "published_at": comment.published_at or "",
                    }
                )

        return target_path

    def load_outgoing_comments(self, csv_path: Path | None = None) -> list[OutgoingComment]:
        target_path = csv_path or self._config.default_outgoing_comments_csv
        if not target_path.is_absolute():
            target_path = self._config.project_root / target_path

        if not target_path.exists():
            raise FileNotFoundError(
                f"Outgoing comments CSV not found: {target_path}. "
                "Fill in data/comments/outgoing_comments.csv first."
            )

        comments: list[OutgoingComment] = []
        with target_path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            required_columns = {"video_url", "comment_text"}
            if not reader.fieldnames or not required_columns.issubset(reader.fieldnames):
                raise ValueError(
                    "Outgoing comments CSV must include: video_url, comment_text. "
                    "Optional: order, delay_seconds."
                )

            for index, row in enumerate(reader, start=1):
                video_url = (row.get("video_url") or "").strip()
                comment_text = (row.get("comment_text") or "").strip()
                if not video_url or not comment_text:
                    continue

                order_value = (row.get("order") or "").strip()
                delay_value = (row.get("delay_seconds") or "").strip()
                comments.append(
                    OutgoingComment(
                        order=int(order_value) if order_value else index,
                        video_url=video_url,
                        text=comment_text,
                        delay_seconds=int(delay_value)
                        if delay_value
                        else (0 if index == 1 else self._config.default_comment_delay_seconds),
                    )
                )

        comments.sort(key=lambda item: item.order)
        return comments

    def _build_default_export_path(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self._config.exports_dir / f"scraped_comments_{timestamp}.csv"
