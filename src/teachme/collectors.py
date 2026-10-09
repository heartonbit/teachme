import difflib
import email.utils
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from teachme.config import AppConfig
from teachme.models import NewsItem, Source, SourceReference


USER_AGENT = "TeachMeNewsDigest/0.1 (+public RSS/Atom reader)"
MAX_RESPONSE_BYTES = 5_000_000
ITEM_TEXT_LIMIT = 1_500
TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
LOGGER = logging.getLogger(__name__)


class CollectionError(Exception):
    """A source could not be collected through its configured public feed."""


class FeedLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return
        attributes = {key.lower(): value or "" for key, value in attrs}
        rel = set(attributes.get("rel", "").lower().split())
        mime = attributes.get("type", "").lower()
        if "alternate" in rel and ("rss" in mime or "atom" in mime or "xml" in mime):
            href = attributes.get("href", "").strip()
            if href:
                self.links.append(href)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and parts.scheme.lower() == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and parts.scheme.lower() == "https":
        netloc = netloc[:-4]
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/") or "/", urlencode(query), ""))


def _plain_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _child_text(element: ElementTree.Element, *names: str) -> str:
    expected = {name.lower() for name in names}
    for child in element.iter():
        if child is element or _local_name(child.tag) not in expected:
            continue
        text = " ".join(part.strip() for part in child.itertext() if part.strip())
        if text:
            return text
    return ""


def _entry_link(entry: ElementTree.Element, base_url: str) -> str:
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        rel = child.attrib.get("rel", "alternate")
        if href and rel in {"alternate", ""}:
            return urljoin(base_url, href)
        if child.text and child.text.strip():
            return urljoin(base_url, child.text.strip())
    return ""


def _parse_feed(content: bytes, feed_url: str, source: Source) -> list[NewsItem]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise CollectionError("Response was not a valid RSS or Atom feed.") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        entries = [node for node in root.iter() if _local_name(node.tag) == "item"]
    elif root_name == "feed":
        entries = [node for node in root if _local_name(node.tag) == "entry"]
    else:
        entries = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]

    items: list[NewsItem] = []
    for entry in entries:
        title = _plain_text(_child_text(entry, "title"))
        link = _entry_link(entry, feed_url)
        if not title or not link or not link.startswith(("http://", "https://")):
            continue
        published = _child_text(entry, "pubdate", "published", "updated", "date")
        try:
            parsed_date = email.utils.parsedate_to_datetime(published) if published else None
            if parsed_date and parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)
            published = parsed_date.astimezone(timezone.utc).isoformat() if parsed_date else "Unknown"
        except (TypeError, ValueError, OverflowError):
            published = published or "Unknown"
        summary = _plain_text(_child_text(entry, "description", "summary", "content", "encoded"))
        reference = SourceReference(source.id, source.name, title, link, published)
        items.append(
            NewsItem(
                title=title,
                url=link,
                published_at=published,
                summary=summary[:ITEM_TEXT_LIMIT],
                sources=[reference],
            )
        )
    return items


def _is_feed_document(content: bytes) -> bool:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return False
    return _local_name(root.tag) in {"rss", "feed", "rdf"}


