import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))


@dataclass(frozen=True)
class Source:
    key: str
    username: str
    label: str
    path: str
    seerr_user_id: int | None = None


VERSION = os.environ.get("APP_VERSION", "dev").removeprefix("v")
GITHUB_URL = "https://github.com/grossf/watchlist-sync"
