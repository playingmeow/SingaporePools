#!/usr/bin/env python3
"""
Singapore Pools 4D updater / verifier.

Examples:

  python final.py4 --csv 4d_prizes.csv

  python final.py4 --csv 4d_prizes.csv --verify 5542 5543 5544

  python final.py4 --csv 4d_prizes.csv --max-new 50


Behavior:

- Fetches official Singapore Pools result pages by draw number.
- Validates 1st/2nd/3rd + 10 Starter + 10 Consolation = 23 numbers.
- Allows duplicate 4D numbers within the same draw.
- Allows local CSV numbers to be in a different order.
- Verification compares the 23 numbers as a MULTISET:
    same numbers + same duplicate counts = match.
- Existing incomplete historical draws are preserved and warned about.
- Appends only newer draws.
- Never overwrites existing draws.
- Preserves leading zeroes.
- Rejects malformed 4D numbers instead of silently modifying them.
- Uses a temporary file before replacing the master CSV.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RESULTS_URL = (
    "https://www.singaporepools.com.sg/en/product/pages/"
    "4d_results.aspx?sppl={token}"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)

EXPECTED_NUMBERS_PER_DRAW = 23
EXPECTED_STARTER = 10
EXPECTED_CONSOLATION = 10

REQUEST_DELAY_SECONDS = 0.5


# ---------------------------------------------------------------------------
# URL / HTTP
# ---------------------------------------------------------------------------

def draw_url(draw_no: int) -> str:
    """Build the Singapore Pools result URL for a draw number."""
    token = base64.b64encode(
        f"DrawNumber={draw_no}".encode("ascii")
    ).decode("ascii")

    return RESULTS_URL.format(token=token)


def fetch_html(draw_no: int, timeout: int = 30) -> str:
    """Fetch raw HTML for a draw."""
    req = Request(
        draw_url(draw_no),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-SG,en;q=0.9",
        },
    )

    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()

    except (HTTPError, URLError, TimeoutError) as e:
        raise RuntimeError(
            f"Could not fetch draw {draw_no}: {e}"
        ) from e

    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def html_to_text(page: str) -> str:
    """
    Convert HTML to normalized text.

    Scripts and styles are removed first so numbers embedded in
    JavaScript/CSS do not contaminate prize extraction.
    """
    page = re.sub(
        r"(?is)<script\b.*?</script>",
        " ",
        page,
    )

    page = re.sub(
        r"(?is)<style\b.*?</style>",
        " ",
        page,
    )

    page = re.sub(
        r"(?s)<[^>]+>",
        " ",
        page,
    )

    page = html.unescape(page)

    return re.sub(r"\s+", " ", page).strip()


def parse_draw(
    draw_no: int,
    page: str,
) -> tuple[str, list[str]]:
    """
    Parse one Singapore Pools draw.

    Returns:

        (
            date_as_YYYY/MM/DD,
            [
                1st,
                2nd,
                3rd,
                starter x10,
                consolation x10,
            ],
        )

    The fetched source order is preserved.
    """

    text = html_to_text(page)

    # ---------------------------------------------------------------
    # Locate draw header and date.
    # ---------------------------------------------------------------

    marker = re.search(
        rf"4D Results.*?"
        rf"(?P<date>"
        rf"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),"
        rf"\s+\d{{2}}\s+[A-Z][a-z]{{2}}\s+\d{{4}}"
        rf")"
        rf"\s*\|\s*Draw No\.\s*{draw_no}\b",
        text,
        flags=re.I,
    )

    if not marker:
        # Some flattened page layouts do not retain the literal "|".
        marker = re.search(
            rf"4D Results.*?"
            rf"(?P<date>"
            rf"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),"
            rf"\s+\d{{2}}\s+[A-Z][a-z]{{2}}\s+\d{{4}}"
            rf")"
            rf".{{0,40}}?"
            rf"Draw No\.\s*{draw_no}\b",
            text,
            flags=re.I,
        )

    if not marker:
        raise ValueError(
            f"Draw {draw_no}: result header not found. "
            "The draw may not exist yet, or Singapore Pools "
            "changed the page layout."
        )

    tail = text[marker.end():]

    # ---------------------------------------------------------------
    # Main prizes.
    # ---------------------------------------------------------------

    m1 = re.search(
        r"\b1st Prize\s+(\d{4})\b",
        tail,
        re.I,
    )

    m2 = re.search(
        r"\b2nd Prize\s+(\d{4})\b",
        tail,
        re.I,
    )

    m3 = re.search(
        r"\b3rd Prize\s+(\d{4})\b",
        tail,
        re.I,
    )

    # ---------------------------------------------------------------
    # Starter prizes.
    # ---------------------------------------------------------------

    ms = re.search(
        r"\bStarter Prizes\b"
        r"(.*?)"
        r"\bConsolation Prizes\b",
        tail,
        re.I,
    )

    # ---------------------------------------------------------------
    # Consolation prizes.
    # ---------------------------------------------------------------

    mc = re.search(
        r"\bConsolation Prizes\b"
        r"(.*?)"
        r"(?:"
        r"\bPrizes not claimed\b"
        r"|"
        r"\b4D 万字票\b"
        r")",
        tail,
        re.I,
    )

    if not all([m1, m2, m3, ms, mc]):
        raise ValueError(
            f"Draw {draw_no}: could not identify all prize sections."
        )

    starter = re.findall(
        r"\b\d{4}\b",
        ms.group(1),
    )

    consolation = re.findall(
        r"\b\d{4}\b",
        mc.group(1),
    )

    # ---------------------------------------------------------------
    # Validate exact section counts.
    # ---------------------------------------------------------------

    if len(starter) != EXPECTED_STARTER:
        raise ValueError(
            f"Draw {draw_no}: expected "
            f"{EXPECTED_STARTER} Starter numbers, "
            f"got {len(starter)}."
        )

    if len(consolation) != EXPECTED_CONSOLATION:
        raise ValueError(
            f"Draw {draw_no}: expected "
            f"{EXPECTED_CONSOLATION} Consolation numbers, "
            f"got {len(consolation)}."
        )

    nums = [
        m1.group(1),
        m2.group(1),
        m3.group(1),
        *starter,
        *consolation,
    ]

    if len(nums) != EXPECTED_NUMBERS_PER_DRAW:
        raise ValueError(
            f"Draw {draw_no}: expected "
            f"{EXPECTED_NUMBERS_PER_DRAW} numbers, "
            f"got {len(nums)}."
        )

    # Every fetched number must be exactly four digits.
    for position, number in enumerate(nums, start=1):
        if not re.fullmatch(r"\d{4}", number):
            raise ValueError(
                f"Draw {draw_no}: invalid number at "
                f"position {position}: {number!r}"
            )

    # Parse source date.
    try:
        date_iso = datetime.strptime(
            marker.group("date"),
            "%a, %d %b %Y",
        ).strftime("%Y/%m/%d")

    except ValueError as e:
        raise ValueError(
            f"Draw {draw_no}: could not parse result date."
        ) from e

    return date_iso, nums


def fetch_draw(draw_no: int) -> tuple[str, list[str]]:
    """Fetch and parse one draw."""
    return parse_draw(
        draw_no,
        fetch_html(draw_no),
    )


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def load_csv(
    path: Path,
) -> tuple[
    list[dict[str, str]],
    dict[int, list[dict[str, str]]],
]:
    """
    Load the CSV.

    Historical draws are allowed to have fewer/more than 23 rows.

    This is intentional:
    the updater should not block newer valid draws merely because
    an old historical draw is incomplete.

    However:
    - draw numbers must be valid integers
    - numbers must be exactly four digits
    - dates cannot be blank
    """

    if not path.exists():
        raise SystemExit(
            f"Can't find CSV: {path.resolve()}"
        )

    with path.open(
        "r",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        reader = csv.DictReader(f)

        required = {
            "draw_number",
            "number",
            "date",
        }

        if not reader.fieldnames or not required.issubset(
            reader.fieldnames
        ):
            raise SystemExit(
                f"Unexpected CSV columns: {reader.fieldnames}"
            )

        rows: list[dict[str, str]] = []
        by_draw: dict[int, list[dict[str, str]]] = {}

        for line_no, r in enumerate(reader, start=2):

            raw_draw = str(
                r.get("draw_number", "")
            ).strip()

            raw_number = str(
                r.get("number", "")
            ).strip()

            raw_date = str(
                r.get("date", "")
            ).strip()

            # -------------------------------------------------------
            # Strict draw number validation.
            # -------------------------------------------------------

            if not re.fullmatch(
                r"\d+",
                raw_draw,
            ):
                raise SystemExit(
                    f"Invalid draw number at CSV line "
                    f"{line_no}: {raw_draw!r}"
                )

            dn = int(raw_draw)

            # -------------------------------------------------------
            # Strict 4D number validation.
            #
            # Do NOT silently transform malformed data.
            # -------------------------------------------------------

            if not re.fullmatch(
                r"\d{4}",
                raw_number,
            ):
                raise SystemExit(
                    f"Invalid 4D number at CSV line "
                    f"{line_no}: {raw_number!r}. "
                    "Expected exactly four digits."
                )

            if not raw_date:
                raise SystemExit(
                    f"Missing date at CSV line {line_no}."
                )

            row = dict(r)

            row["draw_number"] = str(dn)
            row["number"] = raw_number
            row["date"] = raw_date

            rows.append(row)

            by_draw.setdefault(
                dn,
                [],
            ).append(row)

    # ---------------------------------------------------------------
    # Warn about historical problems but don't stop the updater.
    # ---------------------------------------------------------------

    for draw_no, draw_rows in sorted(
        by_draw.items()
    ):
        if len(draw_rows) != EXPECTED_NUMBERS_PER_DRAW:
            print(
                f"WARNING: Draw {draw_no}: CSV contains "
                f"{len(draw_rows)} rows; expected "
                f"{EXPECTED_NUMBERS_PER_DRAW}. "
                "Existing data will be preserved."
            )

        dates = {
            str(r["date"]).replace("-", "/")
            for r in draw_rows
        }

        if len(dates) > 1:
            print(
                f"WARNING: Draw {draw_no}: CSV contains "
                f"multiple dates: {sorted(dates)}. "
                "Existing data will be preserved."
            )

    return rows, by_draw


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def compare_draw(
    draw_no: int,
    local_rows: list[dict[str, str]],
    fetched_date: str,
    fetched_nums: list[str],
) -> bool:
    """
    Compare local and fetched draw data.

    IMPORTANT:
    Numbers are compared as a MULTISET.

    Therefore:

        A B C C

    matches:

        C A C B

    but does NOT match:

        A B C D

    This means:
    - order does not matter
    - duplicate counts do matter
    """

    if len(local_rows) != EXPECTED_NUMBERS_PER_DRAW:
        print(
            f"Draw {draw_no}: LOCAL HAS "
            f"{len(local_rows)} ROWS, expected "
            f"{EXPECTED_NUMBERS_PER_DRAW} ✗"
        )
        return False

    if len(fetched_nums) != EXPECTED_NUMBERS_PER_DRAW:
        print(
            f"Draw {draw_no}: FETCHED HAS "
            f"{len(fetched_nums)} NUMBERS, expected "
            f"{EXPECTED_NUMBERS_PER_DRAW} ✗"
        )
        return False

    local_nums = [
        r["number"]
        for r in local_rows
    ]

    local_dates = {
        str(r["date"]).replace("-", "/")
        for r in local_rows
    }

    # ---------------------------------------------------------------
    # Date comparison remains exact.
    # ---------------------------------------------------------------

    date_ok = (
        len(local_dates) == 1
        and next(iter(local_dates)) == fetched_date
    )

    # ---------------------------------------------------------------
    # Number comparison is order-independent AND duplicate-aware.
    # ---------------------------------------------------------------

    local_counter = Counter(local_nums)
    fetched_counter = Counter(fetched_nums)

    nums_ok = (
        local_counter == fetched_counter
    )

    if nums_ok and date_ok:
        print(
            f"Draw {draw_no}: "
            f"{EXPECTED_NUMBERS_PER_DRAW}/"
            f"{EXPECTED_NUMBERS_PER_DRAW} "
            f"NUMBERS MATCH ✓  ({fetched_date})"
        )
        return True

    print(
        f"Draw {draw_no}: MISMATCH ✗"
    )

    # ---------------------------------------------------------------
    # Date differences.
    # ---------------------------------------------------------------

    if not date_ok:
        print(
            f"  local date(s): {sorted(local_dates)}"
        )
        print(
            f"  fetched date:  {fetched_date}"
        )

    # ---------------------------------------------------------------
    # Number differences.
    #
    # Counter subtraction preserves duplicate counts.
    # ---------------------------------------------------------------

    if not nums_ok:

        missing = (
            fetched_counter - local_counter
        )

        extra = (
            local_counter - fetched_counter
        )

        if missing:
            print(
                "  missing from local: "
                + ", ".join(
                    f"{number} x{count}"
                    for number, count in sorted(
                        missing.items()
                    )
                )
            )

        if extra:
            print(
                "  extra in local: "
                + ", ".join(
                    f"{number} x{count}"
                    for number, count in sorted(
                        extra.items()
                    )
                )
            )

    return False


def verify(
    csv_path: Path,
    draws: list[int],
) -> int:
    """Fetch and verify requested draw numbers."""

    _, by_draw = load_csv(csv_path)

    ok = True

    print(
        f"Verifying against: {csv_path.resolve()}"
    )

    for dn in draws:

        if dn not in by_draw:
            print(
                f"Draw {dn}: "
                "not present in local CSV ✗"
            )
            ok = False
            continue

        try:
            dt, nums = fetch_draw(dn)

        except Exception as e:
            print(
                f"Draw {dn}: "
                f"FETCH/PARSE FAILED ✗ — {e}"
            )
            ok = False
            continue

        result = compare_draw(
            dn,
            by_draw[dn],
            dt,
            nums,
        )

        ok = result and ok

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

    print()

    if ok:
        print(
            "Verification PASSED ✓"
        )
        return 0

    print(
        "Verification FAILED ✗"
    )
    return 1


# ---------------------------------------------------------------------------
# Update validation
# ---------------------------------------------------------------------------

def validate_new_patch(
    new_rows: list[dict[str, str]],
    existing_draws: set[int],
) -> None:
    """
    Validate fetched draws before writing.

    Duplicate numbers are explicitly allowed.

    We validate the draw as a collection of 23 rows,
    not as 23 unique numbers.
    """

    new_draws = sorted(
        {
            int(r["draw_number"])
            for r in new_rows
        }
    )

    # ---------------------------------------------------------------
    # Every fetched draw must contain exactly 23 rows.
    # ---------------------------------------------------------------

    for draw_no in new_draws:

        draw_rows = [
            r
            for r in new_rows
            if int(r["draw_number"]) == draw_no
        ]

        if len(draw_rows) != EXPECTED_NUMBERS_PER_DRAW:
            raise SystemExit(
                f"Draw {draw_no}: fetched patch contains "
                f"{len(draw_rows)} rows; expected "
                f"{EXPECTED_NUMBERS_PER_DRAW}. "
                "Refusing to save."
            )

        # -----------------------------------------------------------
        # Every fetched number must be four digits.
        # -----------------------------------------------------------

        for position, row in enumerate(
            draw_rows,
            start=1,
        ):
            number = row["number"]

            if not re.fullmatch(
                r"\d{4}",
                number,
            ):
                raise SystemExit(
                    f"Draw {draw_no}: invalid 4D number "
                    f"at position {position}: "
                    f"{number!r}. "
                    "Refusing to save."
                )

    # ---------------------------------------------------------------
    # Append-only protection.
    #
    # A fetched draw must not already exist in the CSV.
    # ---------------------------------------------------------------

    overlap = (
        set(new_draws)
        & existing_draws
    )

    if overlap:
        raise SystemExit(
            "Fetched patch contains existing draw(s): "
            f"{sorted(overlap)}; refusing to save."
        )


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------

def write_csv(
    path: Path,
    rows: list[dict[str, str]],
    fieldnames: list[str],
) -> None:
    """
    Write through a temporary file and atomically replace
    the master where supported.
    """

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    try:

        with tmp.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )

            writer.writeheader()
            writer.writerows(rows)

        tmp.replace(path)

    except Exception:

        try:
            tmp.unlink(
                missing_ok=True
            )
        except Exception:
            pass

        raise


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def update(
    csv_path: Path,
    max_new: int = 50,
) -> int:
    """Find and append newer completed draws."""

    if max_new < 1:
        raise SystemExit(
            "--max-new must be at least 1."
        )

    rows, by_draw = load_csv(
        csv_path
    )

    if not by_draw:
        raise SystemExit(
            "CSV has no draws."
        )

    last = max(by_draw)

    print(
        f"Master:    {csv_path.resolve()}"
    )

    print(
        f"Last draw: {last}"
    )

    print(
        "Checking for newer completed draws..."
    )

    new_rows: list[dict[str, str]] = []

    dn = last + 1

    # ---------------------------------------------------------------
    # Search forward until the first unavailable draw.
    # ---------------------------------------------------------------

    for _ in range(max_new):

        try:
            dt, nums = fetch_draw(dn)

        except ValueError as e:
            # Normal stopping condition:
            # draw does not exist / has not been published / layout
            # did not contain a valid result header.
            print(
                f"Stop at {dn}: {e}"
            )
            break

        except RuntimeError as e:
            # Network/server problems are NOT treated as "no draw".
            print(
                f"Network error while checking "
                f"draw {dn}: {e}",
                file=sys.stderr,
            )
            return 2

        except Exception as e:
            # Unexpected parser errors also fail safely.
            print(
                f"Unexpected error while checking "
                f"draw {dn}: {e}",
                file=sys.stderr,
            )
            return 2

        # -----------------------------------------------------------
        # Extra safety check.
        # -----------------------------------------------------------

        if len(nums) != EXPECTED_NUMBERS_PER_DRAW:
            print(
                f"Draw {dn}: parser returned "
                f"{len(nums)} numbers instead of "
                f"{EXPECTED_NUMBERS_PER_DRAW}.",
                file=sys.stderr,
            )
            return 2

        print(
            f"Found draw {dn}: {dt} — "
            f"{EXPECTED_NUMBERS_PER_DRAW} numbers ✓"
        )

        # -----------------------------------------------------------
        # IMPORTANT:
        # Do NOT deduplicate these numbers.
        #
        # If the official result contains:
        #
        #   0529
        #   0529
        #
        # both rows must be preserved.
        # -----------------------------------------------------------

        for number in nums:
            new_rows.append(
                {
                    "draw_number": str(dn),
                    "number": number,
                    "date": dt,
                }
            )

        dn += 1

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

    else:
        raise SystemExit(
            f"Safety stop: reached "
            f"--max-new={max_new}. "
            "Refusing to continue automatically."
        )

    # ---------------------------------------------------------------
    # Nothing new.
    # ---------------------------------------------------------------

    if not new_rows:
        print(
            "Already current. Nothing to append."
        )
        return 0

    # ---------------------------------------------------------------
    # Validate everything before modifying the master.
    # ---------------------------------------------------------------

    validate_new_patch(
        new_rows,
        set(by_draw),
    )

    # ---------------------------------------------------------------
    # Preserve the existing CSV column order.
    # ---------------------------------------------------------------

    fieldnames = list(
        rows[0].keys()
    )

    combined = rows + new_rows

    # ---------------------------------------------------------------
    # Write only after all validation succeeds.
    # ---------------------------------------------------------------

    write_csv(
        csv_path,
        combined,
        fieldnames,
    )

    added_draws = sorted(
        {
            int(r["draw_number"])
            for r in new_rows
        }
    )

    print()

    print(
        f"Rows appended: {len(new_rows):,}"
    )

    print(
        f"Draws added:   "
        f"{added_draws[0]}–{added_draws[-1]}"
    )

    print(
        f"New last draw: "
        f"{added_draws[-1]}"
    )

    print(
        f"Saved:         "
        f"{csv_path.resolve()}"
    )

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    """Command-line entry point."""

    ap = argparse.ArgumentParser(
        description=(
            "Singapore Pools 4D updater / verifier"
        )
    )

    ap.add_argument(
        "--csv",
        default="4d_prizes_updated_20260902.csv",
        help=(
            "Master CSV to verify/update "
            "(default: 4d_prizes_updated_20260902.csv)"
        ),
    )

    ap.add_argument(
        "--verify",
        nargs="+",
        type=int,
        help=(
            "Fetch these draw numbers and compare "
            "their 23 numbers against the local CSV. "
            "Order does not matter, but duplicate counts do."
        ),
    )

    ap.add_argument(
        "--max-new",
        type=int,
        default=50,
        help=(
            "Safety limit for automatic append "
            "(default: 50 draws)."
        ),
    )

    args = ap.parse_args()

    if args.max_new < 1:
        ap.error(
            "--max-new must be at least 1"
        )

    csv_path = Path(
        args.csv
    )

    if args.verify:
        return verify(
            csv_path,
            args.verify,
        )

    return update(
        csv_path,
        args.max_new,
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
