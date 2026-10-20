"""Offline labeled replay. Does not initialize a Store, sender or API client."""
from dataclasses import replace
import json
from pathlib import Path

from .source import Disclosure


def metrics(pairs):
    tp = sum(expected and actual for expected, actual in pairs)
    fp = sum(not expected and actual for expected, actual in pairs)
    fn = sum(expected and not actual for expected, actual in pairs)
    tn = sum(not expected and not actual for expected, actual in pairs)
    def rate(n, d):
        return n / d if d else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "false_positive_rate": rate(fp, fp + tn),
            "false_alarm_share": rate(fp, tp + fp),
            "critical_miss_rate": rate(fn, tp + fn),
            "precision": rate(tp, tp + fp), "recall": rate(tp, tp + fn)}


def replay(path, engine, title_summary_only=False):
    groups, cases = {}, []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        label = item.get("expected", {})
        if type(label.get("critical")) is not bool or type(label.get("flash")) is not bool:
            raise ValueError(f"Satır {number}: critical ve flash insan etiketi gerekli.")
        kind = item.get("kind")
        if kind not in {"historical", "synthetic"}:
            raise ValueError(f"Satır {number}: historical/synthetic türü gerekli.")
        record = Disclosure(**item["record"])
        if title_summary_only:
            record = replace(record, raw_json="{}")
        result = engine.assess(record) if not record.blocked else []
        critical = any(a.priority == "P0" and a.action == "flash" for a in result)
        flash = any(a.action == "flash" for a in result)
        bucket = groups.setdefault(kind, {"critical": [], "flash": []})
        bucket["critical"].append((label["critical"], critical))
        bucket["flash"].append((label["flash"], flash))
        cases.append({"case_id": item["case_id"], "kind": kind,
                      "source_url": item.get("source_url"), "expected": label,
                      "actual_critical": critical, "actual_flash": flash,
                      "manual_review": any(a.manual_review for a in result),
                      "assessments": [a.to_dict() for a in result]})
    return {"rules_revision": engine.revision, "sample_size": len(cases),
            "input_mode": "title_summary_only" if title_summary_only else "provided_fields",
            "not_a_representative_population_estimate": True,
            "metrics_by_dataset": {kind: {name: metrics(pairs) for name, pairs in values.items()}
                                   for kind, values in groups.items()}, "cases": cases}
