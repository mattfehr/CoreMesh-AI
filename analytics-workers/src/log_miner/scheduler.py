"""UTC-by-default cron scheduling for the production log miner."""
from __future__ import annotations

import logging
from collections.abc import Callable

from .extractor import ProductionLogMiner
from .models import RunSummary


logger = logging.getLogger(__name__)


def run_scheduler(
    *,
    miner: ProductionLogMiner,
    cron: str = "0 2 * * *",
    timezone_name: str = "UTC",
    on_summary: Callable[[RunSummary], None] | None = None,
) -> None:
    """Run the miner on a five-field cron expression until interrupted."""

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    callback = on_summary or (lambda summary: None)

    def job() -> None:
        summary = miner.run_once()
        callback(summary)

    scheduler = BlockingScheduler(timezone=timezone_name)
    scheduler.add_job(
        job,
        CronTrigger.from_crontab(cron, timezone=timezone_name),
        id="production-log-miner",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    logger.info("log-miner scheduler started", extra={"cron": cron, "timezone": timezone_name})
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("log-miner scheduler stopped")
