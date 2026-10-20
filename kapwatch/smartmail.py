"""Deterministic SmartMail pilot. No LLM/network call belongs in this module."""
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import html
import json
from pathlib import Path
import re
import unicodedata

from .source import ISTANBUL, utf16_clip


PRIORITIES = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def normalize(text):
    text = unicodedata.normalize("NFC", html.unescape(str(text)))
    text = text.replace("İ", "i").replace("I", "ı").lower()
    text = text.translate(str.maketrans("ıışğüöç", "iisguoc"))
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def matches(text, phrase):
    # Final-token suffixes allow Turkish inflection and explicitly supplied stems.
    return re.search(r"(?<![a-z0-9])" + re.escape(normalize(phrase)), text) is not None


def sentences(text):
    text = html.unescape(re.sub(r"<[^>]*>", " ", str(text)))
    return [s.strip() for s in re.split(r"(?<!\d)[.!?;]\s+|\n+|\s+ancak\s+", text, flags=re.I) if s.strip()]


def decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def parse_amount(text):
    """Only one unambiguous currency amount; never pick the largest number."""
    pattern = r"(?<![\w.,])(\d+(?:[.,]\d+)*)(?:\s*(bin|milyon|milyar))?\s*(TL|TRY|USD|EUR|ABD\s+Doları|Euro|Türk\s+Lirası)(?!\w)"
    amounts = []
    for match in re.finditer(pattern, text, flags=re.I):
        raw, scale, currency = match.groups()
        if "," in raw:
            raw = raw.replace(".", "").replace(",", ".")
        elif "." in raw:
            if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
                raw = raw.replace(".", "")
            elif raw.count(".") > 1:
                continue
        value = decimal(raw)
        if value is None or value <= 0:
            continue
        value *= {None: 1, "bin": 1000, "milyon": 1000000,
                  "milyar": 1000000000}.get(scale.lower() if scale else None, 1)
        name = normalize(currency)
        currency = "TRY" if name in {"tl", "try", "turk lirasi"} else (
            "USD" if name in {"usd", "abd dolari"} else "EUR")
        amounts.append((value, currency))
    unique = set(amounts)
    return next(iter(unique)) if len(unique) == 1 else None


def event_status(text):
    groups = [
        ("denied", ["bulunmamaktadir", "soz konusu degildir", "gercegi yansitmamaktadir",
                    "iflas karari verilmesine yer olmadigina", "iptal edilmeyecektir"]),
        ("rejected", ["reddedil", "reddine", "talebinin reddi"]),
        ("cancelled", ["kaldiril", "sonlandiril", "sona er", "iptal edil"]),
        ("extended", ["uzatil", "uzatilm", "uzatilmasina"]),
        ("approved", ["verilmis", "verilmesine", "onaylan", "kabul edil"]),
        ("application", ["basvur", "talebinde bulun", "talepli"]),
        ("resolved", ["yeniden basla", "tekrar faaliyete", "odenmistir"]),
        ("decision", ["karar veril", "karari", "imzalan", "durdurul"]),
    ]
    for status, phrases in groups:
        if any(matches(text, phrase) for phrase in phrases):
            return status
    return "unknown"


def affected_role(text):
    for role, phrases in [
        ("customer", ["musterimiz", "musterimizin"]),
        ("supplier", ["tedarikcimiz", "tedarikcimizin"]),
        ("subsidiary", ["bagli ortakligimiz", "bagli ortakligimizin"]),
        ("affiliate", ["istirakimiz", "istirakimizin"]),
        ("counterparty", ["karsi taraf", "fon kullanicisi"]),
        ("issuer", ["sirketimiz", "sirketimize", "fabrikamiz", "tesisimiz", "borclarimiz"]),
    ]:
        if any(matches(text, phrase) for phrase in phrases):
            return role
    return "unknown"


@dataclass(frozen=True)
class Assessment:
    event: str
    label: str
    priority: str
    status: str
    affected_entity_role: str
    source_field: str
    source_sentence: str
    action: str
    manual_review: bool
    reason: str
    rules_revision: str
    materiality: dict

    def to_dict(self):
        return asdict(self)


