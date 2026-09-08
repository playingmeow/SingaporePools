#!/usr/bin/env python3
"""
update_toto.py

Append newer Singapore Pools TOTO results to the inherited toto_prizes dataset,
while preserving its existing 3-column format:

    draw_number,number,date

The "number" field stores 7 two-digit strings:
    [6 winning numbers, additional number]

Examples:
    python update_toto.py --csv toto_prizes.csv --verify 4197 4198 4199
    python update_toto.py --csv toto_prizes.csv

Notes:
- Existing historical rows are left untouched, including old NaN / year-0001 rows.
- New rows are appended only if a complete, valid result is fetched.
- Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import html
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = "https://www.singaporepools.com.sg/en/product/sr/Pages/toto_results.aspx?sppl={token}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)

EXPECTED_COLUMNS = ["draw_number", "number", "date"]


class NoPublishedDraw(Exception):
    """Requested draw does not appear to have a published result yet."""


def draw_url(draw_no: int) -> str:
    token = base64.b64encode(f"DrawNumber={draw_no}".encode("ascii")).decode("ascii")
    return BASE_URL.format(token=token)


def fetch_html(draw_no: int, retries: int = 3, timeout: int = 25) -> str:
    url = draw_url(draw_no)
    last_error = None

    for attempt in range(1, retries + 1):
        req = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-SG,en;q=0.9",
                "Cache-Control": "no-cache",
            },
        )

        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")

        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(
        f"Network error while fetching draw {draw_no} after {retries} attempts: {last_error}"
    )


def html_to_text(page: str) -> str:
    # Remove content that can pollute visible-text parsing.
    page = re.sub(r"(?is)<script\b.*?</script>", " ", page)
    page = re.sub(r"(?is)<style\b.*?</style>", " ", page)
    page = re.sub(r"(?is)<!--.*?-->", " ", page)

    # Give common block-ish tags spaces, then strip the rest.
    page = re.sub(r"(?i)<(?:br|p|div|tr|td|th|li|h[1-6])\b[^>]*>", " ", page)
    text = re.sub(r"(?s)<[^>]+>", " ", page)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_toto_result(page: str, requested_draw: int) -> tuple[str, list[str]]:
    text = html_to_text(page)

    # Parse draw number and date independently. The raw Singapore Pools HTML
    # may contain hidden/accessibility markup between items that look adjacent
    # in the browser.
    draw_match = re.search(
        r"Draw\s*(?:No\.?|Number)\s*[:#-]?\s*(\d+)",
        text,
        flags=re.I,
    )

    if not draw_match:
        raise NoPublishedDraw(
            f"Draw {requested_draw}: draw number not found. "
            "The draw may not exist yet, or Singapore Pools changed the page layout."
        )

    returned_draw = int(draw_match.group(1))

    date_match = re.search(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s*"
        r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        text,
        flags=re.I,
    )

    if not date_match:
        date_match = re.search(
            r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{4})\b",
            text,
            flags=re.I,
        )

    if not date_match:
        raise RuntimeError(f"Draw {requested_draw}: result date not found.")

    date_text = date_match.group(1)

    if returned_draw != requested_draw:
        # Singapore Pools appears to fall back to the latest published TOTO draw
        # when a future/unpublished draw number is requested. In update mode this
        # is our clean stopping condition.
        if returned_draw < requested_draw:
            raise NoPublishedDraw(
                f"Draw {requested_draw}: not published yet "
                f"(Singapore Pools returned latest Draw No. {returned_draw})."
            )
        raise RuntimeError(
            f"Draw {requested_draw}: Singapore Pools returned Draw No. {returned_draw}. "
            "Stopping rather than appending uncertain data."
        )

    try:
        dt = datetime.strptime(date_text, "%d %b %Y")
    except ValueError as exc:
        raise RuntimeError(
            f"Draw {requested_draw}: could not parse date {date_text!r}"
        ) from exc

    # Preserve inherited file's YYYY/MM/DD format.
    date_out = dt.strftime("%Y/%m/%d")

    main_match = re.search(
        r"Winning\s+Numbers(.*?)Additional\s+Number",
        text,
        flags=re.I | re.S,
    )
    if not main_match:
        raise RuntimeError(
            f"Draw {requested_draw}: Winning Numbers section not found."
        )

    main_numbers = re.findall(r"(?<!\d)(\d{1,2})(?!\d)", main_match.group(1))
    if len(main_numbers) != 6:
        raise RuntimeError(
            f"Draw {requested_draw}: expected 6 winning numbers, found "
            f"{len(main_numbers)}: {main_numbers}"
        )

    add_match = re.search(
        r"Additional\s+Number\s+(\d{1,2})(?!\d)",
        text,
        flags=re.I,
    )
    if not add_match:
        raise RuntimeError(
            f"Draw {requested_draw}: Additional Number not found."
        )

    all_numbers = [n.zfill(2) for n in main_numbers]
    all_numbers.append(add_match.group(1).zfill(2))

    # Strict validation for newly fetched draws.
    ints = [int(n) for n in all_numbers]

    if len(all_numbers) != 7:
        raise RuntimeError(
            f"Draw {requested_draw}: expected 7 numbers total, got {len(all_numbers)}."
        )

    if any(n < 1 or n > 49 for n in ints):
        raise RuntimeError(
            f"Draw {requested_draw}: out-of-range number found: {all_numbers}"
        )

    if len(set(ints[:6])) != 6:
        raise RuntimeError(
            f"Draw {requested_draw}: duplicate winning number found: {all_numbers[:6]}"
        )

    if ints[6] in ints[:6]:
        raise RuntimeError(
            f"Draw {requested_draw}: additional number duplicates a winning number: "
            f"{all_numbers}"
        )

    return date_out, all_numbers


def fetch_draw(draw_no: int) -> tuple[str, list[str]]:
    page = fetch_html(draw_no)
    return parse_toto_result(page, draw_no)


def read_dataset(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []

        if columns != EXPECTED_COLUMNS:
            raise RuntimeError(
                f"Unexpected CSV columns: {columns}. "
                f"Expected exactly: {EXPECTED_COLUMNS}"
            )

        rows = list(reader)

    return rows, columns


def existing_draw_map(rows: list[dict[str, str]]) -> dict[int, dict[str, str]]:
    result: dict[int, dict[str, str]] = {}

    for row in rows:
        raw = (row.get("draw_number") or "").strip()
        if not raw:
            continue

        try:
            draw_no = int(raw)
        except ValueError:
            continue

        if draw_no in result:
            raise RuntimeError(f"Duplicate draw number already exists in dataset: {draw_no}")

        result[draw_no] = row

    return result


def parse_existing_number_field(value: str) -> list[str] | None:
    value = (value or "").strip()

    if not value or value.lower() == "nan":
        return None

    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None

    if not isinstance(parsed, list) or len(parsed) != 7:
        return None

    return [str(x).zfill(2) for x in parsed]


def verify_draws(path: Path, requested: list[int]) -> int:
    rows, _ = read_dataset(path)
    by_draw = existing_draw_map(rows)

    print(f"Verifying against: {path.resolve()}")

    failures = 0

    for draw_no in requested:
        row = by_draw.get(draw_no)
        if row is None:
            print(f"Draw {draw_no}: NOT FOUND in local dataset ✗")
            failures += 1
            continue

        local_numbers = parse_existing_number_field(row.get("number", ""))
        local_date = (row.get("date") or "").strip()

        if local_numbers is None or not local_date or local_date.lower() == "nan":
            print(f"Draw {draw_no}: local row contains NaN/unparseable data ✗")
            failures += 1
            continue

        try:
            fetched_date, fetched_numbers = fetch_draw(draw_no)
        except Exception as exc:
            print(f"Draw {draw_no}: FETCH/PARSE ERROR ✗ — {exc}")
            failures += 1
            continue

        numbers_match = fetched_numbers == local_numbers
        date_match = fetched_date == local_date

        if numbers_match and date_match:
            print(f"Draw {draw_no}: 7/7 MATCH ✓  ({fetched_date})")
        else:
            print(f"Draw {draw_no}: MISMATCH ✗")
            if not numbers_match:
                print(f"  Local:   {local_numbers}")
                print(f"  Official:{fetched_numbers}")
            if not date_match:
                print(f"  Local date:   {local_date}")
                print(f"  Official date:{fetched_date}")
            failures += 1

    print()
    if failures:
        print(f"Verification FAILED ✗ — {failures} draw(s) did not match.")
        return 1

    print("Verification PASSED ✓")
    return 0


def make_number_field(numbers: list[str]) -> str:
    # Match inherited representation exactly:
    # ['14', '22', '32', '33', '36', '46', '42']
    return repr(numbers)


def safe_write(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        writer = csv.DictWriter(tmp, fieldnames=EXPECTED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        temp_path = Path(tmp.name)

    # Preserve a best-effort backup in case something unexpected happens.
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)

    temp_path.replace(path)

    try:
        backup.unlink()
    except OSError:
        pass


def update_dataset(path: Path) -> int:
    rows, _ = read_dataset(path)
    by_draw = existing_draw_map(rows)

    if not by_draw:
        raise RuntimeError("No valid draw numbers found in dataset.")

    last_draw = max(by_draw)

    print(f"Master:    {path.resolve()}")
    print(f"Last draw: {last_draw}")
    print("Checking for newer completed draws...")

    new_rows: list[dict[str, str]] = []
    draw_no = last_draw + 1

    while True:
        try:
            date_out, numbers = fetch_draw(draw_no)

        except NoPublishedDraw as exc:
            print(f"Stop at {draw_no}: {exc}")
            break

        # Network/site/parser problems are intentionally NOT treated as "no draw".
        except Exception:
            raise

        if draw_no in by_draw:
            raise RuntimeError(
                f"Internal safety check failed: draw {draw_no} already exists."
            )

        new_row = {
            "draw_number": str(draw_no),
            "number": make_number_field(numbers),
            "date": date_out,
        }

        new_rows.append(new_row)
        by_draw[draw_no] = new_row

        print(
            f"Found draw {draw_no}: {date_out} — "
            f"{' '.join(numbers[:6])} + {numbers[6]} ✓"
        )

        draw_no += 1
        time.sleep(0.4)

    if not new_rows:
        print()
        print("No new completed TOTO draws found. File unchanged.")
        return 0

    # Extra duplicate protection inside this update batch.
    new_draws = [int(r["draw_number"]) for r in new_rows]
    if len(new_draws) != len(set(new_draws)):
        raise RuntimeError("Duplicate draw number detected in newly fetched rows.")

    updated_rows = rows + new_rows
    safe_write(path, updated_rows)

    print()
    print(f"Rows appended: {len(new_rows)}")
    print(f"Draws added:   {new_draws[0]}–{new_draws[-1]}")
    print(f"New last draw: {new_draws[-1]}")
    print(f"Saved:         {path.resolve()}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify and update Singapore Pools TOTO historical results."
    )
    parser.add_argument(
        "--csv",
        default="toto_prizes.csv",
        help="Path to the inherited TOTO dataset (default: toto_prizes.csv)",
    )
    parser.add_argument(
        "--verify",
        nargs="+",
        type=int,
        metavar="DRAW",
        help="Verify one or more existing draw numbers against Singapore Pools.",
    )

    args = parser.parse_args()
    path = Path(args.csv)

    try:
        if args.verify:
            return verify_draws(path, args.verify)
        return update_dataset(path)

    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
