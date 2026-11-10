#!/usr/bin/env python3
"""Look up TV programme names on Wikipedia and print them in the site's canonical form.

Usage (from the scripts/ folder, which holds the uv project):
    uv run tv_names.py "twin peaks"            # one title
    uv run tv_names.py -f notes.md             # a list in a file, one programme per line
    pbpaste | uv run tv_names.py | pbcopy      # a list on the clipboard, replaced in place
    uv run tv_names.py                         # paste a list, then press Ctrl-D

Only standard-library Python is used, so `python3 tv_names.py` works too.
This is the TV version of game_names.py and film_names.py and works the same way.

Lines may start with a list marker ("- ", "* ", "1. "); it is kept. Blank lines and
anything that isn't a programme line (headings, code fences) are passed through untouched.

How a name is chosen:
  - Wikipedia is searched for the name, and for the name plus "TV series". Only pages
    that describe a TV programme are considered (not seasons, episodes, characters or
    people), and the name has to match the page title, a redirect to the page, or a
    bold name in its first sentence.
  - The full name that opens the first paragraph is preferred to the page title.
  - A UK / European title given in the first sentence ("broadcast in the UK as ...")
    is preferred to that.
  - "(TV series)" in a page title is dropped, but a year is kept:
    Battlestar Galactica (2004 TV series) -> Battlestar Galactica (2004)
  - If several pages or names fit equally well, they are all given, joined with " OR ":
    Battlestar Galactica (1978) OR Battlestar Galactica (2004)
    Where the page titles tell them apart by something other than a year, that is kept:
    The Office (British) OR The Office (American)
  - If nothing fits, the original name is kept with " (NOT FOUND)" added. If Wikipedia
    couldn't be reached, " (LOOKUP FAILED)" is added instead.

Use -v to see, on stderr, which Wikipedia page each name came from.
"""
from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "backpagepod-tv-names/1.0 (https://www.backpagepod.co.uk; fansite name checker)"
SEARCH_LIMIT = 8      # results taken from each of the two searches
ACCEPT_SCORE = 0.8    # a page has to match the name at least this well
MAX_OPTIONS = 4       # most alternatives to join with " OR "

ROMAN = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
    "viii": "8", "ix": "9", "x": "10", "xi": "11", "xii": "12", "xiii": "13",
    "xiv": "14", "xv": "15", "xvi": "16", "xvii": "17", "xviii": "18", "xix": "19",
    "xx": "20",
}
ARTICLES = {"the", "a", "an"}

# Words in a short description or first sentence that mean "TV programme".
TV_WORDS = (
    r"television|tv|miniseries|sitcom|soap opera|game show|talk show|chat show|quiz show|"
    r"web series|streaming series|animated series|anime series|drama series|comedy series|"
    r"reality series|reality show|panel show|telenovela|docuseries"
)
IS_TV = re.compile(rf"\b({TV_WORDS})\b", re.I)
# Short descriptions that mention television but aren't a programme.
NOT_TV = re.compile(
    r"\b(season|series \d+|episode|episodes|character|actor|actress|presenter|host|comedian|"
    r"writer|screenwriter|producer|director|journalist|personality|channel|network|station|"
    r"broadcaster|company|studio|block|list|film|movie|video game|soundtrack|album|song|"
    r"award|awards|born|genre|format)\b",
    re.I,
)
SERIES = re.compile(r"\bfranchise\b", re.I)   # a franchise page is only a fallback
LEAD_IS_TV = re.compile(rf"\b(?:is|was|are) an? [^.]{{0,150}}\b(?:{TV_WORDS})\b", re.I)
# Parts of a page-title suffix that say nothing: "(American TV series)" -> "American".
SUFFIX_NOISE = re.compile(
    r"\b(tv|television|web|animated|anime|series|serial|programme|program|show|miniseries|"
    r"sitcom|franchise|(19|20)\d\d)\b",
    re.I,
)
LEAD_IS_SERIES = re.compile(r"\b(?:is|was|are) an? [^.]{0,150}\bfranchise\b", re.I)

PAL_REGION = re.compile(
    r"\b(Europe|European|PAL|Australia|Australasia|Oceania|United Kingdom|UK)\b"
)
OTHER_REGION = re.compile(
    r"\b(Japan|Japanese|North America|North American|United States|US|Americas|Asia|"
    r"Korea|China|Western|the West|internationally|outside)\b"
)
NOT_A_TITLE = re.compile(r"abbreviat|shortened|stylized|stylised|simply|subtitled|acronym", re.I)
END_OF_NAMES = re.compile(r"\b(?:is|was|are|were)\s+(?:an?|the|one)\b")

LIST_MARKER = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$")
SKIP_LINE = re.compile(r"^\s*(```|~~~|#|$)")


class LookupFailed(Exception):
    pass


# --------------------------------------------------------------------------- Wikipedia

