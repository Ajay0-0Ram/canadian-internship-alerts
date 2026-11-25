from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import html
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
    description: str
    url: str
    ats: str


# verified public boards across greenhouse, lever, and ashby
TARGETS: tuple[BoardTarget, ...] = (
    # greenhouse
    BoardTarget(ats="greenhouse", company="Pinterest", slug="pinterest"),
    BoardTarget(ats="greenhouse", company="DoorDash Canada", slug="doordashcanada"),
    BoardTarget(ats="greenhouse", company="Lyft", slug="lyft"),
    BoardTarget(ats="greenhouse", company="Plusgrade", slug="plusgrade"),
    BoardTarget(ats="greenhouse", company="Genesys", slug="genesys"),
    BoardTarget(ats="greenhouse", company="PagerDuty", slug="pagerduty"),
    BoardTarget(ats="greenhouse", company="Datadog", slug="datadog"),
    BoardTarget(ats="greenhouse", company="Cloudflare", slug="cloudflare"),
    BoardTarget(ats="greenhouse", company="StackAdapt", slug="stackadapt"),
    BoardTarget(ats="greenhouse", company="Ada", slug="ada"),
    BoardTarget(ats="greenhouse", company="Vena Solutions", slug="venasolutions"),
    BoardTarget(ats="greenhouse", company="TouchBistro", slug="touchbistro"),
    BoardTarget(ats="greenhouse", company="BitGo", slug="bitgo"),
    BoardTarget(ats="greenhouse", company="Geotab", slug="geotab"),
    BoardTarget(ats="greenhouse", company="Hootsuite", slug="hootsuite"),
    BoardTarget(ats="greenhouse", company="Tailscale", slug="tailscale"),
    BoardTarget(ats="greenhouse", company="Verkada", slug="verkada"),
    BoardTarget(ats="greenhouse", company="Tenstorrent University", slug="tenstorrentuniversity"),
    # lever
    BoardTarget(ats="lever", company="EQ Bank", slug="eqbank"),
    BoardTarget(ats="lever", company="Palantir", slug="palantir"),
    BoardTarget(ats="lever", company="KOHO", slug="koho"),
    BoardTarget(ats="lever", company="ApplyBoard", slug="applyboard"),
    BoardTarget(ats="lever", company="FreshBooks", slug="freshbooks"),
    BoardTarget(ats="lever", company="League", slug="league"),
    # ashby
    BoardTarget(ats="ashby", company="Wealthsimple", slug="wealthsimple"),
    BoardTarget(ats="ashby", company="1Password", slug="1password"),
    BoardTarget(ats="ashby", company="Cohere", slug="cohere"),
    BoardTarget(ats="ashby", company="Float Financial", slug="floatfinancial"),
    BoardTarget(ats="ashby", company="Notion", slug="notion"),
    BoardTarget(ats="ashby", company="Linear", slug="linear"),
    BoardTarget(ats="ashby", company="Ramp", slug="ramp"),
    BoardTarget(ats="ashby", company="Rivian & VW Group Tech", slug="rivianvw.tech"),
)

# require both an internship/co-op signal and an engineering/tech track in the title
INTERN_TITLE_PATTERN = re.compile(
    r"\b(intern|internship|co-?op|student)\b",
    re.IGNORECASE,
)

TRACK_TITLE_PATTERN = re.compile(
    r"\b("
    r"software(\s+(engineer|developer|development))?|swe|developer|engineer(ing)?|"
    r"full[\s-]?stack|back[\s-]?end|applied\s+ai|ai(\s+(engineer|developer))?|"
    r"machine\s+learning|ml(\s+engineer)?|platform(\s+engineer)?|"
    r"cloud(\s+engineer)?|infrastructure|security(\s+engineer)?|application\s+security|appsec"
    r")\b",
    re.IGNORECASE,
)

# exclude senior roles, off-cycle terms, multi-term durations, and hardware/non-swe tracks
EXCLUDE_TITLE_PATTERN = re.compile(
    r"\b("
    r"senior|sr\.?|staff|principal|lead|manager|director|vp|head\s+of|"
    r"winter\s*2027|january\s+start|fall\s*2027|autumn\s*2027|"
    r"(?:8|12|16)[\s-]?months?|"
    r"firmware|fpga|asic|soc|rtl|verilog|vhdl|hardware|pcb|rf|analog|"
    r"robotics|mechatronics|embodied\s+ai|"
    r"data\s*center\s+tech(?:nician)?|critical\s+environment|"
    r"salesforce|product\s+manager|ux/ui|designer|unpaid|volunteer"
    r")\b",
    re.IGNORECASE,
)

