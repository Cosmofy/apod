"""NASA Earth Observatory Image of the Day ingestion."""
import re
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from app.errors import Code, Error
from app.source import EarthObservatoryPicture

RSS_URL = "https://science.nasa.gov/feed/earth-observatory/image-of-the-day"
WP_POST_URL = "https://science.nasa.gov/wp-json/wp/v2/posts"


class _Paragraphs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_paragraph = False
        self._current: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p":
            self._in_paragraph = True
            self._current = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._in_paragraph:
            text = " ".join("".join(self._current).split())
            if text:
                self.paragraphs.append(text)
            self._in_paragraph = False

    def handle_data(self, data: str) -> None:
        if self._in_paragraph:
            self._current.append(data)


def _text(html: str) -> str:
    parser = _Paragraphs()
    parser.feed(html)
    if parser.paragraphs:
        return "\n\n".join(parser.paragraphs)
    return " ".join(re.sub(r"<[^>]+>", " ", unescape(html)).split())


def _article_body(html: str) -> str:
    marker = 'usa-article-content"><div class="entry-content">'
    start = html.find(marker)
    if start < 0:
        return _text(html)
    body = html[start + len(marker):]
    # Downloads and related-content are not part of the editorial explanation.
    body = body.split('class="hds-featured-file-list', 1)[0]
    return _text(body)


def _first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return unescape(match.group(1)).strip() if match else None


def _parse_image_date(body: str) -> date | None:
    match = re.search(r"(?:on|dated)\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", body)
    if not match:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(match.group(1), "%B %d, %Y").date()
    except ValueError:
        return None


def _parse_location(body: str) -> str | None:
    match = re.search(r"(?:orbiting|flying)\s+over\s+([^\.]+)", body, re.IGNORECASE)
    return f"over {match.group(1).strip()}" if match else None


async def fetch_earth_observatory_picture(client: httpx.AsyncClient) -> EarthObservatoryPicture:
    try:
        feed_response = await client.get(RSS_URL, follow_redirects=True)
        feed_response.raise_for_status()
        root = ET.fromstring(feed_response.content)
        item = root.find("./channel/item")
        if item is None or not item.findtext("link") or not item.findtext("pubDate"):
            raise ValueError("feed has no item")
        article_url = item.findtext("link").strip()
        published_date = parsedate_to_datetime(item.findtext("pubDate")).date()
        slug = urlparse(article_url).path.rstrip("/").split("/")[-1]
        post_response = await client.get(WP_POST_URL, params={"slug": slug}, follow_redirects=True)
        post_response.raise_for_status()
        posts = post_response.json()
        if not isinstance(posts, list) or len(posts) != 1:
            raise ValueError("article post missing")
        post = posts[0]
        article_response = await client.get(article_url, follow_redirects=True)
        article_response.raise_for_status()
    except (httpx.HTTPError, ET.ParseError, ValueError, KeyError, TypeError):
        raise Error(Code.EARTH_OBSERVATORY_UNAVAILABLE) from None

    article_html = article_response.text
    explanation = _article_body(article_html)
    media_url = _first_match(r'href=["\']([^"\']+_lrg\.(?:jpg|jpeg|png|webp))["\']', article_html)
    title = _text(post.get("title", {}).get("rendered", ""))
    if not title or not explanation or not media_url:
        raise Error(Code.INVALID_EARTH_OBSERVATORY_RESPONSE)

    acf = post.get("acf") or {}
    def coordinate(key: str) -> float | None:
        value = acf.get(key)
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    credit_match = re.search(r"(Astronaut photograph.*?)(?:Story by|NASA Earth Observatory/)", explanation, re.IGNORECASE | re.DOTALL)
    credit = " ".join(credit_match.group(1).split()) if credit_match else None
    return EarthObservatoryPicture(
        date=published_date,
        title=title,
        explanation=explanation,
        url=media_url,
        url_fallback=post.get("featured_image_url"),
        credit=credit,
        article_url=article_url,
        image_date=_parse_image_date(explanation),
        location_name=_parse_location(explanation),
        latitude=coordinate("latitude_coordinate"),
        longitude=coordinate("longitude_coordinate"),
    )