def api(params: dict[str, str]) -> dict:
    """One call to the Wikipedia API, with a single retry."""
    query = {"action": "query", "format": "json", "formatversion": "2", **params}
    request = urllib.request.Request(
        API + "?" + urllib.parse.urlencode(query), headers={"User-Agent": USER_AGENT}
    )
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
            if "error" in data:
                raise LookupFailed(data["error"].get("info", "Wikipedia API error"))
            time.sleep(0.1)  # be polite
            return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(1 + attempt)
    raise LookupFailed(str(last_error))


def search(text: str) -> list[dict]:
    data = api({
        "list": "search", "srsearch": text, "srlimit": str(SEARCH_LIMIT),
        "srprop": "redirecttitle", "srnamespace": "0",
    })
    return data.get("query", {}).get("search", [])


def page_details(pageids: list[int]) -> dict[int, dict]:
    """Intro HTML, short description and redirects for each page."""
    pages: dict[int, dict] = {}
    params = {
        "pageids": "|".join(str(i) for i in pageids),
        "prop": "extracts|description|redirects",
        "exintro": "1", "exlimit": "max", "rdlimit": "max", "rdnamespace": "0",
    }
    cont: dict[str, str] = {}
    for _ in range(6):
        data = api({**params, **cont})
        for page in data.get("query", {}).get("pages", []):
            entry = pages.setdefault(
                page["pageid"], {"title": page["title"], "extract": "", "description": "", "redirects": []}
            )
            entry["extract"] = entry["extract"] or page.get("extract", "")
            entry["description"] = entry["description"] or page.get("description", "")
            entry["redirects"] += [r["title"] for r in page.get("redirects", [])]
        if "continue" not in data:
            break
        cont = {k: str(v) for k, v in data["continue"].items()}
    return pages


# ----------------------------------------------------------------------------- matching

