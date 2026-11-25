from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

WEBHOOK_URL_ENV = "DISCORD_WEBHOOK_URL"
SEEN_FILE = Path("seen_jobs.json")
REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 3
DISCORD_MAX_EMBEDS_PER_MESSAGE = 10
DISCORD_TITLE_MAX_LEN = 256
DISCORD_DESC_MAX_LEN = 4096


@dataclass(frozen=True)
class BoardTarget:
    ats: str
    company: str
    slug: str


@dataclass(frozen=True)
class JobPosting:
    job_id: str
    company: str
    title: str
    location: str
    url: str
    ats: str


# verified public boards with active/historical canadian or tech roles; replace or expand slugs as needed
TARGETS: tuple[BoardTarget, ...] = (
    BoardTarget(ats="greenhouse", company="Tenstorrent University", slug="tenstorrentuniversity"),
    BoardTarget(ats="ashby", company="Steel", slug="steel"),
)

# match internship/co-op terms or core engineering tracks using word boundaries
ROLE_PATTERN = re.compile(
    r"\b("
    r"intern|internship|co-?op|student|"
    r"software|swe|developer|engineer|engineering|"
    r"ai|ml|machine\s+learning|data|cloud|security|appsec|devops|sre"
    r")\b",
    re.IGNORECASE,
)

# match canadian provinces, major cities, or explicit canada/remote-canada strings
LOCATION_PATTERN = re.compile(
    r"\b("
    r"canada|canadian|"
    r"ontario|toronto|waterloo|kitchener|ottawa|markham|mississauga|oshawa|kingston|hamilton|london|"
    r"quebec|montreal|montr[eé]al|"
    r"british\s+columbia|vancouver|burnaby|victoria|"
    r"alberta|calgary|edmonton|"
    r"manitoba|winnipeg|saskatchewan|halifax|nova\s+scotia|new\s+brunswick|"
    r"remote\s*[-–—]?\s*canada"
    r")\b",
    re.IGNORECASE,
)


def fetch_json(url: str) -> object:
    # retry transient network or rate-limit errors with short backoff
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "CanadaInternAlertBot/1.0 (+github-actions)",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * attempt)

    raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error


def extract_greenhouse_location(item: dict) -> str:
    # greenhouse sometimes nests location in `location.name` or `metadata`
    loc_obj = item.get("location")
    if isinstance(loc_obj, dict) and isinstance(loc_obj.get("name"), str):
        return loc_obj["name"].strip()
    return ""


def extract_lever_locations(item: dict) -> list[str]:
    # lever can expose a primary location in `categories.location` and/or `workplaceType`
    locations: list[str] = []
    categories = item.get("categories")
    if isinstance(categories, dict):
        loc = categories.get("location")
        if isinstance(loc, str) and loc.strip():
            locations.append(loc.strip())
    workplace = item.get("workplaceType")
    if isinstance(workplace, str) and workplace.strip():
        locations.append(workplace.strip())
    return locations


def parse_board_jobs(target: BoardTarget) -> list[JobPosting]:
    postings: list[JobPosting] = []

    if target.ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{target.slug}/jobs"
        payload = fetch_json(url)
        raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        for item in raw_jobs:
            if not isinstance(item, dict) or "id" not in item:
                continue
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("absolute_url") or "").strip()
            location = extract_greenhouse_location(item)
            postings.append(
                JobPosting(
                    job_id=f"gh:{target.slug}:{item['id']}",
                    company=target.company,
                    title=title,
                    location=location,
                    url=job_url,
                    ats="Greenhouse",
                )
            )

    elif target.ats == "lever":
        url = f"https://api.lever.co/v0/postings/{target.slug}?mode=json"
        payload = fetch_json(url)
        raw_jobs = payload if isinstance(payload, list) else []
        for item in raw_jobs:
            if not isinstance(item, dict) or "id" not in item:
                continue
            title = str(item.get("text") or "").strip()
            job_url = str(item.get("hostedUrl") or "").strip()
            locs = extract_lever_locations(item)
            location = " | ".join(dict.fromkeys(locs))
            postings.append(
                JobPosting(
                    job_id=f"lv:{target.slug}:{item['id']}",
                    company=target.company,
                    title=title,
                    location=location,
                    url=job_url,
                    ats="Lever",
                )
            )

    elif target.ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{target.slug}"
        payload = fetch_json(url)
        raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        for item in raw_jobs:
            if not isinstance(item, dict) or "id" not in item:
                continue
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("jobUrl") or item.get("applyUrl") or "").strip()
            location = str(item.get("location") or "").strip()
            postings.append(
                JobPosting(
                    job_id=f"ab:{target.slug}:{item['id']}",
                    company=target.company,
                    title=title,
                    location=location,
                    url=job_url,
                    ats="Ashby",
                )
            )

    else:
        raise ValueError(f"unsupported ats provider: {target.ats}")

    return postings


