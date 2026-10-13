import difflib
import email.utils
import html
import json
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
BRUSSELS_TIMES_API = "https://apiv2.brusselstimes.com"
MAX_RESPONSE_BYTES = 5_000_000
ITEM_TEXT_LIMIT = 1_500
ARTICLE_TEXT_LIMIT = 6_000
TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
LOGGER = logging.getLogger(__name__)
IGNORED_HTML_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "aside", "form", "button", "iframe"}
PROMINENT_CLASS_WORDS = {"breaking", "featured", "headline", "hero", "lead", "main", "major", "top", "story", "article"}
EXCLUDED_CLASS_WORDS = {"breadcrumb", "footer", "menu", "navigation", "navbar", "pagination", "sidebar"}


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


@dataclass(frozen=True)
class ArticleCandidate:
    title: str
    url: str
    score: int
    order: int


class NewsLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []
        self.anchor: dict[str, object] | None = None
        self.candidates: list[ArticleCandidate] = []
        self.order = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        classes = (attributes.get("class", "") + " " + attributes.get("id", "")).lower()
        self.stack.append((tag, classes))
        if tag == "a" and attributes.get("href"):
            context = " ".join(value for _, value in self.stack)
            tokens = set(re.findall(r"[a-z0-9]+", context))
            if tokens.intersection(EXCLUDED_CLASS_WORDS) or any(parent_tag in {"nav", "footer", "aside"} for parent_tag, _ in self.stack[:-1]):
                score = -100
            else:
                score = sum(5 for word in PROMINENT_CLASS_WORDS if word in tokens)
                score += 1
                score += 6 if any(parent_tag in {"h1", "h2", "h3"} for parent_tag, _ in self.stack[:-1]) else 0
                score += 4 if any(parent_tag == "article" for parent_tag, _ in self.stack[:-1]) else 0
                score += 2 if any(parent_tag in {"li", "main"} for parent_tag, _ in self.stack[:-1]) else 0
            self.anchor = {"href": attributes["href"], "text": [], "score": score}

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self.anchor is not None:
            title = re.sub(r"\s+", " ", " ".join(self.anchor["text"])).strip()
            href = str(self.anchor["href"]).strip()
            if len(title) >= 24 and len(title.split()) >= 4:
                self.candidates.append(ArticleCandidate(title, href, int(self.anchor["score"]), self.order))
                self.order += 1
            self.anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.anchor is not None:
            self.anchor["text"].append(data)


class ArticleBodyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.ignored_depth = 0
        self.semantic_depth = 0
        self.has_semantic_content = False
        self.body_parts: list[str] = []
        self.semantic_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self.stack.append(tag)
        if tag in IGNORED_HTML_TAGS:
            self.ignored_depth += 1
        if tag in {"article", "main"}:
            self.semantic_depth += 1
            self.has_semantic_content = True
        if tag in {"p", "br", "div", "section", "h1", "h2", "h3", "li"} and self.ignored_depth == 0:
            self.body_parts.append("\n")
            if self.semantic_depth:
                self.semantic_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                removed = self.stack[index:]
                del self.stack[index:]
                self.ignored_depth = max(0, self.ignored_depth - sum(item in IGNORED_HTML_TAGS for item in removed))
                self.semantic_depth = max(0, self.semantic_depth - sum(item in {"article", "main"} for item in removed))
                break
        if tag in {"p", "div", "section", "h1", "h2", "h3", "li"} and self.ignored_depth == 0:
            self.body_parts.append("\n")
            if self.semantic_depth:
                self.semantic_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        self.body_parts.append(data)
        if self.semantic_depth:
            self.semantic_parts.append(data)

    def text(self) -> str:
        parts = self.semantic_parts if self.has_semantic_content else self.body_parts
        return re.sub(r"\s+", " ", " ".join(parts)).strip()


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


