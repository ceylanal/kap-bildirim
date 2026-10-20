"""Parse the public KAP page's own detail payload, with explicit schema checks."""
from datetime import datetime
from html.parser import HTMLParser
import json
import re

from .source import ISTANBUL, SourceError


class TurkishText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        excluded = ("content-en" in attrs.get("class", "").split() or
                    re.search(r"display\s*:\s*none", attrs.get("style", "")) is not None or
                    tag in {"script", "style"})
        if tag not in {"br", "hr", "img", "input", "meta", "link"}:
            self.stack.append((tag, excluded or (self.stack[-1][1] if self.stack else False)))
        if tag in {"tr", "p", "br", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in {"tr", "p", "td", "li"}:
            self.parts.append("\n" if tag != "td" else " ")

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)

    def text(self):
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        lines = [re.sub(r"\b[a-zA-Z0-9_]+\|", "", line).strip() for line in lines]
        return "\n".join(line for line in lines if line)


def flight_text_records(flight):
    """Walk length-prefixed text records; adjacent records need no newline."""
    encoded, offset, records = flight.encode("utf-8"), 0, {}
    while offset < len(encoded):
        match = re.match(rb"([a-fA-F0-9]+):T([a-fA-F0-9]+),", encoded[offset:])
        if match:
            start = offset + match.end()
            end = start + int(match.group(2), 16)
            if end > len(encoded):
                raise SourceError("detail_body_truncated")
            records[match.group(1).decode()] = encoded[start:end].decode("utf-8")
            offset = end
        else:
            end = encoded.find(b"\n", offset)
            offset = len(encoded) if end < 0 else end + 1
    return records


def parse_detail(page, index):
    try:
        text = page.decode("utf-8") if isinstance(page, bytes) else page
        chunks = []
        for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)</script>', text):
            value = json.loads(match.group(1))
            if len(value) > 1 and isinstance(value[1], str):
                chunks.append(value[1])
        flight = "".join(chunks)
        start = flight.find('"sendData":')
        if start < 0:
            raise SourceError("detail_schema_missing")
        data, _ = json.JSONDecoder().raw_decode(flight[start + len('"sendData":'):])
        basic = data["disclosure"]["disclosureBasic"]
        if type(basic.get("disclosureIndex")) is not int or basic["disclosureIndex"] != index:
            raise SourceError("detail_id_mismatch")
        detail = data["disclosure"].get("disclosureDetail") or {}
        blocked = bool(basic.get("isBlocked") or detail.get("blockedDescription"))
        if blocked:
            raise SourceError("detail_source_blocked", 3600)
        stamp = datetime.strptime(basic["publishDate"], "%Y.%m.%d %H:%M:%S").replace(tzinfo=ISTANBUL)
        body_parts = []
        text_records = flight_text_records(flight)
        for reference in data["disclosureBody"]:
            if not isinstance(reference, str):
                raise SourceError("detail_body_schema")
            if reference.startswith("<"):
                body_parts.append(reference)
                continue
            value = text_records.get(reference.lstrip("$"))
            if value is None:
                raise SourceError("detail_body_reference_missing")
            body_parts.append(value)
        parser = TurkishText()
        parser.feed("\n".join(body_parts))
        body = parser.text()
        if not body:
            raise SourceError("detail_body_empty")
        if len(body) > 400000:
            raise SourceError("detail_body_too_large", 3600)
        structured = {}
        if detail.get("opinionType") in {"AC", "OS", "OC", "PP"}:
            structured["opinionType"] = detail["opinionType"]
        return {"body": body, "structured_fields": structured,
                "issuer": basic["companyTitle"], "stock_code": basic.get("stockCode") or "",
                "subject": basic["title"], "summary": basic.get("summary") or "",
                "disclosure_index": index,
                "source_url": f"https://www.kap.org.tr/tr/Bildirim/{index}",
                "published_at": stamp.isoformat(), "attachments_parsed": False,
                "parser_version": "kap-public-flight-v1"}
    except SourceError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError):
        raise SourceError("detail_schema_invalid") from None
