#!/usr/bin/env python3
"""
Singapore Pools 4D updater / verifier.

Examples:
  python update_4d.py --csv 4d_prizes_updated_20260902.csv --verify 5528 5529 5530
  python update_4d.py --csv 4d_prizes_updated_20260902.csv
  python update_4d.py --csv 4d_prizes.csv

The script:
- fetches official Singapore Pools result pages by draw number
- validates 1st/2nd/3rd + 10 Starter + 10 Consolation = 23 numbers
- can compare fetched draws against draws already in your CSV
- appends only newer draws
- preserves leading zeroes
- refuses to overwrite suspicious/incomplete data
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RESULTS_URL = "https://www.singaporepools.com.sg/en/product/pages/4d_results.aspx?sppl={token}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)


def draw_url(draw_no: int) -> str:
    token = base64.b64encode(f"DrawNumber={draw_no}".encode("ascii")).decode("ascii")
    return RESULTS_URL.format(token=token)


def fetch_html(draw_no: int, timeout: int = 30) -> str:
    req = Request(
        draw_url(draw_no),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-SG,en;q=0.9",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except (HTTPError, URLError, TimeoutError) as e:
        raise RuntimeError(f"Could not fetch draw {draw_no}: {e}") from e

    # Singapore Pools pages are UTF-8 in practice; replacement keeps parser robust.
    return raw.decode("utf-8", errors="replace")


def html_to_text(page: str) -> str:
    # Remove scripts/styles first so embedded JS numbers do not contaminate extraction.
    page = re.sub(r"(?is)<script\b.*?</script>", " ", page)
    page = re.sub(r"(?is)<style\b.*?</style>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    page = html.unescape(page)
    return re.sub(r"\s+", " ", page).strip()


def parse_draw(draw_no: int, page: str) -> tuple[str, list[str]]:
    text = html_to_text(page)

    marker = re.search(
        rf"4D Results.*?(?P<date>(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{{2}}\s+[A-Z][a-z]{{2}}\s+\d{{4}})"
        rf"\s*\|\s*Draw No\.\s*{draw_no}\b",
        text,
        flags=re.I,
    )
    if not marker:
        # Some page layouts omit the literal pipe after HTML is flattened.
        marker = re.search(
            rf"4D Results.*?(?P<date>(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{{2}}\s+[A-Z][a-z]{{2}}\s+\d{{4}})"
            rf".{{0,40}}?Draw No\.\s*{draw_no}\b",
            text,
            flags=re.I,
        )
    if not marker:
        raise ValueError(
            f"Draw {draw_no}: result header not found. "
            "The draw may not exist yet, or Singapore Pools changed the page layout."
        )

    tail = text[marker.end():]

    m1 = re.search(r"\b1st Prize\s+(\d{4})\b", tail, re.I)
    m2 = re.search(r"\b2nd Prize\s+(\d{4})\b", tail, re.I)
    m3 = re.search(r"\b3rd Prize\s+(\d{4})\b", tail, re.I)
    ms = re.search(r"\bStarter Prizes\b(.*?)\bConsolation Prizes\b", tail, re.I)
    mc = re.search(r"\bConsolation Prizes\b(.*?)(?:\bPrizes not claimed\b|\b4D 万字票\b)", tail, re.I)

    if not all([m1, m2, m3, ms, mc]):
        raise ValueError(f"Draw {draw_no}: could not identify all prize sections.")

    starter = re.findall(r"\b\d{4}\b", ms.group(1))
    consolation = re.findall(r"\b\d{4}\b", mc.group(1))

    if len(starter) != 10 or len(consolation) != 10:
        raise ValueError(
            f"Draw {draw_no}: expected 10 Starter + 10 Consolation, "
            f"got {len(starter)} + {len(consolation)}."
        )

    nums = [m1.group(1), m2.group(1), m3.group(1), *starter, *consolation]
    if len(nums) != 23:
        raise ValueError(f"Draw {draw_no}: expected 23 numbers, got {len(nums)}.")

    # Keep source order because the downstream ranker infers prize tier from row position.
    date_iso = datetime.strptime(marker.group("date"), "%a, %d %b %Y").strftime("%Y/%m/%d")
    return date_iso, nums


def fetch_draw(draw_no: int) -> tuple[str, list[str]]:
    return parse_draw(draw_no, fetch_html(draw_no))


def load_csv(path: Path) -> tuple[list[dict[str, str]], dict[int, list[dict[str, str]]]]:
    if not path.exists():
        raise SystemExit(f"Can't find CSV: {path.resolve()}")

    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"draw_number", "number", "date"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise SystemExit(f"Unexpected CSV columns: {reader.fieldnames}")

        rows = []
        by_draw: dict[int, list[dict[str, str]]] = {}
        for r in reader:
            dn = int(r["draw_number"])
            num = re.sub(r"\D", "", str(r["number"])).zfill(4)[-4:]
            row = dict(r)
            row["draw_number"] = str(dn)
            row["number"] = num
            rows.append(row)
            by_draw.setdefault(dn, []).append(row)

    return rows, by_draw


def compare_draw(draw_no: int, local_rows: list[dict[str, str]], fetched_date: str, fetched_nums: list[str]) -> bool:
    if len(local_rows) != 23:
        print(f"Draw {draw_no}: LOCAL HAS {len(local_rows)} ROWS, expected 23 ✗")
        return False

    local_nums = [r["number"] for r in local_rows]
    local_dates = {str(r["date"]).replace("-", "/") for r in local_rows}

    nums_ok = local_nums == fetched_nums
    date_ok = len(local_dates) == 1 and next(iter(local_dates)) == fetched_date

    if nums_ok and date_ok:
        print(f"Draw {draw_no}: 23/23 MATCH ✓  ({fetched_date})")
        return True

    print(f"Draw {draw_no}: MISMATCH ✗")
    if not date_ok:
        print(f"  local date(s): {sorted(local_dates)}")
        print(f"  fetched date:  {fetched_date}")

    if not nums_ok:
        for i, (a, b) in enumerate(zip(local_nums, fetched_nums), start=1):
            if a != b:
                tier = (
                    "1st" if i == 1 else
                    "2nd" if i == 2 else
                    "3rd" if i == 3 else
                    "Starter" if i <= 13 else
                    "Consolation"
                )
                print(f"  position {i:2d} ({tier}): local={a} fetched={b}")
        if len(local_nums) != len(fetched_nums):
            print(f"  row counts: local={len(local_nums)}, fetched={len(fetched_nums)}")
    return False


def verify(csv_path: Path, draws: list[int]) -> int:
    _, by_draw = load_csv(csv_path)
    ok = True

    print(f"Verifying against: {csv_path.resolve()}")
    for dn in draws:
        if dn not in by_draw:
            print(f"Draw {dn}: not present in local CSV ✗")
            ok = False
            continue
        try:
            dt, nums = fetch_draw(dn)
        except Exception as e:
            print(f"Draw {dn}: FETCH/PARSE FAILED ✗ — {e}")
            ok = False
            continue
        ok = compare_draw(dn, by_draw[dn], dt, nums) and ok
        time.sleep(0.5)

    print()
    print("Verification PASSED ✓" if ok else "Verification FAILED ✗")
    return 0 if ok else 1


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def update(csv_path: Path, max_new: int = 50) -> int:
    rows, by_draw = load_csv(csv_path)
    if not by_draw:
        raise SystemExit("CSV has no draws.")

    last = max(by_draw)
    print(f"Master:    {csv_path.resolve()}")
    print(f"Last draw: {last}")
    print("Checking for newer completed draws...")

    new_rows: list[dict[str, str]] = []
    dn = last + 1

    for _ in range(max_new):
        try:
            dt, nums = fetch_draw(dn)
        except ValueError as e:
            # The first non-existent / not-yet-published draw is our normal stopping point.
            print(f"Stop at {dn}: {e}")
            break
        except RuntimeError as e:
            # Network/server errors must NOT be treated as "no new draw".
            print(f"Network error while checking draw {dn}: {e}", file=sys.stderr)
            return 2

        print(f"Found draw {dn}: {dt} — 23 numbers ✓")
        for n in nums:
            new_rows.append({"draw_number": str(dn), "number": n, "date": dt})

        dn += 1
        time.sleep(0.5)
    else:
        raise SystemExit(
            f"Safety stop: reached --max-new={max_new}. "
            "Refusing to continue automatically."
        )

    if not new_rows:
        print("Already current. Nothing to append.")
        return 0

    # Validate new data before touching the master.
    seen_pairs: set[tuple[int, str]] = set()
    for r in new_rows:
        pair = (int(r["draw_number"]), r["number"])
        if pair in seen_pairs:
            raise SystemExit(f"Duplicate inside fetched patch: {pair}; refusing to save.")
        seen_pairs.add(pair)

    existing_pairs = {(int(r["draw_number"]), r["number"]) for r in rows}
    overlap = seen_pairs & existing_pairs
    if overlap:
        raise SystemExit(
            f"Fetched patch overlaps {len(overlap)} existing draw/number rows; refusing to save."
        )

    # Preserve original column order; our three known fields are all the current dataset needs.
    fieldnames = list(rows[0].keys())
    combined = rows + new_rows
    write_csv(csv_path, combined, fieldnames)

    added_draws = sorted({int(r["draw_number"]) for r in new_rows})
    print()
    print(f"Rows appended: {len(new_rows):,}")
    print(f"Draws added:   {added_draws[0]}–{added_draws[-1]}")
    print(f"New last draw: {added_draws[-1]}")
    print(f"Saved:         {csv_path.resolve()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--csv",
        default="4d_prizes_updated_20260902.csv",
        help="Master CSV to verify/update (default: 4d_prizes_updated_20260902.csv)",
    )
    ap.add_argument(
        "--verify",
        nargs="+",
        type=int,
        help="Fetch these draw numbers and compare them exactly against the local CSV.",
    )
    ap.add_argument(
        "--max-new",
        type=int,
        default=50,
        help="Safety limit for automatic append (default: 50 draws).",
    )
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if args.verify:
        return verify(csv_path, args.verify)
    return update(csv_path, args.max_new)


if __name__ == "__main__":
    raise SystemExit(main())