def is_relevant_canadian_role(job: JobPosting) -> bool:
    if not job.title or not job.url:
        return False
    # require explicit canadian signal in location or title so us-only postings do not leak
    combined_loc_text = f"{job.location} {job.title}"
    has_canada = LOCATION_PATTERN.search(combined_loc_text) is not None
    has_role = ROLE_PATTERN.search(job.title) is not None
    return has_canada and has_role


def load_seen_ids(path: Path) -> tuple[set[str], bool]:
    # return (seen_ids, is_initial_seed_run)
    if not path.exists():
        return set(), True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(item) for item in data}, False
        if isinstance(data, dict) and isinstance(data.get("seen_ids"), list):
            return {str(item) for item in data["seen_ids"]}, False
    except (OSError, json.JSONDecodeError):
        pass
    return set(), True


def save_seen_ids(path: Path, seen_ids: set[str]) -> None:
    # write via a temporary file first to avoid corrupt json on interrupted writes
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    serialized = json.dumps(sorted(seen_ids), indent=2) + "\n"
    tmp_path.write_text(serialized, encoding="utf-8")
    tmp_path.replace(path)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_embed(job: JobPosting) -> dict:
    title = truncate(f"{job.company} — {job.title}", DISCORD_TITLE_MAX_LEN)
    loc_display = job.location if job.location else "Not specified"
    desc = truncate(
        f"**Location:** {loc_display}\n**Source:** {job.ats}",
        DISCORD_DESC_MAX_LEN,
    )
    return {
        "title": title,
        "url": job.url,
        "description": desc,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def post_discord_batches(webhook_url: str, matches: list[JobPosting]) -> None:
    for start in range(0, len(matches), DISCORD_MAX_EMBEDS_PER_MESSAGE):
        chunk = matches[start : start + DISCORD_MAX_EMBEDS_PER_MESSAGE]
        payload = {"embeds": [build_embed(job) for job in chunk]}
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "CanadaInternAlertBot/1.0 (+github-actions)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            _ = resp.read()
        # brief pause between multi-embed messages to respect webhook rate limits
        if start + DISCORD_MAX_EMBEDS_PER_MESSAGE < len(matches):
            time.sleep(0.6)


def main() -> int:
    webhook_url = os.environ.get(WEBHOOK_URL_ENV, "").strip()
    if not webhook_url:
        print(f"error: {WEBHOOK_URL_ENV} environment variable is missing or empty", file=sys.stderr)
        return 1

    seen_ids, is_initial_run = load_seen_ids(SEEN_FILE)
    new_matches: list[JobPosting] = []
    board_errors = 0

    for target in TARGETS:
        try:
            postings = parse_board_jobs(target)
        except Exception as exc:
            board_errors += 1
            print(f"warning: failed polling {target.company} ({target.slug}): {exc}", file=sys.stderr)
            continue

        for posting in postings:
            if posting.job_id in seen_ids:
                continue
            seen_ids.add(posting.job_id)
            if not is_initial_run and is_relevant_canadian_role(posting):
                new_matches.append(posting)

    if new_matches and not is_initial_run:
        try:
            post_discord_batches(webhook_url, new_matches)
        except Exception as exc:
            print(f"error: failed sending discord webhook: {exc}", file=sys.stderr)
            return 1

    try:
        save_seen_ids(SEEN_FILE, seen_ids)
    except OSError as exc:
        print(f"error: failed saving {SEEN_FILE}: {exc}", file=sys.stderr)
        return 1

    mode_label = "seeded baseline" if is_initial_run else f"sent {len(new_matches)} alert(s)"
    print(f"checked {len(TARGETS)} boards ({board_errors} error(s)); {mode_label}; total tracked ids: {len(seen_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())