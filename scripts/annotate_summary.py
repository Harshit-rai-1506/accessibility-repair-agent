"""Print the latest evaluation summaries as GitHub Actions notices.

Notices appear on the workflow run's page (and through GitHub's API), so the
headline numbers can be read without downloading the run's files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="accessguru")
    parser.add_argument("--split", default="dev")
    args = parser.parse_args()
    base = ROOT / "runs" / f"{args.dataset}-{args.split}"
    for path in sorted(base.glob("*/summary.json")):
        s = json.loads(path.read_text(encoding="utf-8"))
        a, e = s["all"], s["element_level"]
        print(
            f"::notice title={s['method']} / {s['model']} / {args.dataset}-{args.split}::"
            f"items {s['items_done']}/{s['items_total']}; score decrease {a['score_decrease']} "
            f"(element-level {e['score_decrease']}); resolved {a['resolved']}/{a['violations']}; "
            f"broke something {a['introduced_new_violations']}; dropped content {s['dropped_content']}; "
            f"status {json.dumps(s['status'])}; tokens/item {s['tokens_per_item']}; "
            f"paid cost ${s.get('paid_cost_usd')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
