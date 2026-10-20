from dataclasses import dataclass
import os
from pathlib import Path
import re


def load_env(path):
    """Read plain KEY=value settings without shell expansion or execution."""
    if path is None:
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise ValueError("Ayar dosyasında geçersiz satır var.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def boolean(key, default="false"):
    value = os.getenv(key, default).lower()
    if value not in {"true", "false"}:
        raise ValueError(key + " true veya false olmalı.")
    return value == "true"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    poll_seconds: int = 180
    poll_timeout_seconds: int = 8
    reconcile_seconds: int = 180
    timeout_seconds: int = 25
    retention_days: int = 90
    max_query_days: int = 7
    telegram_enabled: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_spacing: float = 1.2
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: str = "starttls"
    email_from: str = ""
    email_to: str = ""
    email_provider: str = "smtp"
    resend_api_key: str = ""
    smartmail_enabled: bool = False
    smartmail_rules_file: str = "config/smartmail_rules.json"
    smartmail_financial_context_file: str = ""
    smartmail_mode: str = "shadow"
    smartmail_details_enabled: bool = True
    smartmail_detail_streams: tuple = ("members",)
    smartmail_detail_spacing: float = 5.0
    smartmail_detail_daily_limit: int = 2000

    @classmethod
    def from_env(cls):
        result = cls(
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
            poll_seconds=int(os.getenv("POLL_SECONDS", "180")),
            poll_timeout_seconds=int(os.getenv("POLL_REQUEST_TIMEOUT_SECONDS", "8")),
            reconcile_seconds=int(os.getenv("POLL_RECONCILE_SECONDS", "180")),
            timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "25")),
            retention_days=int(os.getenv("RETENTION_DAYS", "90")),
            telegram_enabled=boolean("TELEGRAM_ENABLED"),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            telegram_spacing=float(os.getenv("TELEGRAM_SEND_INTERVAL_SECONDS", "1.2")),
            email_enabled=boolean("EMAIL_ENABLED"),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_tls=os.getenv("SMTP_TLS", "starttls"),
            email_from=os.getenv("EMAIL_FROM", ""),
            email_to=os.getenv("EMAIL_TO", ""),
            email_provider=os.getenv("EMAIL_PROVIDER", "smtp"),
            resend_api_key=os.getenv("RESEND_API_KEY", ""),
            smartmail_enabled=boolean("SMARTMAIL_ENABLED"),
            smartmail_rules_file=os.getenv("SMARTMAIL_RULES_FILE", "config/smartmail_rules.json"),
            smartmail_financial_context_file=os.getenv("SMARTMAIL_FINANCIAL_CONTEXT_FILE", ""),
            smartmail_mode=os.getenv("SMARTMAIL_MODE", "shadow"),
            smartmail_details_enabled=boolean("SMARTMAIL_DETAILS_ENABLED", "true"),
            smartmail_detail_streams=tuple(v.strip() for v in os.getenv("SMARTMAIL_DETAIL_STREAMS", "members").split(",") if v.strip()),
            smartmail_detail_spacing=float(os.getenv("SMARTMAIL_DETAIL_INTERVAL_SECONDS", "5")),
            smartmail_detail_daily_limit=int(os.getenv("SMARTMAIL_DETAIL_DAILY_LIMIT", "2000")),
        )
        if result.poll_seconds < 10:
            raise ValueError("POLL_SECONDS en az 10 olmalı; hızlı pilot için 15 önerilir.")
        if not 5 <= result.poll_timeout_seconds <= 120 or result.reconcile_seconds < 60:
            raise ValueError("Liste zaman aşımı 5–120 saniye, mutabakat aralığı en az 60 olmalı.")
        if not 5 <= result.timeout_seconds <= 120:
            raise ValueError("İstek zaman aşımı 5–120 saniye olmalı.")
        if result.retention_days < 7 or result.telegram_spacing < 1.1:
            raise ValueError("Saklama süresi en az 7 gün, Telegram aralığı en az 1.1 saniye olmalı.")
        if result.smartmail_mode not in {"shadow", "p0", "live"}:
            raise ValueError("SMARTMAIL_MODE shadow, p0 veya live olmalı.")
        if result.email_provider not in {"smtp", "resend"}:
            raise ValueError("EMAIL_PROVIDER smtp veya resend olmalı.")
        if not set(result.smartmail_detail_streams) <= {"members", "funds"} or not result.smartmail_detail_streams:
            raise ValueError("SmartMail detay kapsamı members/funds olmalı.")
        if not 2 <= result.smartmail_detail_spacing <= 300 or not 1 <= result.smartmail_detail_daily_limit <= 10000:
            raise ValueError("Detay aralığı 2–300 saniye, günlük sınır 1–10000 olmalı.")
        return result

    @property
    def destinations(self):
        result = {}
        if self.telegram_enabled:
            result["telegram"] = self.telegram_chat_id
        if self.email_enabled and not (self.smartmail_enabled and self.smartmail_mode == "shadow"):
            result["email"] = self.email_to
        return result

    def validate_delivery(self):
        if self.telegram_enabled:
            if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", self.telegram_token):
                raise ValueError("TELEGRAM_BOT_TOKEN eksik veya geçersiz.")
            if not re.fullmatch(r"-100\d+", self.telegram_chat_id):
                raise ValueError("Özel kanal için TELEGRAM_CHAT_ID -100 ile başlamalı.")
        if self.email_enabled:
            if not all([self.email_from, self.email_to]):
                raise ValueError("EMAIL_FROM ve EMAIL_TO gerekli.")
            if self.email_provider == "resend" and not self.resend_api_key:
                raise ValueError("RESEND_API_KEY gerekli.")
            if self.email_provider == "smtp" and not self.smtp_host:
                raise ValueError("SMTP_HOST gerekli.")
            if self.email_provider == "smtp" and self.smtp_tls not in {"starttls", "ssl"}:
                raise ValueError("SMTP_TLS starttls veya ssl olmalı.")
            if any(c in self.email_from + self.email_to for c in "\r\n"):
                raise ValueError("E-posta adreslerinde satır sonu olamaz.")
