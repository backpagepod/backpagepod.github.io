#!/usr/bin/env python3
"""Create draft episode posts from the saved Patreon RSS feed.

Usage (from the scripts/ folder, which holds the uv project):
    uv run draft_from_feed.py                  # dry run: list what would be created
    uv run draft_from_feed.py --write          # create the draft folders
    uv run draft_from_feed.py --since 2026-08-11 --write

Only standard-library Python is used, so `python3 draft_from_feed.py` works too.

What it does, for each "Episode N", "XL Episode N" or "XXL Episode N" post in the feed
that doesn't already have a post (matched on the Patreon link):
  - makes content/episode/<date>-<type>-<num>-<slug>/index.md, marked draft: true
  - fills in title, date, Patreon link, tags, and the description quoted from the feed
  - leaves TODOs for the summary check, cover image and your own notes

It never overwrites an existing folder and never writes anything from the feed except
title, date, link and description (the feed's audio URLs contain your private token).
"""
from __future__ import annotations

import argparse
import email.utils
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPISODES = ROOT / "content" / "episode"
TRANSCRIPTS = ROOT / "content" / "transcripts"

TITLE_RE = re.compile(r"^(?P<kind>XXL Episode|XL Episode|Episode)\s+(?P<num>\d+)(?::\s*(?P<rest>.*))?$")
GUEST_RE = re.compile(r"\(with ([^)]+)\)\s*$")

# Extra tags for regular episodes, chosen from words in the title.
TITLE_TAGS = [
    ("What We've Been Playing", "WWBP"),
    ("Mailbag", "Mailbag"),
    ("Best Games of", "GOTY"),
    ("Games Court", "Games Court"),
    ("Game Design Crimes", "Game Design Crimes"),
    ("Draft", "Draft"),
    ("Hall of Fame", "Hall of Fame"),
]


