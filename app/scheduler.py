import logging
import threading
import time
from datetime import datetime, timedelta

from . import runner, settings
from .config import TZ

log = logging.getLogger("scheduler")
next_run: datetime | None = None


def reschedule() -> None:
    global next_run
    s = settings.load()
    if not s.schedule_enabled:
        next_run = None
        return
    hour, minute = s.schedule_hm
    now = datetime.now(TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    next_run = target if target > now else target + timedelta(days=1)
    log.info("Next scheduled sync at %s", next_run.isoformat())


def _loop() -> None:
    reschedule()
    while True:
        time.sleep(15)
        if next_run and datetime.now(TZ) >= next_run:
            if not runner.run(None, "schedule"):
                log.warning("Skipped scheduled sync, another run is still in progress")
            reschedule()


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="scheduler").start()