class RuleEngine:
    def __init__(self, rules_path, financial_context_path=None):
        raw = Path(rules_path).read_bytes()
        self.config = json.loads(raw)
        self.revision = hashlib.sha256(raw).hexdigest()[:16]
        self.rules = self.config.get("rules", [])
        ids = set()
        for rule in self.rules:
            if (rule.get("priority") not in PRIORITIES or not rule.get("match_any")
                    or rule.get("id") in ids):
                raise ValueError("SmartMail kural kimliği/önceliği/ifadeleri geçersiz.")
            ids.add(rule["id"])
        settings = self.config.get("execution", {})
        if settings.get("p0_wait_for_llm", False) is not False:
            raise ValueError("P0 gönderimi LLM bekleyemez.")
        if settings.get("ratio_is_legal_threshold", False) is not False:
            raise ValueError("Önemlilik eşiği yasal eşik değildir.")
        self.threshold = decimal(settings.get("new_business_revenue_ratio_threshold", "0.20"))
        self.escalated_priority = settings.get("new_business_escalated_priority", "P1")
        if self.threshold is None or self.threshold <= 0 or self.escalated_priority not in {"P0", "P1"}:
            raise ValueError("SmartMail önemlilik parametresi geçersiz.")
        self.max_revenue_age = int(settings.get("max_revenue_age_days", 540))
        if self.max_revenue_age <= 0:
            raise ValueError("Finansal veri yaş sınırı pozitif olmalı.")
        self.financials = {}
        if financial_context_path:
            self.financials = json.loads(Path(financial_context_path).read_text())

    def materiality(self, record, sentence, fields):
        result = {"ratio": None, "threshold": str(self.threshold), "legal_threshold": False,
                  "escalated": False, "reason": "amount_missing_or_ambiguous"}
        provided = fields.get("contract_amount")
        amount = None
        scope = fields.get("consolidation_scope", "unknown")
        if isinstance(provided, dict) and provided.get("verified") is True:
            value = decimal(provided.get("value"))
            if value is not None and value > 0 and provided.get("currency") in {"TRY", "USD", "EUR"}:
                amount = value, provided["currency"]
                scope = provided.get("consolidation_scope", scope)
        else:
            amount = parse_amount(sentence)
            if amount and "kdv dahil" in normalize(sentence):
                result["reason"] = "tax_basis_requires_review"
                return result
        if not amount:
            return result
        result.update(amount=str(amount[0]), currency=amount[1], consolidation_scope=scope,
                      ratio_basis="total_contract_amount_to_annual_revenue")
        if scope not in {"consolidated", "standalone"}:
            result["reason"] = "consolidation_scope_unknown"
            return result
        current = datetime.fromisoformat(record.published_at).astimezone(ISTANBUL)
        eligible = []
        for revenue in self.financials.get("companies", {}).get(record.code, []):
            if revenue.get("verified") is not True or not revenue.get("source_url"):
                continue
            try:
                published = datetime.fromisoformat(revenue["published_at"])
                if published.tzinfo is None:
                    continue
                published = published.astimezone(ISTANBUL)
                start = datetime.fromisoformat(revenue["period_start"]).date()
                end = datetime.fromisoformat(revenue["period_end"]).date()
            except (KeyError, TypeError, ValueError):
                continue
            value = decimal(revenue.get("value"))
            if (published <= current and 360 <= (end - start).days <= 366
                    and 0 <= (current.date() - end).days <= self.max_revenue_age
                    and value is not None and value > 0
                    and revenue.get("currency") == amount[1]
                    and revenue.get("consolidation_scope") == scope):
                eligible.append((end, published, value, revenue))
        if not eligible:
            result["reason"] = "comparable_revenue_unavailable_as_of_event"
            return result
        _, _, revenue_value, revenue = max(eligible, key=lambda row: row[:2])
        ratio = amount[0] / revenue_value
        result.update(ratio=str(ratio), revenue=str(revenue_value),
                      revenue_period_end=revenue["period_end"],
                      revenue_source_url=revenue["source_url"],
                      escalated=ratio > self.threshold, reason="computed")
        return result

    def assess(self, record):
        try:
            raw = json.loads(record.raw_json)
        except ValueError:
            raw = {}
        # Explicit adapter data, never arbitrary JSON/boilerplate flattened into evidence.
        fields = raw.get("smartmail", {})
        if not isinstance(fields, dict):
            fields = {}
        sources = [("subject", record.title), ("summary", record.summary)]
        for name in ("body", "structured_fields", "attachments_if_parsed"):
            value = fields.get(name)
            if isinstance(value, str):
                sources.append((name, value))
            elif isinstance(value, dict):
                sources.extend((name + ":" + key, val) for key, val in value.items()
                               if isinstance(val, str))
        assessments = []
        prepared = [(field, sentence, normalize(sentence)) for field, content in sources
                    for sentence in sentences(content)]
        for rule in self.rules:
            candidates = []
            official_opinion = (fields.get("structured_fields") or {}).get("opinionType") if isinstance(fields.get("structured_fields"), dict) else None
            if rule["id"] == "audit_adverse_disclaimer" and official_opinion in {"AC", "OS"}:
                assessments.append(Assessment(rule["id"], rule["label"], "P0", "decision", "issuer",
                    "structured_fields:opinionType", "KAP denetim görüşü alanı (opinionType): " + official_opinion,
                    "flash", False, "official_audit_opinion_without_llm", self.revision, {}))
                continue
            for field, sentence, text in prepared:
                operation_pause = rule["id"] == "material_operations_halt" and (
                    matches(text, "uretime") and matches(text, "ara veril"))
                if operation_pause or any(matches(text, phrase) for phrase in rule["match_any"]):
                    candidates.append((field, sentence, text))
            if not candidates:
                continue
            # Prefer body evidence over generic subject templates and denial over title-only match.
            candidates.sort(key=lambda row: (row[0] == "subject", row[0] == "summary"))
            field, sentence, text = candidates[0]
            substantive = [row for row in candidates if row[0] != "subject"]
            if substantive:
                evidence = [row for row in substantive if row[0] != "summary"] or substantive
                affirmative = [row for row in evidence if event_status(row[2]) != "denied"]
                field, sentence, text = (affirmative or evidence)[0]
            status = event_status(text)
            role = fields.get("affected_entity_role") or affected_role(text)
            if role not in self.config["global_policy"]["affected_entity_roles"]:
                role = "unknown"
            priority, action, reason = rule["priority"], "digest", "keyword_context_review"
            review = status == "unknown" or role != "issuer"
            materiality = {}
            if status == "denied":
                priority, action, reason, review = "P3", "suppressed", "sentence_negated", False
            elif rule["id"] == "material_operations_halt" and any(
                matches(normalize(content), term) for _, content in sources if _ != "subject"
                for term in ["planli bakim", "planli izin", "yillik izin", "envanter sayimi", "mevsimsel"]
            ):
                # Downgrade only maintenance evidence; an independent unplanned stop survives.
                unplanned = [row for row in substantive if any(matches(row[2], term)
                    for term in ["plansiz", "yangin", "patlama", "finansal", "belirsiz sure"])]
                if unplanned:
                    field, sentence, text = unplanned[0]
                    status, role = event_status(text), affected_role(text)
                    review = status == "unknown" or role == "unknown"
                else:
                    priority, reason = "P2", "planned_maintenance"
            elif rule["id"] == "audit_adverse_disclaimer":
                evidence = normalize(sentence + " " + record.title)
                if not any(matches(evidence, term) for term in ["denetim rapor", "finansal tablo", "denetci"]):
                    priority, action, reason, review = "P3", "suppressed", "not_audit_opinion", False
            if action != "suppressed":
                if priority == "P0":
                    action, reason = "flash", "p0_without_llm"
                if rule["id"] == "material_new_business":
                    materiality = self.materiality(record, sentence, fields)
                    review = review or materiality["ratio"] is None
                    if materiality["escalated"] and role == "issuer":
                        priority = min((priority, self.escalated_priority), key=PRIORITIES.get)
                        action, reason = "flash", "material_contract"
                    elif materiality["escalated"]:
                        review = True
                        materiality["escalated"] = False
                        materiality["reason"] = "affected_entity_scope_requires_review"
            assessments.append(Assessment(rule["id"], rule["label"], priority, status, role,
                field, sentence, action, review, reason, self.revision, materiality))
        return sorted(assessments, key=lambda a: PRIORITIES[a.priority])


def flash_body(record, assessments):
    flash = [a for a in assessments if a.action == "flash"]
    first = flash[0]
    lines = [f"[{first.priority} KAP] {record.code or record.company} — {first.label}",
             record.company, ""]
    for assessment in flash:
        lines += ["Olay: " + assessment.label, "Statü: " + assessment.status,
                  "Etkilenen taraf: " + assessment.affected_entity_role,
                  "Kaynak cümle: " + utf16_clip(assessment.source_sentence, 1500)]
        if assessment.manual_review:
            lines.append("Manuel inceleme: gerekli; eşleşme kesin fiyat etkisi anlamına gelmez.")
        if assessment.materiality.get("ratio") is not None:
            pct = Decimal(assessment.materiality["ratio"]) * 100
            lines.append(f"Toplam sözleşme / yıllık ciro: %{pct:.2f} (sistem eşiği; yasal eşik değildir).")
    lines += ["", "AI yorumu beklenmeden oluşturuldu.", "Yayın: " + record.published_at,
              "Bildirim: " + str(record.index), record.link]
    return "\n".join(lines)
