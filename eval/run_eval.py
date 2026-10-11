"""Run every labeled test photo through the real verifier (quality check + Gemma vision) and write EVAL.md.

    python -m eval.run_eval [--labels eval/labels.csv] [--out EVAL.md]
"""
import argparse
import asyncio
import csv
import io
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from basecamp.app import config, gemma, verify

ROOT = Path(__file__).resolve().parent


async def judge(path: Path, row: dict) -> dict:
    t0 = time.perf_counter()
    jpeg, img = verify.clean_image(path.read_bytes())
    qual = verify.quality(Image.open(io.BytesIO(jpeg)), row["category"])
    if not qual["ok"]:
        return {"decision": "retake", "detail": qual["problem"], "seconds": time.perf_counter() - t0, "gemma": False}
    g = await verify.check_content(jpeg, {"prompt": row["prompt"], "verify": row["verify"]})
    decision = verify.decide_content(g)
    detail = g.get("error") or f"{g.get('what_i_see', '')} (conf {g.get('confidence', 0):.2f})"
    return {"decision": decision, "detail": detail, "seconds": time.perf_counter() - t0, "gemma": True}


def classify(expected: str, decision: str) -> str:
    if decision == "pending":
        return "error"
    if decision == "unsure":
        return "couldnt_tell"
    accepted = decision == "pass"
    if expected == "accept":
        return "correct_accept" if accepted else "false_reject"
    return "false_accept" if accepted else "correct_reject"


LABELS = {"correct_accept": "Correct accept", "correct_reject": "Correct reject", "false_accept": "False accept",
          "false_reject": "False reject", "couldnt_tell": "Couldn't tell", "error": "Error (Gemma unreachable)"}


def md_escape(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(ROOT / "labels.csv"))
    ap.add_argument("--photos", default=str(ROOT / "photos"))
    ap.add_argument("--out", default=str(ROOT.parent / "EVAL.md"))
    args = ap.parse_args()

    if not await gemma.available():
        print(f"Gemma ({config.GEMMA_MODEL}) isn't reachable at {config.OLLAMA_URL}. Start Ollama first.")
        return 1
    rows = list(csv.DictReader(open(args.labels, encoding="utf-8")))
    results, missing = [], []
    for row in rows:
        path = Path(args.photos) / row["file"]
        if not path.exists():
            missing.append(row["file"])
            continue
        r = await judge(path, row)
        r |= {"row": row, "outcome": classify(row["expected"], r["decision"])}
        results.append(r)
        print(f"{row['file']:28} expected={row['expected']:6} -> {r['decision']:7} {LABELS[r['outcome']]:16} {r['seconds']:.1f}s")
    if not results:
        print("No photos found. See eval/photos/README.md.")
        return 1

    counts = {k: sum(1 for r in results if r["outcome"] == k) for k in LABELS}
    gemma_times = [r["seconds"] for r in results if r["gemma"]]
    decided = [r for r in results if r["outcome"] not in ("couldnt_tell", "error")]
    accuracy = sum(r["outcome"].startswith("correct") for r in decided) / len(decided) if decided else 0
    lines = [
        "# Walk Journal — verifier evaluation",
        "",
        f"Model: `{config.GEMMA_MODEL}` via Ollama, run locally on {datetime.now():%Y-%m-%d %H:%M}. "
        f"{len(results)} labeled photos. Pass rule: Gemma `match=true` with confidence ≥ {verify.MIN_CONFIDENCE}; "
        "dark or blurry photos are sent back for a retake before Gemma sees them (counted as a reject).",
        "",
        "## Summary",
        "",
        "| Result | Count |",
        "|---|---|",
        *[f"| {LABELS[k]} | {v} |" for k, v in counts.items() if k != "error" or v],
        f"| **Accuracy on decided photos** | **{accuracy:.0%}** ({len(decided)} decided) |",
        f"| Average time per Gemma check | {sum(gemma_times) / len(gemma_times):.1f} s |" if gemma_times else "",
        "",
        "## Every photo",
        "",
        "| Photo | Task | Expected | Verifier said | Result | What Gemma saw | Time |",
        "|---|---|---|---|---|---|---|",
        *[f"| {md_escape(r['row']['file'])} | {md_escape(r['row']['prompt'])} | {r['row']['expected']} | {r['decision']} | "
          f"{LABELS[r['outcome']]} | {md_escape(r['detail'])} | {r['seconds']:.1f} s |" for r in results],
        "",
        "## Honest limits",
        "",
        "- Gemma can tell \"this is a lake\", not \"this is *that* lake\". The GPS radius check covers the place.",
        "- \"Couldn't tell\" is a feature: the app offers a retake or an honest self-confirm instead of guessing.",
        "- Phone clocks and GPS can be spoofed. This is a personal motivation app, not an anti-cheat system.",
    ]
    if missing:
        lines += ["", f"_Skipped {len(missing)} labeled photos that weren't in `eval/photos/`._"]
    Path(args.out).write_text("\n".join(l for l in lines if l is not None) + "\n", encoding="utf-8")
    print(f"\nWrote {args.out}: accuracy {accuracy:.0%} on {len(decided)} decided photos")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