class DescriptionToMarkdown(HTMLParser):
    """Turn the feed's HTML description into Markdown paragraphs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.current = ""
        self.href: str | None = None
        self.link_text = ""

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", self.current.replace("\xa0", " ")).strip()
        if text:
            self.paragraphs.append(text)
        self.current = ""

    def _write(self, s: str) -> None:
        if self.href is not None:
            self.link_text += s
        else:
            self.current += s

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "div", "li", "h1", "h2", "h3"):
            self._flush()
            if tag == "li":
                self.current = "- "
        elif tag == "br":
            self._flush()
        elif tag == "a":
            self.href = dict(attrs).get("href", "")
            self.link_text = ""
        elif tag in ("strong", "b"):
            self._write("**")
        elif tag in ("em", "i"):
            self._write("*")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li", "h1", "h2", "h3"):
            self._flush()
        elif tag == "a" and self.href is not None:
            text, href = self.link_text.strip(), self.href
            self.href = None
            self.current += f"[{text}]({href})" if href else text
        elif tag in ("strong", "b"):
            self._write("**")
        elif tag in ("em", "i"):
            self._write("*")

    def handle_data(self, data):
        self._write(data)

    def markdown(self) -> list[str]:
        self._flush()
        return self.paragraphs


def description_paragraphs(raw: str) -> list[str]:
    parser = DescriptionToMarkdown()
    parser.feed(raw or "")
    return parser.markdown()


def slugify(text: str, max_words: int = 6) -> str:
    text = text.lower().replace("'", "").replace("’", "")
    text = GUEST_RE.sub("", text)
    words = re.findall(r"[a-z0-9]+", text)
    return "-".join(words[:max_words])


def guests(rest: str) -> list[str]:
    m = GUEST_RE.search(rest or "")
    if not m:
        return []
    return [g.strip() for g in re.split(r",\s*|\s+and\s+|\s*&\s*", m.group(1)) if g.strip()]


def existing_links() -> set[str]:
    links: set[str] = set()
    for f in EPISODES.glob("*/index.md"):
        links.update(re.findall(r"https://www\.patreon\.com/[^\s)]+", f.read_text(encoding="utf-8")))
    return {l.split("?")[0].replace("/backpagepod/posts/", "/posts/") for l in links}


def norm_link(link: str) -> str:
    return link.split("?")[0].replace("/backpagepod/posts/", "/posts/")


def latest_post_date() -> date:
    dates = [d for d in (re.match(r"(\d{4}-\d{2}-\d{2})-", p.name) for p in EPISODES.iterdir()) if d]
    return max(date.fromisoformat(d.group(1)) for d in dates)


def yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON strings are valid YAML double-quoted strings


def build_post(kind: str, num: int, rest: str, title: str, day: date, link: str, desc: list[str]) -> tuple[str, str]:
    if kind == "XL Episode":
        prefix, tags, transcript = f"xl-{num}", ["XL"], f"xl-{num:03d}.md"
    elif kind == "XXL Episode":
        prefix, tags, transcript = f"xxl-{num}", ["XXL"], f"xxl-{num:03d}.md"
    else:
        prefix, tags, transcript = f"episode-{num}", ["Regular"], f"ep-{num}.md"
        for needle, tag in TITLE_TAGS:
            if needle.lower() in (rest or "").lower():
                tags.append(tag)
        if tags[1:2] == ["WWBP"]:
            prefix += "-wwbp"
    names = guests(rest)
    if names:
        tags += ["Special Guest", *names]

    slug = slugify(rest.replace("What We've Been Playing", "") if "WWBP" in tags else rest)
    folder = f"{day.isoformat()}-{prefix}" + (f"-{slug}" if slug else "")

    summary = desc[0] if desc else ""
    first_sentence = re.match(r"(.+?[.!?])(\s|$)", summary)
    if first_sentence:
        summary = first_sentence.group(1)
    summary = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", summary)  # no links in the summary

    has_transcript = (TRANSCRIPTS / transcript).exists()
    lines = [
        "---",
        "categories:",
        "- Episode",
        f'date: "{day.isoformat()}T09:00:00Z"',
        "draft: true",
        f"summary: {yaml_str(summary)}  # TODO: check summary",
        "tags:",
        *[f"- {t}" for t in tags],
        f"title: {yaml_str(title)}",
        "cover: ",
        "  image: cover.png  # TODO: add cover image to this folder",
        "  hidden: false",
        "  hiddenInList: false",
        "  hiddenInSingle: false",
        "  relative: true",
        "---",
        "",
        f"[Play Episode]({link})",
    ]
    for i, para in enumerate(desc):
        if i:
            lines.append(">")
        lines.append(f"> {para}")
    lines += [
        "",
        f"<!-- TODO: your notes. Transcript: content/transcripts/{transcript}"
        + ("" if has_transcript else " (not found)") + " -->",
        "",
    ]
    return folder, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feed", default=str(ROOT / "patreon-feed.xml"), help="saved Patreon RSS file")
    ap.add_argument("--since", type=date.fromisoformat,
                    help="only posts on or after this date (default: day after the newest existing post)")
    ap.add_argument("--write", action="store_true", help="create the folders (default is a dry run)")
    args = ap.parse_args()

    feed = Path(args.feed)
    if not feed.exists():
        print(f"Feed not found: {feed}", file=sys.stderr)
        return 1
    since = args.since or date.fromordinal(latest_post_date().toordinal() + 1)
    have = existing_links()

    created = 0
    for item in reversed(ET.parse(feed).getroot().findall("./channel/item")):
        title = html.unescape((item.findtext("title") or "").strip())
        m = TITLE_RE.match(title)
        if not m:
            continue
        day = email.utils.parsedate_to_datetime(item.findtext("pubDate")).date()
        link = (item.findtext("link") or "").strip()
        if day < since or norm_link(link) in have:
            continue
        folder, text = build_post(m["kind"], int(m["num"]), m["rest"] or "", title, day, link,
                                  description_paragraphs(item.findtext("description") or ""))
        target = EPISODES / folder
        if target.exists():
            print(f"skip (folder exists): {folder}")
            continue
        print(("create " if args.write else "would create ") + folder)
        if args.write:
            target.mkdir(parents=True)
            (target / "index.md").write_text(text, encoding="utf-8")
        created += 1

    print(f"{created} post(s) {'created' if args.write else 'to create'} since {since}."
          + ("" if args.write else " Run again with --write to create them."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
