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
WP_MEDIA_URL = "https://science.nasa.gov/wp-json/wp/v2/media/{attachment_id}"


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


def _wordpress_editorial_content(html: str) -> str:
    """Drop the secondary navigation prepended to migrated WP post content."""
    navigation_start = html.find('class="hds-secondary-navigation-menu-items"')
    if navigation_start >= 0:
        navigation_end = html.find("</nav>", navigation_start)
        if navigation_end >= 0:
            html = html[navigation_end + len("</nav>"):]
    return html.split('class="hds-content-lists-inner', 1)[0]


def _first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return unescape(match.group(1)).strip() if match else None


def _media_from_editorial_content(html: str) -> tuple[str, str] | None:
    """Return the first editorial image or video, never a navigation asset."""
    video = _first_match(r'<iframe[^>]+src=["\']([^"\']+(?:youtube\.com|youtu\.be)[^"\']*)["\']', html)
    if video:
        return "video", video
    image = _first_match(r'href=["\']([^"\']+_lrg\.(?:jpg|jpeg|png|webp))[^"\']*["\']', html)
    if image:
        return "image", image
    image = _first_match(r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp))[^"\']*["\']', html)
    return ("image", image) if image else None


async def _media_from_post(
    client: httpx.AsyncClient, post: dict, editorial_html: str
) -> tuple[str, str] | None:
    """Read editorial media, falling back to the migrated post attachment."""
    media = _media_from_editorial_content(editorial_html)
    if media is not None:
        return media

    metadata = post.get("meta") or {}
    attachment_ids = str(metadata.get("smd_core_meta_tracked_attachment_ids", "")).split(",")
    for attachment_id in attachment_ids:
        attachment_id = attachment_id.strip()
        if not attachment_id.isdigit():
            continue
        response = await client.get(WP_MEDIA_URL.format(attachment_id=attachment_id))
        response.raise_for_status()
        attachment = response.json()
        source_url = attachment.get("source_url")
        if attachment.get("media_type") == "image" and isinstance(source_url, str):
            return "image", source_url
    return None


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
    except (httpx.HTTPError, ET.ParseError, ValueError, KeyError, TypeError):
        raise Error(Code.EARTH_OBSERVATORY_UNAVAILABLE) from None

    editorial_html = _wordpress_editorial_content(post.get("content", {}).get("rendered", ""))
    explanation = _text(editorial_html)
    try:
        media = await _media_from_post(client, post, editorial_html)
    except (httpx.HTTPError, KeyError, TypeError):
        raise Error(Code.EARTH_OBSERVATORY_UNAVAILABLE) from None
    title = _text(post.get("title", {}).get("rendered", ""))
    if not title or not explanation or media is None:
        raise Error(Code.INVALID_EARTH_OBSERVATORY_RESPONSE)
    media_type, media_url = media

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
        media_type=media_type,
        url=media_url,
        url_fallback=post.get("featured_image_url") if media_type == "image" else None,
        credit=credit,
        article_url=article_url,
        image_date=_parse_image_date(explanation),
        location_name=_parse_location(explanation),
        latitude=coordinate("latitude_coordinate"),
        longitude=coordinate("longitude_coordinate"),
    )
