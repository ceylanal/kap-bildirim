from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import re
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ISTANBUL = ZoneInfo("Europe/Istanbul")
SOURCE_URL = "https://www.kap.org.tr/tr/api/disclosure/list/main"
MEMBER_TYPES = ["IGS", "YK", "PYS", "DDK", "KVH", "DG"]
FUND_TYPES = ["BYF", "YF", "EYF", "OKS", "YYF", "VFF", "KFF", "GMF", "GSF", "PFF"]
STREAMS = ("members", "funds")


class SourceError(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def retry_after_seconds(value, now=None):
    if not value:
        return None
    if str(value).isdigit():
        return max(1, int(value))
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(1, int((parsed - current).total_seconds()) + 1)
    except (ValueError, TypeError, OverflowError):
        return None


def clean_text(value):
    if value is None:
        return ""
    if not isinstance(value, (str, int)):
        raise SourceError("schema_invalid_text")
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    return " ".join(text.split())


def utf16_clip(text, limit):
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    return text.encode("utf-16-le")[:(limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


@dataclass(frozen=True)
class Disclosure:
    index: int
    stream: str
    published_at: str
    company: str
    code: str
    title: str
    summary: str
    blocked: bool
    raw_json: str

    @property
    def link(self):
        return "https://www.kap.org.tr/tr/Bildirim/" + str(self.index)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.raw_json.encode()).hexdigest()

    def message(self):
        category = "Fon" if self.stream == "funds" else "Şirket / kurum"
        local = datetime.fromisoformat(self.published_at).astimezone(ISTANBUL)
        lines = [
            "KAP | " + category + " | " + utf16_clip(self.code or "Kod yok", 100),
            utf16_clip(self.company, 500),
            utf16_clip(self.title, 700),
        ]
        if self.summary and self.summary != self.title:
            lines += ["", utf16_clip(self.summary, 2000)]
        lines += ["", "Yayın: " + local.strftime("%d.%m.%Y %H:%M:%S") + " (Türkiye)",
                  "Bildirim: " + str(self.index), self.link]
        return "\n".join(lines)


def parse_records(data, stream, start, end):
    """Validate the complete batch before any cursor or record is committed."""
    if stream not in STREAMS or not isinstance(data, list):
        raise SourceError("schema_expected_list")
    records = {}
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("disclosureBasic"), dict):
            raise SourceError("schema_missing_disclosureBasic")
        basic = item["disclosureBasic"]
        index = basic.get("disclosureIndex")
        if isinstance(index, bool) or not isinstance(index, int) or index <= 0:
            raise SourceError("schema_invalid_id")
        try:
            published = datetime.strptime(basic["publishDate"], "%d.%m.%Y %H:%M:%S").replace(tzinfo=ISTANBUL)
        except (KeyError, ValueError, TypeError):
            raise SourceError("schema_invalid_publish_date") from None
        if not start <= published.date() <= end:
            raise SourceError("schema_date_outside_query")
        company, title = clean_text(basic.get("companyTitle")), clean_text(basic.get("title"))
        if not company or not title:
            raise SourceError("schema_missing_company_or_title")
        blocked = basic.get("isBlocked")
        if blocked is not None and not isinstance(blocked, bool):
            raise SourceError("schema_invalid_blocked")
        records[index] = Disclosure(
            index=index, stream=stream, published_at=published.isoformat(),
            company=company, code=clean_text(basic.get("stockCode")), title=title,
            summary=clean_text(basic.get("summary")), blocked=bool(blocked),
            raw_json=json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    return sorted(records.values(), key=lambda r: (r.published_at, r.index))


def query_window(state, now, max_days=7):
    today = now.astimezone(ISTANBUL).date()
    previous = state.get("covered_through")
    start = datetime.fromisoformat(previous).date() - timedelta(days=1) if previous else today - timedelta(days=1)
    end = min(start + timedelta(days=max_days - 1), today)
    return start, end


class KapSource:
    def __init__(self, timeout=25, list_timeout=None):
        self.timeout = timeout
        self.list_timeout = timeout if list_timeout is None else list_timeout

    def fetch_detail(self, index):
        from .detail import parse_detail
        if type(index) is not int or index <= 0:
            raise ValueError("Geçerli KAP bildirim numarası gerekli.")
        request = urllib.request.Request(f"https://www.kap.org.tr/tr/Bildirim/{index}",
            headers={"User-Agent": "kap-bildirim-pilot/0.1", "Accept-Language": "tr"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(5 * 1024 * 1024 + 1)
                if len(raw) > 5 * 1024 * 1024:
                    raise SourceError("detail_response_too_large", 3600)
        except urllib.error.HTTPError as exc:
            wait = retry_after_seconds(exc.headers.get("Retry-After"))
            if exc.code in {401, 403, 666}:
                wait = max(wait or 0, 3600)
            raise SourceError("detail_http_" + str(exc.code), wait) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SourceError("detail_transport_error") from None
        return parse_detail(raw, index)

    def fetch(self, stream, start, end):
        payload = {
            "fromDate": start.strftime("%d.%m.%Y"), "toDate": end.strftime("%d.%m.%Y"),
            "disclosureTypes": None, "mkkMemberOid": None,
        }
        if stream == "members":
            payload["memberTypes"] = MEMBER_TYPES
        elif stream == "funds":
            payload["fundTypes"] = FUND_TYPES
        else:
            raise ValueError("Unknown stream")
        request = urllib.request.Request(
            SOURCE_URL, data=json.dumps(payload).encode(), method="POST",
            headers={"User-Agent": "kap-bildirim-pilot/0.1", "Content-Type": "application/json",
                     "Accept": "application/json", "Accept-Language": "tr"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.list_timeout) as response:
                raw = response.read(20 * 1024 * 1024 + 1)
                if len(raw) > 20 * 1024 * 1024:
                    raise SourceError("response_too_large")
                data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            wait = retry_after_seconds(exc.headers.get("Retry-After"))
            if exc.code in {401, 403, 666}:
                wait = max(wait or 0, 3600)
            raise SourceError("http_" + str(exc.code), wait) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SourceError("transport_error") from None
        except (ValueError, UnicodeError):
            raise SourceError("invalid_json_or_access_page", 3600) from None
        return parse_records(data, stream, start, end)