def _decode_html(content: bytes, content_type: str) -> str:
    charset = re.search(r"(?i)charset\s*=\s*[\"']?([a-z0-9._-]+)", content_type)
    encoding = charset.group(1) if charset else "utf-8"
    try:
        return content.decode(encoding, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _site_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _select_article_candidates(html_document: str, page_url: str, source_url: str, limit: int) -> list[ArticleCandidate]:
    parser = NewsLinkParser()
    try:
        parser.feed(html_document)
    except Exception as exc:
        raise CollectionError("News listing page could not be parsed.") from exc

    page_normalized = normalize_url(page_url)
    source_host = _site_host(source_url)
    candidates: list[ArticleCandidate] = []
    seen: set[str] = set()
    for candidate in parser.candidates:
        url = urljoin(page_url, candidate.url)
        parts = urlsplit(url)
        if candidate.score < 0 or parts.scheme not in {"http", "https"} or _site_host(url) != source_host:
            continue
        normalized = normalize_url(url)
        if normalized == page_normalized or normalized in seen:
            continue
        path_parts = {part.lower() for part in parts.path.split("/") if part}
        if path_parts.intersection({"tag", "tags", "category", "categories", "author", "authors", "search", "video", "videos"}):
            continue
        seen.add(normalized)
        candidates.append(ArticleCandidate(candidate.title, url, candidate.score, candidate.order))
    candidates.sort(key=lambda item: (-item.score, item.order))
    return candidates[:limit]


def _extract_article_text(content: bytes, content_type: str) -> str:
    document = _decode_html(content, content_type)
    parser = ArticleBodyParser()
    try:
        parser.feed(document)
    except Exception as exc:
        raise CollectionError("Article page could not be parsed.") from exc
    text = parser.text()
    if len(text) < 80:
        raise CollectionError("Article page did not contain enough readable text.")
    return text[:ARTICLE_TEXT_LIMIT]


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

    def _collect_html_page(self, source: Source, page_body: bytes, page_url: str, content_type: str) -> list[NewsItem]:
        document = _decode_html(page_body, content_type)
        candidates = _select_article_candidates(
            document,
            page_url,
            source.url,
            self.config.max_articles_per_source,
        )
        if not candidates:
            raise CollectionError("No same-site news headlines with article links were found on the source page.")

        items: list[NewsItem] = []
        errors: list[str] = []
        for candidate in candidates:
            try:
                article_body, article_url, article_content_type = self._request(candidate.url)
                if _site_host(article_url) != _site_host(source.url):
                    raise CollectionError("Article redirected away from the configured news site.")
                article_text = _extract_article_text(article_body, article_content_type)
            except CollectionError as exc:
                errors.append(str(exc))
                LOGGER.debug("Could not read selected article from source %s: %s", source.id, exc)
                continue

            reference = SourceReference(source.id, source.name, candidate.title, candidate.url, "Unknown")
            items.append(
                NewsItem(
                    title=candidate.title,
                    url=candidate.url,
                    published_at="Unknown",
                    summary=article_text,
                    sources=[reference],
                )
            )
        if not items:
            reason = errors[0] if errors else "Selected headlines did not contain readable articles."
            raise CollectionError("Could not read selected news articles: " + reason)
        return items

    def _collect_brussels_times(self, source: Source) -> list[NewsItem]:
        try:
            listing_body, _, _ = self._request(BRUSSELS_TIMES_API + "/front")
            listing = json.loads(listing_body)
        except (CollectionError, json.JSONDecodeError) as exc:
            raise CollectionError("Brussels Times public front-page API could not be read.") from exc
        records = listing.get("listed") if isinstance(listing, dict) else None
        if not isinstance(records, list):
            raise CollectionError("Brussels Times public front-page API had no listed articles.")

        unique_records: list[dict[str, object]] = []
        seen_ids: set[int] = set()
        seen_titles: list[str] = []
        scanned = 0
        for record in records:
            if not isinstance(record, dict) or record.get("type") != "article" or record.get("plus") is True:
                continue
            try:
                article_id = int(record.get("id"))
            except (TypeError, ValueError):
                continue
            title = record.get("title")
            slug = record.get("slug")
            if article_id in seen_ids or not isinstance(title, str) or not title.strip() or not isinstance(slug, str) or not slug.strip():
                continue
            scanned += 1
            normalized_title = re.sub(r"[^\w]+", " ", title.casefold()).strip()
            if any(difflib.SequenceMatcher(None, normalized_title, previous).ratio() >= 0.86 for previous in seen_titles):
                LOGGER.debug("Skipping duplicate Brussels Times headline id %d.", article_id)
                seen_ids.add(article_id)
                continue
            seen_ids.add(article_id)
            seen_titles.append(normalized_title)
            unique_records.append(record)

        selected = unique_records[: self.config.max_articles_per_source]
        LOGGER.debug(
            "Brussels Times scanned %d eligible headline(s), found %d unique, selected %d.",
            scanned,
            len(unique_records),
            len(selected),
        )

        if not selected:
            raise CollectionError("Brussels Times public front-page API contained no eligible articles.")

        items: list[NewsItem] = []
        errors: list[str] = []
        for record in selected:
            article_id = int(record["id"])
            try:
                article_body, _, _ = self._request(f"{BRUSSELS_TIMES_API}/article?id={article_id}")
                article = json.loads(article_body)
                if not isinstance(article, dict) or not isinstance(article.get("content"), str):
                    raise CollectionError("Article API response had no readable content field.")
                if article.get("plus") is True or article.get("premium_article") is True:
                    raise CollectionError("Premium article body is not collected.")
                article_text = _extract_article_text(article["content"].encode("utf-8"), "text/html; charset=utf-8")
            except (CollectionError, json.JSONDecodeError) as exc:
                errors.append(str(exc))
                LOGGER.debug("Could not read Brussels Times article id %d: %s", article_id, exc)
                continue

            title = str(article.get("title") or record["title"]).strip()
            slug = str(article.get("slug") or record["slug"]).strip()
            categories = article.get("categories") or record.get("categories") or []
            category = categories[0] if isinstance(categories, list) and categories and isinstance(categories[0], str) else "news"
            article_url = f"https://www.brusselstimes.com/{category}/{article_id}/{slug}"
            published = str(article.get("published") or record.get("published") or "Unknown")
            try:
                published = datetime.strptime(published, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                published = published or "Unknown"
            reference = SourceReference(source.id, source.name, title, article_url, published)
            items.append(NewsItem(title, article_url, published, article_text, [reference]))

        if not items:
            reason = errors[0] if errors else "No selected article could be read."
            raise CollectionError("Could not read Brussels Times articles: " + reason)
        return items

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
            if _site_host(source.url) == "brusselstimes.com":
                return self._collect_brussels_times(source)
            return self._collect_html_page(source, body, final_url, content_type)

        errors: list[str] = []
        for feed_url in feed_urls[:3]:
            try:
                feed_body, feed_final_url, _ = self._request(feed_url)
                items = _parse_feed(feed_body, feed_final_url, source)
                return items[: self.config.max_articles_per_source]
            except CollectionError as exc:
                errors.append(str(exc))
        try:
            if _site_host(source.url) == "brusselstimes.com":
                return self._collect_brussels_times(source)
            return self._collect_html_page(source, body, final_url, content_type)
        except CollectionError as exc:
            errors.append(str(exc))
        raise CollectionError(errors[-1] if errors else "Could not read an advertised RSS or Atom feed or HTML news list.")

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