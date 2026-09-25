import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
import pytz
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


TIMEZONE = pytz.timezone("America/Toronto")
OUTPUT_FILE = Path("montreal_cinema.ics")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
}

VENUES = [
    {
        "name": "Cinéma du Parc",
        "url": "https://www.cinoche.com/cinemas/cinema-du-parc",
    },
    {
        "name": "Cinéma du Musée",
        "url": "https://www.cinoche.com/cinemas/cinemadumusee",
    },
    {
        "name": "Cinéma Moderne",
        "url": "https://www.cinoche.com/cinemas/cinemamoderne",
    },
    {
        "name": "Cineplex Forum",
        "url": "https://www.cinoche.com/cinemas/amc-forum-22",
    },
    {
        "name": "Cineplex Banque Scotia",
        "url": "https://www.cinoche.com/cinemas/cinema-banque-scotia",
    },
    {
        "name": "Cineplex Quartier Latin",
        "url": "https://www.cinoche.com/cinemas/quartier-latin",
    },
    {
        "name": "La Cinémathèque québécoise",
        "url": "https://www.cinoche.com/cinemas/cinematheque-quebecoise",
    },
]


TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

LANGUAGE_RE = re.compile(
    r"^(V\.O\.|V\.F\.|V\.A\.|V\.O\.A\.|V\.O\.F\.|V\.O\.JAP\.|"
    r"V\.O\.COR\.|V\.O\.HINDI\.|V\.O\.PUNJABI\.|V\.O\.CHIN\.|"
    r"V\.O\.HARY\.|V\.O\.INNUE\.|V\.O\.INTER\.|V\.O\.ES\.|"
    r"V\.O\.F\.S\.|V\.O\.A\.S\.|V\.O\.COR\.S\.|V\.O\.HINDI\.S\.|"
    r"V\.O\.PUNJABI\.S\.|V\.O\.JAP\.S\.|V\.O\.CHIN\.S\.|"
    r"V\.O\.HARY\.S\.|V\.O\.INNUE\.S\.|V\.O\.INTER\.S\.|"
    r"V\.A\.S\.|V\.F\.S\.)",
    re.IGNORECASE,
)


def make_session():
    session = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(HEADERS)

    return session


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def is_language(text):
    text = clean_text(text)
    return bool(LANGUAGE_RE.match(text))


def normalize_language(text):
    text = clean_text(text)

    upper = text.upper()

    if "SOUS-TITRES EN FRANÇAIS" in upper:
        if "COR" in upper:
            return "Coréen, STFR"
        if "JAP" in upper:
            return "Japonais, STFR"
        if "HINDI" in upper:
            return "Hindi, STFR"
        if "PUNJABI" in upper:
            return "Punjabi, STFR"
        if "HARY" in upper:
            return "Haryanvi, STFR"
        if "INNUE" in upper:
            return "Innu, STFR"
        if "INTER" in upper:
            return "International, STFR"
        return "VO, STFR"

    if "SOUS-TITRES EN ANGLAIS" in upper:
        if "COR" in upper:
            return "Coréen, STAN"
        if "JAP" in upper:
            return "Japonais, STAN"
        if "HINDI" in upper:
            return "Hindi, STAN"
        if "PUNJABI" in upper:
            return "Punjabi, STAN"
        if "HARY" in upper:
            return "Haryanvi, STAN"
        if "CHIN" in upper:
            return "Chinois, STAN"
        if "INNUE" in upper:
            return "Innu, STAN"
        if "F" in upper:
            return "VO, STAN"
        return "VO, STAN"

    if "VERSION EN FRANÇAIS" in upper:
        return "VF"

    if "VERSION EN ANGLAIS" in upper:
        return "VOA"

    if "VERSION ORIGINALE EN FRANÇAIS" in upper:
        return "VOF"

    return text


def get_text_between(start_anchor, end_anchor):
    """
    Returns all visible text between two film links in document order.
    """
    pieces = []

    for item in start_anchor.next_elements:
        if item is end_anchor:
            break

        if hasattr(item, "strip"):
            value = item.strip()
            if value:
                pieces.append(value)

    return pieces


