#!/usr/bin/env python3

"""
Montreal Cinema Showtime Aggregator

Scrapes current-day showtimes from Cinoche for seven Montreal cinemas
and generates an iCalendar feed at:

    montreal_cinema.ics

The scraper is intentionally defensive:
- Uses a realistic browser User-Agent.
- Retries transient HTTP failures.
- Supports canonical Cinoche URL fallbacks.
- Parses server-rendered HTML with BeautifulSoup.
- Handles multiple languages/formats per movie.
- Generates stable event UIDs.
- Uses America/Toronto for all local times.
- Refuses to replace the existing ICS file if every venue fails.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin

import pytz
import requests
from bs4 import BeautifulSoup, Tag
from icalendar import Calendar, Event
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_FILE = Path("montreal_cinema.ics")
TIMEZONE_NAME = "America/Toronto"
LOCAL_TZ = pytz.timezone(TIMEZONE_NAME)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = (10, 30)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# These are the URLs requested by the project, followed by canonical
# Cinoche URLs where Cinoche currently uses a different slug.
VENUES = [
    {
        "name": "Cinéma du Parc",
        "url": "https://www.cinoche.com/cinemas/cinema-du-parc",
        "fallback_urls": [],
    },
    {
        "name": "Cinéma du Musée",
        "url": "https://www.cinoche.com/cinemas/cinema-du-musee",
        "fallback_urls": [
            "https://www.cinoche.com/cinemas/cinemadumusee",
        ],
    },
    {
        "name": "Cinéma Moderne",
        "url": "https://www.cinoche.com/cinemas/cinema-moderne",
        "fallback_urls": [
            "https://www.cinoche.com/cinemas/cinemamoderne",
        ],
    },
    {
        "name": "Cineplex Forum",
        "url": "https://www.cinoche.com/cinemas/cineplex-cinemas-forum",
        "fallback_urls": [
            "https://www.cinoche.com/cinemas/amc-forum-22",
        ],
    },
    {
        "name": "Cineplex Banque Scotia",
        "url": (
            "https://www.cinoche.com/cinemas/"
            "cineplex-cinemas-banque-scotia-montreal"
        ),
        "fallback_urls": [
            "https://www.cinoche.com/cinemas/cinema-banque-scotia",
        ],
    },
    {
        "name": "Cineplex Quartier Latin",
        "url": (
            "https://www.cinoche.com/cinemas/"
            "cineplex-odeon-quartier-latin"
        ),
        "fallback_urls": [
            "https://www.cinoche.com/cinemas/quartier-latin",
        ],
    },
    {
        "name": "La Cinémathèque québécoise",
        "url": (
            "https://www.cinoche.com/cinemas/"
            "cinematheque-quebecoise"
        ),
        "fallback_urls": [],
    },
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("montreal-cinema")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Showtime:
    venue: str
    movie: str
    language: str
    show_date: date
    show_time: time
    source_url: str


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    """
    Build a requests Session with automatic retries for transient failures.
    """
    session = requests.Session()

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)

    return session


# ---------------------------------------------------------------------------
# Text / parsing helpers
# ---------------------------------------------------------------------------

TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

MONTHS = {
    # French
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,

    # English
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

DATE_RE = re.compile(
    r"^\s*(\d{1,2})\s+([A-Za-zÀ-ÿ]+)"
    r"(?:\s+\([^)]*\))?\s*$",
    re.IGNORECASE,
)

LANGUAGE_RE = re.compile(
    r"""
    (?:
        \bV\s*\.?\s*O\s*\.?
        |
        \bV\s*\.?\s*F\s*\.?
        |
        \bVOA\b
        |
        \bVOF\b
        |
        \bVOSTFR\b
        |
        \bVOSTA\b
        |
        \bVOST\b
        |
        \bVersion\s+originale\b
        |
        \bVersion\s+en\b
        |
        \bEnglish\s+Subtitles\b
        |
        \bFrench\s+Subtitles\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_times(value: str) -> list[str]:
    """
    Cinoche can render adjacent times as:

        13:2015:3018:40

    so we intentionally do not require word boundaries around HH:MM.
    """
    return TIME_RE.findall(value)


def is_language_text(value: str) -> bool:
    return bool(LANGUAGE_RE.search(value))


def normalize_language(raw: str) -> str:
    """
    Convert Cinoche's verbose language labels into compact calendar labels.

    Examples:
        V.O.A. -> VOA
        V.O.F. -> VOF
        V.O.A.S.-T.F. -> VOSTFR
        V.O.F.S.-T.A. -> VOF + English Subtitles
    """
    text = normalize_space(raw)
    upper = text.upper()

    # More specific combinations must be checked before generic V.O.A./V.O.F.
    if "V.O.A.S.-T.F." in upper or "VOA.S.-T.F." in upper:
        return "VOSTFR"

    if "V.O.A.S.-T.A." in upper or "VOA.S.-T.A." in upper:
        return "VOA + English Subtitles"

    if "V.O.F.S.-T.A." in upper or "VOF.S.-T.A." in upper:
        return "VOF + English Subtitles"

    if "V.O.F.S.-T.F." in upper or "VOF.S.-T.F." in upper:
        return "VOF + French Subtitles"

    if "V.O.COR.S.-T.A." in upper:
        return "Korean + English Subtitles"

    if "V.O.COR.S.-T.F." in upper:
        return "Korean + French Subtitles"

    if "V.O.INNUE.S.-T.F." in upper:
        return "Innu + French Subtitles"

    if "V.O.INNUE.S.-T.A." in upper:
        return "Innu + English Subtitles"

    if "V.O.INTER.S.-T.A." in upper:
        return "International + English Subtitles"

    if "V.O.INTER.S.-T.F." in upper:
        return "International + French Subtitles"

    if "V.O.A." in upper or re.search(r"\bVOA\b", upper):
        return "VOA"

    if "V.O.F." in upper or re.search(r"\bVOF\b", upper):
        return "VOF"

    if "V.F." in upper:
        return "VF"

    if "V.O." in upper:
        return "VO"

    # Generic fallback.
    cleaned = re.sub(r"\([^)]*\)", "", text)
    cleaned = normalize_space(cleaned)

    return cleaned[:100] if cleaned else "Language unspecified"


def parse_time(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour=hour, minute=minute)


def localized_datetime(
    show_date: date,
    show_time: time,
) -> datetime:
    """
    Create a timezone-aware America/Toronto datetime.

    pytz.localize() is used rather than assigning tzinfo directly.
    """
    naive = datetime.combine(show_date, show_time)
    return LOCAL_TZ.localize(naive, is_dst=None)


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

def extract_schedule_date(
    soup: BeautifulSoup,
    today: date,
) -> date:
    """
    Find Cinoche's active schedule date.

    Cinoche exposes date tabs as text such as:

        25 septembre (septembre)

    There can also be unrelated dates elsewhere on some pages, so we
    only accept text nodes that consist entirely of a day + month.
    """
    candidates: list[date] = []

    for text_node in soup.stripped_strings:
        text = normalize_space(text_node)
        match = DATE_RE.match(text)

        if not match:
            continue

        day = int(match.group(1))
        month_name = match.group(2).lower()

        if month_name not in MONTHS:
            continue

        month = MONTHS[month_name]

        for year in (today.year - 1, today.year, today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue

            # The active Cinoche schedule should be close to today.
            if abs((candidate - today).days) <= 10:
                candidates.append(candidate)

    if not candidates:
        logger.warning(
            "Could not determine Cinoche schedule date. "
            "Falling back to local date %s.",
            today.isoformat(),
        )
        return today

    return min(
        candidates,
        key=lambda candidate: abs((candidate - today).days),
    )


# ---------------------------------------------------------------------------
# Movie-card parsing
# ---------------------------------------------------------------------------

def get_unique_movie_links(element: Tag) -> set[str]:
    links = set()

    for anchor in element.select('a[href*="/films/"]'):
        href = anchor.get("href")
        if href:
            links.add(href.split("#", 1)[0])

    return links


def find_movie_card(title_anchor: Tag) -> Optional[Tag]:
    """
    Starting from a movie title link, walk upward until we find the smallest
    container that appears to represent exactly one movie and contains at
    least one showtime.

    This deliberately avoids depending on Cinoche's CSS class names, which
    are more likely to change than the semantic /films/ links.
    """
    current: Optional[Tag] = title_anchor

    for _ in range(10):
        if current is None:
            break

        if not isinstance(current, Tag):
            break

        unique_links = get_unique_movie_links(current)
        text = normalize_space(current.get_text(" ", strip=True))
        times = extract_times(text)

        if len(unique_links) == 1 and times:
            return current

        parent = current.parent

        if not isinstance(parent, Tag):
            break

        current = parent

    return None


def extract_movie_title(anchor: Tag) -> str:
    """
    Get the visible title from the /films/ anchor.

    Cinoche sometimes has extra nested/duplicated markup around titles, so
    normalize whitespace and discard obvious UI-only text.
    """
    title = normalize_space(anchor.get_text(" ", strip=True))

    title = re.sub(
        r"^\s*Nouveauté\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )

    return title


def parse_language_showtimes(
    card: Tag,
    show_date: date,
    venue_name: str,
    movie_title: str,
    source_url: str,
) -> list[Showtime]:
    """
    Walk text nodes in DOM order.

    Once a language label is encountered, subsequent time strings belong to
    that language until another language label appears.

    This handles cards such as:

        V.F.
        12:30 14:50 17:10

        V.O.A.
        14:15 16:40 19:00

    as well as premium-format variants such as:

        V.O.A. (3D)
        18:50
    """
    events: list[Showtime] = []
    current_language = "Language unspecified"

    for node in card.stripped_strings:
        text = normalize_space(node)

        if not text:
            continue

        if is_language_text(text):
            current_language = normalize_language(text)

        times = extract_times(text)

        if not times:
            continue

        for raw_time in times:
            try:
                parsed_time = parse_time(raw_time)
            except ValueError:
                logger.warning(
                    "Skipping invalid time %r for %s / %s",
                    raw_time,
                    venue_name,
                    movie_title,
                )
                continue

            events.append(
                Showtime(
                    venue=venue_name,
                    movie=movie_title,
                    language=current_language,
                    show_date=show_date,
                    show_time=parsed_time,
                    source_url=source_url,
                )
            )

    return events


# ---------------------------------------------------------------------------
# Venue scraping
# ---------------------------------------------------------------------------

def fetch_soup(
    session: requests.Session,
    urls: Iterable[str],
) -> tuple[BeautifulSoup, str]:
    """
    Try the requested URL followed by configured canonical fallbacks.
    """
    errors = []

    for url in urls:
        try:
            logger.info("Fetching %s", url)

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                errors.append(
                    f"{url}: HTTP {response.status_code}"
                )
                continue

            if not response.text.strip():
                errors.append(f"{url}: empty response")
                continue

            soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            return soup, response.url

        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")

    raise RuntimeError(
        "All URL attempts failed:\n"
        + "\n".join(f"  - {error}" for error in errors)
    )


def scrape_venue(
    session: requests.Session,
    venue: dict,
    today: date,
) -> list[Showtime]:
    urls = [venue["url"], *venue.get("fallback_urls", [])]

    soup, final_url = fetch_soup(session, urls)

    page_date = extract_schedule_date(
        soup=soup,
        today=today,
    )

    logger.info(
        "%s: schedule date detected as %s",
        venue["name"],
        page_date.isoformat(),
    )

    title_anchors = soup.select('a[href*="/films/"]')

    if not title_anchors:
        raise RuntimeError(
            f"{venue['name']}: no movie links found"
        )

    events: list[Showtime] = []
    seen_cards: set[int] = set()
    seen_events: set[tuple] = set()

    for title_anchor in title_anchors:
        movie_title = extract_movie_title(title_anchor)

        if not movie_title:
            continue

        card = find_movie_card(title_anchor)

        if card is None:
            logger.debug(
                "%s: unable to locate card for %s",
                venue["name"],
                movie_title,
            )
            continue

        card_identity = id(card)

        if card_identity in seen_cards:
            continue

        seen_cards.add(card_identity)

        card_events = parse_language_showtimes(
            card=card,
            show_date=page_date,
            venue_name=venue["name"],
            movie_title=movie_title,
            source_url=final_url,
        )

        for event in card_events:
            key = (
                event.venue,
                event.movie,
                event.language,
                event.show_date,
                event.show_time,
            )

            if key in seen_events:
                continue

            seen_events.add(key)
            events.append(event)

    logger.info(
        "%s: extracted %d showtimes",
        venue["name"],
        len(events),
    )

    return events


# ---------------------------------------------------------------------------
# iCalendar generation
# ---------------------------------------------------------------------------

def stable_uid(showtime: Showtime) -> str:
    """
    Stable UID so calendar clients can update existing events instead of
    treating each daily scraper run as a completely new set of events.
    """
    identity = "|".join(
        [
            showtime.venue,
            showtime.movie,
            showtime.language,
            showtime.show_date.isoformat(),
            showtime.show_time.strftime("%H:%M"),
        ]
    )

    digest = hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:24]

    return f"{digest}@montreal-cinema"


def build_calendar(events: list[Showtime]) -> Calendar:
    calendar = Calendar()

    calendar.add(
        "PRODID",
        "-//Montreal Cinema Showtime Aggregator//EN",
    )
    calendar.add(
        "VERSION",
        "2.0",
    )
    calendar.add(
        "CALSCALE",
        "GREGORIAN",
    )
    calendar.add(
        "METHOD",
        "PUBLISH",
    )
    calendar.add(
        "X-WR-CALNAME",
        "Montreal Cinema Showtimes",
    )
    calendar.add(
        "X-WR-CALDESC",
        (
            "Current-day movie showtimes for seven Montreal cinemas "
            "aggregated from Cinoche."
        ),
    )
    calendar.add(
        "X-WR-TIMEZONE",
        TIMEZONE_NAME,
    )

    generated_at = datetime.now(timezone.utc)

    for showtime in sorted(
        events,
        key=lambda item: (
            item.show_date,
            item.show_time,
            item.venue,
            item.movie,
            item.language,
        ),
    ):
        start = localized_datetime(
            showtime.show_date,
            showtime.show_time,
        )

        end = start + timedelta(hours=2)

        event = Event()

        event.add(
            "UID",
            stable_uid(showtime),
        )

        event.add(
            "DTSTAMP",
            generated_at,
        )

        event.add(
            "DTSTART",
            start,
        )

        event.add(
            "DTEND",
            end,
        )

        event.add(
            "SUMMARY",
            (
                f"[{showtime.venue}] "
                f"{showtime.movie} "
                f"({showtime.language})"
            ),
        )

        event.add(
            "LOCATION",
            showtime.venue,
        )

        event.add(
            "DESCRIPTION",
            (
                f"Language: {showtime.language}\n"
                f"Source: {showtime.source_url}"
            ),
        )

        event.add(
            "URL",
            showtime.source_url,
        )

        calendar.add_component(event)

    return calendar


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_calendar(
    calendar: Calendar,
    output_file: Path,
) -> None:
    """
    Write atomically so a failed/interrupted run cannot leave a truncated
    .ics file behind.
    """
    temporary_file = output_file.with_suffix(".ics.tmp")

    data = calendar.to_ical()

    temporary_file.write_bytes(data)

    temporary_file.replace(output_file)

    logger.info(
        "Wrote %d bytes to %s",
        output_file.stat().st_size,
        output_file,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    today = datetime.now(LOCAL_TZ).date()

    logger.info(
        "Starting Montreal cinema sync for %s",
        today.isoformat(),
    )

    session = build_session()

    all_events: list[Showtime] = []
    successful_venues = 0
    failed_venues = 0

    for venue in VENUES:
        try:
            venue_events = scrape_venue(
                session=session,
                venue=venue,
                today=today,
            )

            successful_venues += 1
            all_events.extend(venue_events)

        except Exception as exc:
            failed_venues += 1

            logger.exception(
                "Failed to scrape %s: %s",
                venue["name"],
                exc,
            )

    # Do not destroy an existing working feed if Cinoche is temporarily
    # unavailable or its HTML changes across every venue.
    if successful_venues == 0:
        logger.error(
            "Every venue failed. Existing %s was left untouched.",
            OUTPUT_FILE,
        )
        return 1

    # Remove accidental cross-venue duplicates while preserving stable order.
    unique_events: dict[tuple, Showtime] = {}

    for event in all_events:
        key = (
            event.venue,
            event.movie,
            event.language,
            event.show_date,
            event.show_time,
        )
        unique_events[key] = event

    all_events = list(unique_events.values())

    logger.info(
        "Successful venues: %d/%d",
        successful_venues,
        len(VENUES),
    )

    logger.info(
        "Failed venues: %d/%d",
        failed_venues,
        len(VENUES),
    )

    logger.info(
        "Total unique showtimes: %d",
        len(all_events),
    )

    # A completely empty calendar from otherwise successful pages can be
    # legitimate, but log it loudly because it may indicate a parser change.
    if not all_events:
        logger.warning(
            "No showtimes were extracted. "
            "The generated calendar will be empty."
        )

    calendar = build_calendar(all_events)

    write_calendar(
        calendar=calendar,
        output_file=OUTPUT_FILE,
    )

    logger.info("Montreal cinema sync completed successfully.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
