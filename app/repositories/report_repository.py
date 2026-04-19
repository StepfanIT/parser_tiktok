from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.config import AppConfig
from app.models import RunAccountSummary


class ReportRepository:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def write_run_report(
        self,
        *,
        action: str,
        summaries: Iterable[RunAccountSummary],
        notes: Iterable[str] = (),
    ) -> Path:
        self._config.reports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        report_path = self._config.reports_dir / f"run_report_{action}_{timestamp}.json"
        payload = {
            "action": action,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "notes": list(notes),
            "accounts": [asdict(summary) for summary in summaries],
        }
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return report_path