def extract_movies(soup):
    """
    Cinoche's cinema pages are essentially structured like:

        Movie title
        duration
        genre
        ...
        language
        showtimes
        language
        showtimes
        next movie

    We therefore use the film links as boundaries instead of relying
    on a specific CSS class that can change.
    """

    all_links = soup.select('a[href*="/films/"]')

    movie_anchors = []
    seen_urls = set()

    for anchor in all_links:
        href = anchor.get("href")
        if not href:
            continue

        full_url = urljoin("https://www.cinoche.com", href)

        title = clean_text(anchor.get_text(" ", strip=True))

        if not title:
            continue

        # Cinoche sometimes has a "Nouveauté" link pointing to the
        # exact same film URL before the actual title.
        if title.lower() == "nouveauté":
            continue

        if full_url in seen_urls:
            continue

        seen_urls.add(full_url)
        movie_anchors.append((anchor, title, full_url))

    movies = []

    for index, (anchor, title, url) in enumerate(movie_anchors):
        next_anchor = None

        if index + 1 < len(movie_anchors):
            next_anchor = movie_anchors[index + 1][0]

        pieces = get_text_between(anchor, next_anchor)

        if not pieces:
            continue

        # Find language/showtime groups.
        current_language = None
        showtimes = []

        for piece in pieces:
            text = clean_text(piece)

            if not text:
                continue

            if is_language(text):
                current_language = normalize_language(text)
                continue

            times = TIME_RE.findall(text)

            if times and current_language:
                for hour, minute in times:
                    showtimes.append(
                        {
                            "time": f"{int(hour):02d}:{minute}",
                            "language": current_language,
                        }
                    )

        # Remove duplicates while preserving order.
        unique_showtimes = []
        seen = set()

        for item in showtimes:
            key = (item["time"], item["language"])

            if key not in seen:
                seen.add(key)
                unique_showtimes.append(item)

        if unique_showtimes:
            movies.append(
                {
                    "title": title,
                    "url": url,
                    "showtimes": unique_showtimes,
                }
            )

    return movies


def scrape_venue(session, venue):
    print(f"\nScraping {venue['name']}...")
    print(f"URL: {venue['url']}")

    response = session.get(venue["url"], timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    movies = extract_movies(soup)

    print(f"Found {len(movies)} movies.")

    total_showtimes = sum(len(movie["showtimes"]) for movie in movies)
    print(f"Found {total_showtimes} showtimes.")

    for movie in movies:
        print(f"  {movie['title']}")

        for showtime in movie["showtimes"]:
            print(
                f"    {showtime['time']} "
                f"({showtime['language']})"
            )

    return movies


def create_calendar(all_showtimes):
    calendar = Calendar()

    calendar.add("prodid", "-//Montreal Cinema Showtime Aggregator//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add(
        "x-wr-caldesc",
        "Current-day movie showtimes for seven Montreal cinemas aggregated from Cinoche.",
    )
    calendar.add("x-wr-calname", "Montreal Cinema Showtimes")
    calendar.add("x-wr-timezone", "America/Toronto")

    for item in all_showtimes:
        start = TIMEZONE.localize(
            datetime.combine(
                item["date"],
                datetime.strptime(item["time"], "%H:%M").time(),
            )
        )

        end = start + timedelta(hours=2)

        event = Event()

        event.add(
            "summary",
            f"[{item['venue']}] {item['title']} ({item['language']})",
        )

        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("location", item["venue"])

        event.add(
            "description",
            f"Showtime found on Cinoche.com\n{item['url']}",
        )

        event.add("uid", item["uid"])
        event.add("dtstamp", datetime.now(TIMEZONE))

        calendar.add_component(event)

    return calendar


def main():
    today = datetime.now(TIMEZONE).date()

    print("=" * 60)
    print("Montreal Cinema Showtime Aggregator")
    print("=" * 60)
    print(f"Date: {today}")
    print()

    session = make_session()

    all_showtimes = []
    successful_venues = 0

    for venue in VENUES:
        try:
            movies = scrape_venue(session, venue)

            if movies:
                successful_venues += 1

            for movie in movies:
                for showtime in movie["showtimes"]:
                    uid = (
                        f"{today.isoformat()}-"
                        f"{venue['name']}-"
                        f"{movie['title']}-"
                        f"{showtime['time']}-"
                        f"{showtime['language']}"
                    )

                    all_showtimes.append(
                        {
                            "date": today,
                            "venue": venue["name"],
                            "title": movie["title"],
                            "time": showtime["time"],
                            "language": showtime["language"],
                            "url": movie["url"],
                            "uid": uid,
                        }
                    )

        except Exception as exc:
            print(f"ERROR scraping {venue['name']}: {exc}")

    print()
    print("=" * 60)
    print(f"Successful venues: {successful_venues}/{len(VENUES)}")
    print(f"Total showtimes: {len(all_showtimes)}")
    print("=" * 60)

    # Never overwrite a working calendar with an empty one.
    if successful_venues == 0:
        raise RuntimeError(
            "All cinema scrapes failed. Existing calendar was not replaced."
        )

    if not all_showtimes:
        raise RuntimeError(
            "The scraper reached Cinoche successfully but found "
            "zero showtimes. Existing calendar was not replaced."
        )

    calendar = create_calendar(all_showtimes)

    temp_file = OUTPUT_FILE.with_suffix(".tmp")

    with open(temp_file, "wb") as f:
        f.write(calendar.to_ical())

    temp_file.replace(OUTPUT_FILE)

    print(f"\nCalendar written to: {OUTPUT_FILE}")
    print(f"Events written: {len(all_showtimes)}")


if __name__ == "__main__":
    main()
