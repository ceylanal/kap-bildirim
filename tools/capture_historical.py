"""Capture finite, explicitly labeled KAP pages with the production detail parser."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kapwatch.source import KapSource


def capture(index):
    detail = KapSource().fetch_detail(index)
    return {"case_id": "kap-" + str(index), "kind": "historical",
            "source_url": detail["source_url"], "captured_at": datetime.now(timezone.utc).isoformat(),
            "record": {"index": index, "stream": "members", "published_at": detail["published_at"],
                       "company": detail["issuer"], "code": detail["stock_code"],
                       "title": detail["subject"], "summary": detail["summary"], "blocked": False,
                       "raw_json": json.dumps({"smartmail": detail}, ensure_ascii=False)}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("output")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(capture, [item["index"] for item in manifest]))
    for snapshot, item in zip(snapshots, manifest):
        snapshot["expected"] = item["expected"]
        snapshot["label_basis"] = item["label_basis"]
    Path(args.output).write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in snapshots))
    print(f"Captured {len(snapshots)} labeled KAP snapshots; no notifications sent.")


if __name__ == "__main__":
    main()
