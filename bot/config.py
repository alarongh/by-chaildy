from dataclasses import dataclass
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str = ""
    admins: tuple[int, ...] = ()
    master: str = ""
    city: str = ""
    operator: str = ""
    address: str = ""
    privacy_contact: str = ""
    timezone: str = "Europe/Moscow"
    db_path: str = "data/chaildy.sqlite3"
    retention_days: int = 90
    privacy_ready: bool = False

    @classmethod
    def from_env(cls):
        load_dotenv()
        value = cls(
            token=os.getenv("BOT_TOKEN", "").strip(),
            admins=tuple(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()),
            master=os.getenv("MASTER_TELEGRAM", "").lstrip("@"),
            city=os.getenv("CITY", ""), operator=os.getenv("OPERATOR_NAME", ""),
            address=os.getenv("OPERATOR_ADDRESS", ""),
            privacy_contact=os.getenv("PRIVACY_CONTACT", ""),
            timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
            db_path=os.getenv("DB_PATH", "data/chaildy.sqlite3"),
            retention_days=int(os.getenv("RETENTION_DAYS", "90")),
            privacy_ready=os.getenv("PRIVACY_READY", "false").lower() == "true",
        )
        ZoneInfo(value.timezone)
        if not 1 <= value.retention_days <= 365:
            raise ValueError("RETENTION_DAYS must be between 1 and 365")
        return value

    @property
    def can_collect(self):
        return bool(self.privacy_ready and self.admins and self.operator.strip()
                    and self.address.strip() and self.privacy_contact.strip())