# exclude off-cycle terms or long durations if explicitly stated in the body
EXCLUDE_BODY_PATTERN = re.compile(
    r"\b("
    r"winter\s*2027|january\s*2027\s+start|fall\s*2027|autumn\s*2027|"
    r"(?:8|12|16)[\s-]?months?\s+(?:co-?op|internship|term|placement)"
    r")\b",
    re.IGNORECASE,
)

# explicit ontario/canadian cities and regions
CANADIAN_LOC_PATTERN = re.compile(
    r"\b("
    r"canada|canadian|ontario|gta|greater\s+toronto\s+area|"
    r"oshawa|whitby|ajax|pickering|markham|richmond\s+hill|vaughan|thornhill|"
    r"toronto|north\s+york|scarborough|etobicoke|mississauga|oakville|burlington|"
    r"waterloo|kitchener|ottawa|"
    r"remote\s*[-–—]?\s*(?:canada|americas)"
    r")\b",
    re.IGNORECASE,
)

# generic remote/hybrid markers (verified alongside canadian eligibility)
GENERIC_WORKMODE_PATTERN = re.compile(
    r"\b(remote|hybrid|flexible)\b",
    re.IGNORECASE,
)

# target stack and summer 2027 keywords searched across title + description
BODY_KEYWORDS: tuple[str, ...] = (
    "summer 2027",
    "may 2027",
    "june 2027",
    "4-month",
    "4 month",
    "16 weeks",
    "python",
    "fastapi",
    "aws",
    "cloud",
    "distributed",
    "api",
    "kotlin",
    "android",
    "hilt",
    "junit",
    "c#",
    ".net",
    "asp.net",
    "java",
    "javascript",
    "typescript",
    "react",
    "node.js",
    "sql",
    "docker",
    "kubernetes",
    "llm",
    "agentic",
    "inference",
    "security",
    "tls",
)


def compile_keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    # build boundary-safe regex that still matches symbols like c# and .net
    parts: list[str] = []
    for kw in keywords:
        escaped = re.escape(kw)
        prefix = r"(?<!\w)" if re.match(r"^\w", kw) else r"(?<!\S)"
        suffix = r"(?!\w)" if re.search(r"\w$", kw) else r"(?!\S)"
        parts.append(f"{prefix}{escaped}{suffix}")
    return re.compile("|".join(parts), re.IGNORECASE)


BODY_KEYWORD_PATTERN = compile_keyword_pattern(BODY_KEYWORDS)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def clean_html_text(raw: str) -> str:
    if not raw:
        return ""
    unescaped = html.unescape(raw)
    stripped = HTML_TAG_PATTERN.sub(" ", unescaped)
    return re.sub(r"\s+", " ", stripped).strip()


def fetch_json(url: str) -> object:
    # retry transient network or rate-limit errors with short backoff
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "CanadaInternAlertBot/2.0 (+github-actions)",
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
    loc_obj = item.get("location")
    if isinstance(loc_obj, dict) and isinstance(loc_obj.get("name"), str):
        return loc_obj["name"].strip()
    return ""


