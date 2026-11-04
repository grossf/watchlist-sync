import json
import re
from dataclasses import asdict, dataclass, field, fields

from . import db
from .config import Source

USERNAME = re.compile(r"^[A-Za-z0-9_]+$")
TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SECRET_KEYS = ("password_hash", "session_secret")


@dataclass
class Settings:
    seerr_url: str = ""
    seerr_api_key: str = ""
    seerr_user_id: int | None = None
    request_4k: bool = False
    letterboxd_usernames: list[str] = field(default_factory=list)
    schedule_enabled: bool = True
    schedule_time: str = "04:00"
    dry_run: bool = True
    letterboxd_delay: float = 2.0
    log_retention_days: int = 30

    @property
    def schedule_hm(self) -> tuple[int, int]:
        hour, minute = self.schedule_time.split(":")
        return int(hour), int(minute)

    @property
    def sources(self) -> list[Source]:
        return [
            Source(key=f"watchlist:{u}", username=u, label=u, path=f"/{u}/watchlist/")
            for u in self.letterboxd_usernames
        ]

    def problems(self) -> list[str]:
        out = []
        if not self.seerr_url or not self.seerr_api_key:
            out.append("Seerr is not set up yet")
        if not self.letterboxd_usernames:
            out.append("No Letterboxd username added yet")
        return out


FIELDS = {f.name for f in fields(Settings)}


def load() -> Settings:
    stored = db.get_values()
    values = {k: json.loads(v) for k, v in stored.items() if k in FIELDS}
    return Settings(**values)


def secret(key: str) -> str | None:
    return db.get_values().get(key)


def set_secret(key: str, value: str | None) -> None:
    db.set_values({key: value})


def normalize_username(raw: str) -> str:
    value = raw.strip().rstrip("/")
    match = re.search(r"letterboxd\.com/([^/?#]+)", value)
    value = (match.group(1) if match else value).lstrip("@").lower()
    if not USERNAME.match(value):
        raise ValueError(f"{raw!r} is not a valid Letterboxd username")
    return value


def _int(value, name: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number")
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def validate(changes: dict) -> dict:
    out = {}
    for key, value in changes.items():
        if key not in FIELDS:
            raise ValueError(f"Unknown setting {key!r}")
        if key == "seerr_url":
            value = re.sub(r"/+(api/v1/?)?$", "", str(value).strip())
            if value and not re.match(r"^https?://[^/]+", value):
                raise ValueError("Seerr URL must start with http:// or https://")
        elif key == "seerr_api_key":
            value = str(value).strip()
            if not value:
                continue
        elif key == "seerr_user_id":
            value = _int(value, "Seerr user", 1, 10**9) if value not in (None, "", 0) else None
        elif key in ("request_4k", "schedule_enabled", "dry_run"):
            value = bool(value)
        elif key == "letterboxd_usernames":
            value = list(dict.fromkeys(normalize_username(u) for u in value if str(u).strip()))
        elif key == "schedule_time":
            match = TIME.match(str(value).strip())
            if not match:
                raise ValueError("Sync time must look like 04:00")
            value = f"{int(match.group(1)):02d}:{match.group(2)}"
        elif key == "letterboxd_delay":
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError("Letterboxd delay must be a number")
            if not 0.5 <= value <= 30:
                raise ValueError("Letterboxd delay must be between 0.5 and 30 seconds")
        elif key == "log_retention_days":
            value = _int(value, "Log retention", 1, 3650)
        out[key] = value
    return out


def save(changes: dict) -> Settings:
    clean = validate(changes)
    db.set_values({k: json.dumps(v) for k, v in clean.items()})
    return load()


def public(s: Settings) -> dict:
    data = asdict(s)
    data.pop("seerr_api_key")
    data["has_api_key"] = bool(s.seerr_api_key)
    data["password_enabled"] = bool(secret("password_hash"))
    return data