def tokens(text: str) -> list[str]:
    """Lower-case words with accents, punctuation and articles removed, and roman
    numerals turned into digits, so 'Dragon Quest 9' and 'Dragon Quest IX' compare equal."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("'", "").replace("’", "")
    words = re.findall(r"[a-z0-9]+", text)
    return [ROMAN.get(w, w) for w in words if w not in ARTICLES]


def match_score(query: list[str], name: str, exact_only: bool = False) -> float:
    candidate = tokens(name)
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    if exact_only:
        return 0.0
    if candidate[: len(query)] == query:          # "onimusha 2" -> "Onimusha 2: Samurai's Destiny"
        return 0.9
    if {t for t in query if t.isdigit()} != {t for t in candidate if t.isdigit()}:
        return 0.0                                # never confuse sequel numbers
    ratio = difflib.SequenceMatcher(None, " ".join(query), " ".join(candidate)).ratio()
    return ratio * 0.95 if ratio >= 0.85 else 0.0  # small typos


def split_title(title: str) -> tuple[str, str, str]:
    """'Battlestar Galactica (2004 TV series)' -> ('Battlestar Galactica', ' (2004)', '')
    'The Office (British TV series)' -> ('The Office', '', 'British')."""
    found = re.match(r"^(.*\S)\s+\(([^()]*)\)$", title)
    if not found:
        return title, "", ""
    year = re.search(r"\b(19|20)\d\d\b", found.group(2))
    extra = " ".join(SUFFIX_NOISE.sub(" ", found.group(2)).split())
    return found.group(1), (f" ({year.group(0)})" if year else ""), extra


def strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment)))


def first_paragraph(extract: str) -> str:
    extract = re.sub(r"<!--.*?-->", "", extract, flags=re.S)
    for paragraph in re.findall(r"<p\b[^>]*>(.*?)</p>", extract, flags=re.S):
        if strip_tags(paragraph).strip():
            return paragraph
    return ""


def page_kind(description: str, lead_text: str, title: str) -> str | None:
    """'tv', 'series' (a franchise) or None for anything else."""
    if title.startswith(("List of ", "Lists of ")):
        return None
    if description:
        if SERIES.search(description) and IS_TV.search(description + " " + lead_text[:300]):
            return "series"
        if not IS_TV.search(description) or NOT_TV.search(description):
            return None
        return "tv"
    if LEAD_IS_SERIES.search(lead_text):
        return "series" if IS_TV.search(lead_text[:400]) else None
    return "tv" if LEAD_IS_TV.search(lead_text) else None


def lead_names(paragraph: str) -> list[tuple[str, str]]:
    """The bold names at the start of the first paragraph, each tagged as
    'main', 'pal', 'other' (another region), 'alt' (untagged alternative) or 'skip'."""
    names: list[tuple[str, str]] = []
    bolds = list(re.finditer(r"<b>(.*?)</b>", paragraph, flags=re.S))
    if not bolds:
        return names
    # Only look at names in the opening clause, before "... is a British television sitcom".
    stop = END_OF_NAMES.search(paragraph, bolds[0].end())
    if stop:
        paragraph = paragraph[:stop.start()]
        bolds = [b for b in bolds if b.end() <= stop.start()]
    previous_end = 0
    for index, bold in enumerate(bolds):
        name = strip_tags(bold.group(1)).strip()
        before = strip_tags(paragraph[previous_end:bold.start()])
        next_start = bolds[index + 1].start() if index + 1 < len(bolds) else len(paragraph)
        after = strip_tags(paragraph[bold.end():next_start])
        previous_end = bold.end()
        if len(name) < 2:
            continue
        clause = re.split(r"[,;(]", after)[0][:80]   # e.g. " when localized to North America"
        if index == 0:
            tag = "main" if not before.strip() else "skip"
        elif NOT_A_TITLE.search(before) or before.rstrip().endswith("("):
            tag = "skip"
        elif PAL_REGION.search(clause):
            tag = "pal"
        elif OTHER_REGION.search(clause):
            tag = "other"
        elif PAL_REGION.search(before):
            tag = "pal"
        elif OTHER_REGION.search(before):
            tag = "other"
        else:
            tag = "alt"
        names.append((name, tag))
    return names


def display_names(title: str, paragraph: str) -> tuple[list[str], list[str], str]:
    """(names to print for this page, every name the page goes by, title qualifier)."""
    base, suffix, extra = split_title(title)
    found = lead_names(paragraph)
    main = base
    for name, tag in found:
        if tag == "main" and len(name) >= len(base):
            main = name
    pal = [name for name, tag in found if tag == "pal"]
    alts = [name for name, tag in found if tag == "alt" and tokens(name) != tokens(main)]
    chosen = pal[:1] if pal else [main] + alts
    known = [base, main] + [name for name, tag in found if tag != "skip"]
    return [name + suffix for name in chosen], known, extra


def canonical(name: str, verbose: bool = False) -> str:
    """The canonical form of one TV programme name, or the name with ' (NOT FOUND)'."""
    query = tokens(name)
    if not query:
        return name

    order: list[int] = []
    via_redirect: dict[int, list[str]] = {}
    for text in (name, f"{name} TV series"):
        for position, hit in enumerate(search(text)):
            if hit["pageid"] not in order:
                order.append(hit["pageid"])
            if hit.get("redirecttitle"):
                via_redirect.setdefault(hit["pageid"], []).append(hit["redirecttitle"])
    if not order:
        return f"{name} (NOT FOUND)"

    details = page_details(order[:20])
    candidates = []
    for rank, pageid in enumerate(order):
        page = details.get(pageid)
        if not page:
            continue
        paragraph = first_paragraph(page["extract"])
        kind = page_kind(page["description"], strip_tags(paragraph), page["title"])
        if kind is None:
            continue
        shown, known, extra = display_names(page["title"], paragraph)
        score = max(match_score(query, n) for n in known)
        redirects = page["redirects"] + via_redirect.get(pageid, [])
        score = max([score] + [match_score(query, r, exact_only=True) for r in redirects])
        candidates.append({"score": score, "film": kind == "tv", "rank": rank, "shown": shown, "extra": extra, "title": page["title"]})

    candidates = [c for c in candidates if c["score"] >= ACCEPT_SCORE]
    if not candidates:
        return f"{name} (NOT FOUND)"
    # Best score first; a programme beats a franchise page with the same score; then search order.
    candidates.sort(key=lambda c: (-c["score"], not c["film"], c["rank"]))
    best = candidates[0]
    chosen = [c for c in candidates if c["score"] == best["score"] and c["film"] == best["film"]]

    # If two pages would print the same name, tell them apart by the title qualifier.
    printed = [shown for c in chosen for shown in dict.fromkeys(c["shown"])]
    options: list[str] = []
    for candidate in chosen:
        for shown in candidate["shown"]:
            if printed.count(shown) > 1 and candidate["extra"]:
                shown = f"{shown} ({candidate['extra']})"
            if shown not in options:
                options.append(shown)
    if verbose:
        pages = ", ".join(f"[[{c['title']}]]" for c in chosen)
        print(f"{name!r}: {pages} (match {best['score']:.2f})", file=sys.stderr)
    return " OR ".join(options[:MAX_OPTIONS])


# ------------------------------------------------------------------------------- lists

def convert_line(line: str, verbose: bool = False) -> str:
    if SKIP_LINE.match(line):
        return line
    marker = LIST_MARKER.match(line)
    prefix, name = (marker.group(1), marker.group(2)) if marker else ("", line)
    name = name.strip()
    if not name:
        return line
    try:
        return prefix + canonical(name, verbose)
    except LookupFailed as error:
        print(f"Couldn't look up {name!r}: {error}", file=sys.stderr)
        return f"{prefix}{name} (LOOKUP FAILED)"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replace TV programme names with their canonical form, using Wikipedia.",
        epilog="With no title and no -f, a list is read from standard input.",
    )
    parser.add_argument("title", nargs="*", help="a single programme name (quotes are optional)")
    parser.add_argument("-f", "--file", help="read a list of names from this file, one per line")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the Wikipedia pages used (on stderr)")
    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    elif args.title:
        text = " ".join(args.title)
    else:
        if sys.stdin.isatty():
            print("Paste a list of TV programmes, one per line, then press Ctrl-D:", file=sys.stderr)
        text = sys.stdin.read()

    for line in text.splitlines():
        print(convert_line(line, args.verbose), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