def extract_lever_locations(item: dict) -> list[str]:
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
        url = f"https://boards-api.greenhouse.io/v1/boards/{target.slug}/jobs?content=true"
        payload = fetch_json(url)
        raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        for item in raw_jobs:
            if not isinstance(item, dict) or "id" not in item:
                continue
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("absolute_url") or "").strip()
            location = extract_greenhouse_location(item)
            description = clean_html_text(str(item.get("content") or ""))
            postings.append(
                JobPosting(
                    job_id=f"gh:{target.slug}:{item['id']}",
                    company=target.company,
                    title=title,
                    location=location,
                    description=description,
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
            description = clean_html_text(
                str(item.get("descriptionPlain") or item.get("description") or "")
            )
            postings.append(
                JobPosting(
                    job_id=f"lv:{target.slug}:{item['id']}",
                    company=target.company,
                    title=title,
                    location=location,
                    description=description,
                    url=job_url,
                    ats="Lever",
                )
            )

    elif target.ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{target.slug}?includeCompensation=true"
        payload = fetch_json(url)
        raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        for item in raw_jobs:
            if not isinstance(item, dict) or "id" not in item:
                continue
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("jobUrl") or item.get("applyUrl") or "").strip()
            location = str(item.get("location") or "").strip()
            description = clean_html_text(
                str(item.get("descriptionPlain") or item.get("descriptionHtml") or "")
            )
            postings.append(
                JobPosting(
                    job_id=f"ab:{target.slug}:{item['id']}",
                    company=target.company,
                    title=title,
                    location=location,
                    description=description,
                    url=job_url,
                    ats="Ashby",
                )
            )

    else:
        raise ValueError(f"unsupported ats provider: {target.ats}")

    return postings


def evaluate_posting(job: JobPosting) -> list[str] | None:
    # return matched body/stack keywords if the posting satisfies all rules, else none
    if not job.title or not job.url:
        return None

    if EXCLUDE_TITLE_PATTERN.search(job.title):
        return None

    if not INTERN_TITLE_PATTERN.search(job.title):
        return None

    if not TRACK_TITLE_PATTERN.search(job.title):
        return None

    if job.description and EXCLUDE_BODY_PATTERN.search(job.description):
        return None

    loc_and_title = f"{job.location} {job.title}"
    has_canadian_loc = CANADIAN_LOC_PATTERN.search(loc_and_title) is not None

    # allow generic remote/hybrid only when canada is mentioned in the posting
    if not has_canadian_loc:
        has_generic_mode = GENERIC_WORKMODE_PATTERN.search(loc_and_title) is not None
        mentions_canada_in_body = re.search(r"\b(canada|canadian|ontario)\b", job.description, re.IGNORECASE) is not None
        if not (has_generic_mode and mentions_canada_in_body):
            return None

    searchable_text = f"{job.title} {job.description}"
    raw_matches = BODY_KEYWORD_PATTERN.findall(searchable_text)
    if not raw_matches:
        return None

    # deduplicate matched keywords while preserving display order
    deduped = list(dict.fromkeys(m.lower() for m in raw_matches))
    return deduped


def load_seen_ids(path: Path) -> tuple[set[str], bool]:
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
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    serialized = json.dumps(sorted(seen_ids), indent=2) + "\n"
    tmp_path.write_text(serialized, encoding="utf-8")
    tmp_path.replace(path)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_embed(job: JobPosting, matched_keywords: list[str]) -> dict:
    title = truncate(f"{job.company} — {job.title}", DISCORD_TITLE_MAX_LEN)
    loc_display = job.location if job.location else "Not specified"
    tags_display = ", ".join(matched_keywords[:12])
    desc = truncate(
        f"**Location:** {loc_display}\n"
        f"**Source:** {job.ats}\n"
        f"**Matched:** {tags_display}",
        DISCORD_DESC_MAX_LEN,
    )
    return {
        "title": title,
        "url": job.url,
        "description": desc,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def post_discord_batches(webhook_url: str, matches: list[tuple[JobPosting, list[str]]]) -> None:
    for start in range(0, len(matches), DISCORD_MAX_EMBEDS_PER_MESSAGE):
        chunk = matches[start : start + DISCORD_MAX_EMBEDS_PER_MESSAGE]
        payload = {"embeds": [build_embed(job, kws) for job, kws in chunk]}
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "CanadaInternAlertBot/2.0 (+github-actions)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            _ = resp.read()
        if start + DISCORD_MAX_EMBEDS_PER_MESSAGE < len(matches):
            time.sleep(0.6)


def main() -> int:
    webhook_url = os.environ.get(WEBHOOK_URL_ENV, "").strip()
    if not webhook_url:
        print(f"error: {WEBHOOK_URL_ENV} environment variable is missing or empty", file=sys.stderr)
        return 1

    seen_ids, is_initial_run = load_seen_ids(SEEN_FILE)
    new_matches: list[tuple[JobPosting, list[str]]] = []
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
            if not is_initial_run:
                matched_keywords = evaluate_posting(posting)
                if matched_keywords is not None:
                    new_matches.append((posting, matched_keywords))

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