class NewsCollector:
    def __init__(self, config: AppConfig, *, opener=urlopen, sleeper=time.sleep):
        self.config = config
        self.opener = opener
        self.sleeper = sleeper

    def _request(self, url: str) -> tuple[bytes, str, str]:
        current_url = url
        for attempt in range(self.config.http_max_retries + 1):
            request = Request(current_url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, text/html;q=0.8"})
            try:
                with self.opener(request, timeout=self.config.http_timeout_seconds) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    final_url = response.geturl() if hasattr(response, "geturl") else current_url
                    content_type = response.headers.get("Content-Type", "") if hasattr(response, "headers") else ""
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CollectionError("Feed response exceeded the 5 MB size limit.")
                return body, final_url, content_type
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= self.config.http_max_retries:
                    raise CollectionError(f"HTTP {exc.code} while requesting source feed.") from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                LOGGER.debug("Retrying source feed after HTTP %d (retry %d/%d).", exc.code, attempt + 1, self.config.http_max_retries)
                self._wait(attempt, retry_after)
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.http_max_retries:
                    raise CollectionError(f"Network error while requesting source feed ({type(exc).__name__}).") from exc
                LOGGER.debug("Retrying source feed after %s (retry %d/%d).", type(exc).__name__, attempt + 1, self.config.http_max_retries)
                self._wait(attempt, None)
        raise CollectionError("Feed request failed after retries.")

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        delay = min(30.0, 2.0 ** attempt)
        if retry_after:
            try:
                delay = min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    retry_at = email.utils.parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = min(30.0, max(0.0, (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        self.sleeper(delay)

    def collect_source(self, source: Source) -> list[NewsItem]:
        LOGGER.debug("Collecting public feed for source %s.", source.id)
        body, final_url, content_type = self._request(source.url)
        try:
            items = _parse_feed(body, final_url, source)
            if items or _is_feed_document(body) or "xml" in content_type.lower() or "rss" in content_type.lower() or "atom" in content_type.lower():
                return items[: self.config.max_articles_per_source]
        except CollectionError:
            pass

        parser = FeedLinkParser()
        try:
            parser.feed(body.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise CollectionError("Source page could not be parsed for a public feed link.") from exc
        feed_urls = list(dict.fromkeys(urljoin(final_url, href) for href in parser.links))
        if not feed_urls:
            raise CollectionError("No RSS or Atom feed was advertised by this source URL.")

        errors: list[str] = []
        for feed_url in feed_urls[:3]:
            try:
                feed_body, feed_final_url, _ = self._request(feed_url)
                items = _parse_feed(feed_body, feed_final_url, source)
                return items[: self.config.max_articles_per_source]
            except CollectionError as exc:
                errors.append(str(exc))
        raise CollectionError(errors[-1] if errors else "Could not read an advertised RSS or Atom feed.")

    def collect_all(self) -> tuple[list[NewsItem], dict[str, dict[str, object]]]:
        all_items: list[NewsItem] = []
        source_results: dict[str, dict[str, object]] = {}
        for source in self.config.sources:
            try:
                items = self.collect_source(source)
                all_items.extend(items)
                source_results[source.id] = {
                    "name": source.name,
                    "status": "success" if items else "empty",
                    "discovered": len(items),
                    "error_code": None,
                    "error": None,
                }
                LOGGER.debug("Source %s returned %d candidate item(s).", source.id, len(items))
            except CollectionError as exc:
                source_results[source.id] = {
                    "name": source.name,
                    "status": "failed",
                    "discovered": 0,
                    "error_code": "collection_error",
                    "error": str(exc),
                }
                LOGGER.debug("Source %s collection failed: %s", source.id, exc)
        return deduplicate_items(all_items), source_results


def deduplicate_items(items: list[NewsItem]) -> list[NewsItem]:
    unique: list[NewsItem] = []
    url_indexes: dict[str, int] = {}
    title_indexes: list[tuple[str, int]] = []
    for item in items:
        normalized_url = normalize_url(item.url)
        normalized_title = re.sub(r"[^\w]+", " ", item.title.casefold()).strip()
        duplicate_index = url_indexes.get(normalized_url)
        if duplicate_index is None and len(normalized_title.split()) >= 4:
            for previous_title, index in title_indexes:
                if difflib.SequenceMatcher(None, normalized_title, previous_title).ratio() >= 0.94:
                    duplicate_index = index
                    break
        if duplicate_index is None:
            index = len(unique)
            unique.append(item)
            url_indexes[normalized_url] = index
            title_indexes.append((normalized_title, index))
            continue

        existing = unique[duplicate_index]
        known_urls = {normalize_url(reference.url) for reference in existing.sources}
        for reference in item.sources:
            if normalize_url(reference.url) not in known_urls:
                existing.sources.append(reference)
                known_urls.add(normalize_url(reference.url))
        if len(item.summary) > len(existing.summary):
            existing.summary = item.summary
    return unique