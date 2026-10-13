import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch
from zoneinfo import ZoneInfo

from teachme.cli import _lesson_filename, _parse_date, execute, parse_args, send_test_email
from teachme.collectors import CollectionError, NewsCollector, deduplicate_items
from teachme.config import AppConfig, ConfigurationError, load_config
from teachme.lesson import LessonError, LessonGenerator, add_lesson_metadata, populate_sources_section, render_prompt, validate_lesson
from teachme.models import NewsItem, Source, SourceReference
from teachme.mailer import MailError, markdown_to_html, send_lesson


def build_config(project_dir: Path, *, sources: tuple[Source, ...] | None = None, input_limit: int = 24000) -> AppConfig:
    prompt_file = project_dir / "prompts" / "news_to_lesson.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    if not prompt_file.exists():
        prompt_file.write_text("{{learning_language}} {{cefr_level}} {{news_items}}", encoding="utf-8")
    return AppConfig(
        project_dir=project_dir,
        llm_base_url="https://llm.example/v1",
        llm_api_key="test-secret-key",
        llm_model="test-model",
        learning_language="English",
        cefr_level="B1",
        max_articles_per_source=3,
        max_total_input_chars=input_limit,
        timezone=ZoneInfo("UTC"),
        timezone_name="UTC",
        http_timeout_seconds=1,
        http_max_retries=0,
        llm_max_tokens=1400,
        output_dir=project_dir / "result",
        prompt_file=prompt_file,
        log_level="ERROR",
        sources=sources or (Source("test", "Test News", "https://example.test/feed"),),
    )


def build_item(title: str, url: str, source_id: str = "test", summary: str = "A short news summary.") -> NewsItem:
    reference = SourceReference(source_id, "Test News", title, url, "2026-10-05T10:00:00+00:00")
    return NewsItem(title, url, reference.published_at, summary, [reference])


def valid_lesson(url: str, count: int = 260) -> str:
    words = " ".join(f"word{index}" for index in range(count))
    return (
        "# Today's News Digest\n\n"
        "## Reading\n" + words + "\n\n"
        "## Vocabulary\n- item\n\n"
        "## Comprehension\n1. Question?\n\n"
        "## Answer Key\n1. Answer.\n\n"
        "## Sources\n- [Test story](" + url + ")\n"
    )


class ConfigurationTests(unittest.TestCase):
    def test_missing_required_keys_are_reported_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ConfigurationError, "LLM_BASE_URL, LLM_API_KEY, LLM_MODEL"):
                    load_config(Path(directory))

    def test_default_sources_and_timezone_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = {
                "LLM_BASE_URL": "https://llm.example/v1",
                "LLM_API_KEY": "secret",
                "LLM_MODEL": "model",
            }
            with patch.dict(os.environ, values, clear=True):
                config = load_config(Path(directory))
        self.assertEqual(len(config.sources), 6)
        self.assertEqual(config.cefr_level, "B1")
        self.assertEqual(config.timezone_name, "UTC")

    def test_learning_language_environment_variable_is_ignored(self) -> None:
        values = {
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "model",
            "LEARNING_LANGUAGE": "French",
            "CEFR_LEVEL": "C2",
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, values, clear=True):
                config = load_config(Path(directory))
        self.assertEqual(config.learning_language, "English")
        self.assertEqual(config.cefr_level, "B1")

    def test_language_option_is_required(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            parse_args(["--language", "English"])
        self.assertEqual(caught.exception.code, 2)
        args = parse_args(["--language", "English", "--level", "B1"])
        self.assertEqual((args.language, args.level), ("English", "B1"))

    def test_language_filename_is_safe_and_keeps_unicode(self) -> None:
        self.assertEqual(_lesson_filename("English"), "English.md")
        self.assertEqual(_lesson_filename("French"), "French.md")
        self.assertEqual(_lesson_filename("한국어"), "한국어.md")
        self.assertEqual(_lesson_filename("../French"), "French.md")

    def test_multiline_quoted_news_sources_json_loads(self) -> None:
        dotenv = """LLM_BASE_URL=https://llm.example/v1
LLM_API_KEY=secret
LLM_MODEL=model
NEWS_SOURCES_JSON='[
  {"id": "bbc", "name": "BBC News", "url": "https://bbc.example/rss"},
  {"id": "ap", "name": "AP News", "url": "https://ap.example/feed.xml"}
]'
"""
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(dotenv, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(Path(directory))
        self.assertEqual([source.id for source in config.sources], ["bbc", "ap"])
        self.assertEqual(config.sources[0].name, "BBC News")

    def test_multiline_unquoted_news_sources_json_loads(self) -> None:
        dotenv = """LLM_BASE_URL=https://llm.example/v1
LLM_API_KEY=secret
LLM_MODEL=model
NEWS_SOURCES_JSON=[
  {"id": "bbc", "name": "BBC News", "url": "https://bbc.example/rss"},
  {"id": "ap", "name": "AP News", "url": "https://ap.example/feed.xml"}
]
"""
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(dotenv, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(Path(directory))
        self.assertEqual([source.id for source in config.sources], ["bbc", "ap"])


class CollectorTests(unittest.TestCase):
    def test_rss_items_parse_and_limit(self) -> None:
        xml = b"""<?xml version='1.0'?><rss><channel>
        <item><title>First story</title><link>https://news.test/1</link><pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate><description><![CDATA[<p>First summary.</p>]]></description></item>
        <item><title>Second story</title><link>https://news.test/2</link></item>
        </channel></rss>"""
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            config = AppConfig(**{**config.__dict__, "max_articles_per_source": 1})
            source = config.sources[0]
            class FakeResponse:
                headers = {"Content-Type": "application/rss+xml"}

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    return False

                def read(self, size=-1):
                    return xml

                def geturl(self):
                    return source.url

            collector = NewsCollector(config, opener=lambda request, timeout: FakeResponse())
            items = collector.collect_source(source)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].title, "First story")
            self.assertEqual(items[0].summary, "First summary.")

    def test_empty_rss_feed_is_not_a_collection_failure(self) -> None:
        xml = b"<?xml version='1.0'?><rss><channel></channel></rss>"
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            source = config.sources[0]

            class FakeResponse:
                headers = {"Content-Type": "text/plain"}

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    return False

                def read(self, size=-1):
                    return xml

                def geturl(self):
                    return source.url

            collector = NewsCollector(config, opener=lambda request, timeout: FakeResponse())
            self.assertEqual(collector.collect_source(source), [])

    def test_html_source_selects_prominent_headlines_and_reads_article_bodies(self) -> None:
        listing_url = "https://news.example/world"
        listing = b"""<!doctype html><html><body>
        <nav><a href="/news/navigation">Navigation link with a headline-looking title</a></nav>
        <main>
          <article class="featured-story"><h2><a href="/news/top-story">Government announces major climate plan for the country</a></h2></article>
          <article class="latest"><h2><a href="/news/second-story">Parliament approves new national transport funding plan</a></h2></article>
          <article><h2><a href="/news/third-story">Local team wins its regional championship final</a></h2></article>
          <a href="https://external.example/news">External report with a sufficiently long headline</a>
        </main>
        </body></html>"""
        article_pages = {
            "https://news.example/news/top-story": (
                "Top report",
                b"<html><body><nav>Navigation must not appear in article text</nav><main><article><h1>Top report</h1>"
                b"<p>Officials announced a major climate plan today. The proposal includes new investment in clean energy, public transport, and building improvements across the country. Ministers said the program will begin next year after a public review. Opposition lawmakers asked for details about the cost, while environmental groups welcomed the targets and requested faster action.</p>"
                b"<script>script text must not appear</script></article></main></body></html>",
            ),
            "https://news.example/news/second-story": (
                "Transport funding",
                b"<html><body><main><article><p>Parliament approved a national transport funding plan after a long debate. The measure provides money for railway repairs, regional bus services, and safer stations. The transport ministry will publish a schedule next month. Several local councils said they need more information before planning projects. The government expects the first work to begin next spring.</p></article></main></body></html>",
            ),
        }
        requested: list[str] = []

        class FakeResponse:
            def __init__(self, url: str, body: bytes, content_type: str):
                self.url = url
                self.body = body
                self.headers = {"Content-Type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                return self.body

            def geturl(self):
                return self.url

        def opener(request, timeout):
            url = request.full_url
            requested.append(url)
            if url == listing_url:
                return FakeResponse(url, listing, "text/html; charset=utf-8")
            _, body = article_pages[url]
            return FakeResponse(url, body, "text/html; charset=utf-8")

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            config = AppConfig(**{**config.__dict__, "max_articles_per_source": 2, "sources": (Source("demo", "Demo News", listing_url),)})
            items = NewsCollector(config, opener=opener).collect_source(config.sources[0])

        self.assertEqual([item.title for item in items], [
            "Government announces major climate plan for the country",
            "Parliament approves new national transport funding plan",
        ])
        self.assertEqual(len(requested), 3)
        self.assertIn("clean energy", items[0].summary)
        self.assertNotIn("Navigation must not appear", items[0].summary)
        self.assertNotIn("script text", items[0].summary)
        self.assertEqual(items[0].sources[0].url, "https://news.example/news/top-story")

    def test_html_listing_without_article_headlines_reports_clear_error(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                return b"<html><body><nav><a href='/x'>Short</a></nav></body></html>"

            def geturl(self):
                return "https://news.example/"

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            source = Source("demo", "Demo News", "https://news.example/")
            collector = NewsCollector(config, opener=lambda request, timeout: FakeResponse())
            with self.assertRaisesRegex(CollectionError, "No same-site news headlines"):
                collector.collect_source(source)

    def test_brussels_times_public_api_collects_front_articles_and_bodies(self) -> None:
        source = Source("brusselstimes", "The Brussels Times", "https://www.brusselstimes.com/")
        listing = {
            "listed": [
                {"id": 101, "type": "article", "title": "Government announces major climate plan for Belgium", "slug": "climate-plan-belgium", "categories": ["belgium"]},
                {"id": 105, "type": "article", "title": "Government announces major climate plan for Belgium today", "slug": "climate-plan-belgium-today", "categories": ["belgium"]},
                {"id": 102, "type": "article", "title": "Police unions call for action after Brussels protest", "slug": "police-unions-brussels-protest", "categories": ["brussels"]},
                {"id": 103, "type": "article", "title": "Premium news", "slug": "premium-news", "plus": True},
                {"id": 104, "type": "article", "title": "Third news", "slug": "third-news", "categories": ["world"]},
            ]
        }
        article_text = (
            "<article><h1>Article heading</h1><p>Officials announced a new measure today after a meeting in Brussels. "
            "The plan includes additional public funding, a review period, and regular reports from the responsible ministry. "
            "Local groups welcomed the announcement and asked officials to publish further details next week.</p></article>"
        )
        requested: list[str] = []

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __init__(self, payload):
                self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                return self.payload

            def geturl(self):
                return requested[-1]

        def opener(request, timeout):
            requested.append(request.full_url)
            if request.full_url == source.url:
                return FakeResponse(b"<html><body><app-root></app-root></body></html>")
            if request.full_url.endswith("/front"):
                return FakeResponse(listing)
            article_id = int(request.full_url.rsplit("=", 1)[1])
            return FakeResponse({
                "id": article_id,
                "title": f"Top news {1 if article_id == 101 else 2}",
                "slug": f"top-news-{1 if article_id == 101 else 2}",
                "published": "2026-10-11 12:30:00",
                "categories": ["belgium" if article_id == 101 else "brussels"],
                "content": article_text,
            })

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            config = AppConfig(**{**config.__dict__, "max_articles_per_source": 2})
            items = NewsCollector(config, opener=opener).collect_source(source)

        self.assertEqual([item.title for item in items], ["Top news 1", "Top news 2"])
        self.assertEqual(len(requested), 4)
        self.assertTrue(requested[1].endswith("/front"))
        self.assertFalse(any("id=105" in url for url in requested))
        self.assertIn("responsible ministry", items[0].summary)
        self.assertEqual(items[0].url, "https://www.brusselstimes.com/belgium/101/top-news-1")
        self.assertEqual(items[0].published_at, "2026-10-11T12:30:00+00:00")

    def test_deduplicate_url_and_near_identical_title_preserving_sources(self) -> None:
        first = build_item("Government announces new climate plan for the country", "https://news.test/story?utm_source=a")
        second = build_item("Government announces new climate plan for the country", "https://mirror.test/story", "mirror")
        unique = deduplicate_items([first, second])
        self.assertEqual(len(unique), 1)
        self.assertEqual(len(unique[0].sources), 2)


class PromptAndValidationTests(unittest.TestCase):
    def test_prompt_serializes_news_and_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            prompt = render_prompt("{{learning_language}} {{cefr_level}} {{news_items}}", config, [build_item("A title", "https://news.test/a")])
        self.assertIn("English B1", prompt)
        self.assertIn("https://news.test/a", prompt)

    def test_date_requires_iso_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            with self.assertRaisesRegex(ConfigurationError, "YYYY-MM-DD"):
                _parse_date("20261005", config)

    def test_prompt_rejects_input_above_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory), input_limit=10)
            with self.assertRaisesRegex(LessonError, "MAX_TOTAL_INPUT_CHARS"):
                render_prompt("{{learning_language}} {{cefr_level}} {{news_items}}", config, [build_item("A title", "https://news.test/a")])

    def test_lesson_must_match_word_limit_and_source_urls(self) -> None:
        item = build_item("A title", "https://news.test/a")
        self.assertEqual(validate_lesson(valid_lesson(item.url), [item]), 260)
        with self.assertRaisesRegex(LessonError, "250-350"):
            validate_lesson(valid_lesson(item.url, 200), [item])
        with self.assertRaisesRegex(LessonError, "not present in the collected news"):
            validate_lesson(valid_lesson("https://other.test/a"), [item])

    def test_application_fills_missing_model_source_links(self) -> None:
        first = build_item("First story", "https://news.test/a")
        second = build_item("Second story", "https://news.test/b")
        markdown = valid_lesson(first.url)
        self.assertEqual(validate_lesson(markdown, [first, second]), 260)
        completed = populate_sources_section(markdown, [first, second])
        self.assertIn("[Test News: First story](https://news.test/a)", completed)
        self.assertIn("[Test News: Second story](https://news.test/b)", completed)

    def test_model_cannot_add_uncollected_source_links(self) -> None:
        first = build_item("First story", "https://news.test/a")
        with self.assertRaisesRegex(LessonError, "not present in the collected news"):
            validate_lesson(valid_lesson("https://other.test/unknown"), [first])

    def test_prompt_truncates_article_text_to_fit_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory), input_limit=800)
            item = build_item("A title", "https://news.test/a", summary="detail " * 500)
            prompt = render_prompt("{{learning_language}} {{cefr_level}} {{news_items}}", config, [item])
        serialized_news = prompt.split("English B1 ", 1)[1]
        self.assertLessEqual(len(serialized_news), 800)
        self.assertIn("https://news.test/a", serialized_news)

    def test_metadata_is_added_to_generated_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            rendered = add_lesson_metadata(valid_lesson("https://news.test/a"), config, datetime(2026, 10, 5, tzinfo=timezone.utc))
        self.assertIn("- Language / Level: English / B1", rendered)
        self.assertIn("- Estimated study time: 3-5 minutes", rendered)

    def test_metadata_records_configured_and_responding_model(self) -> None:
        generated_at = datetime(2026, 10, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            same = add_lesson_metadata(valid_lesson("https://news.test/a"), config, generated_at, "test-model")
            routed = add_lesson_metadata(valid_lesson("https://news.test/a"), config, generated_at, "vendor/actual\n- Injected: yes")
            unknown = add_lesson_metadata(valid_lesson("https://news.test/a"), config, generated_at, None)
        self.assertIn("- Model: test-model\n", same)
        self.assertIn("- Model: test-model\n", unknown)
        self.assertIn("- Model: vendor/actual - Injected: yes (requested: test-model)\n", routed)
        self.assertNotIn("\n- Injected", routed)


class ExecutionTests(unittest.TestCase):
    def test_generates_one_daily_file_and_summary_for_batch(self) -> None:
        class FakeCollector:
            def collect_all(self):
                return [build_item("A story", "https://news.test/a"), build_item("Another story", "https://news.test/b")], {
                    "test": {"name": "Test News", "status": "success", "discovered": 2, "error_code": None, "error": None}
                }

        class FakeGenerator:
            def generate(self, items):
                self.items = items
                return valid_lesson("https://news.test/a"), 260

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            args = type("Args", (), {"language": "English", "level": "B1", "debug": False, "run_date": "2026-10-05", "force": False})()
            exit_code = execute(config, args, collector=FakeCollector(), generator=FakeGenerator())
            result_dir = config.output_dir / "2026-10-05"
            summary = json.loads((result_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertTrue((result_dir / "English.md").is_file())
            self.assertEqual(len(list(result_dir.glob("*.md"))), 1)
            self.assertEqual(summary["counts"]["generated"], 1)
            self.assertEqual(summary["counts"]["discovered"], 2)
            self.assertIn("prompt_version", summary)
            lesson = (result_dir / "English.md").read_text(encoding="utf-8")
            self.assertIn("- Generated:", lesson)
            self.assertIn("- Language / Level: English / B1", lesson)

    def test_partial_source_failure_returns_one(self) -> None:
        class FakeCollector:
            def collect_all(self):
                return [build_item("A story", "https://news.test/a")], {
                    "ok": {"name": "Okay", "status": "success", "discovered": 1, "error_code": None, "error": None},
                    "bad": {"name": "Broken", "status": "failed", "discovered": 0, "error_code": "collection_error", "error": "unavailable"},
                }

        class FakeGenerator:
            def generate(self, items):
                return valid_lesson("https://news.test/a"), 260

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            args = type("Args", (), {"language": "English", "level": "B1", "debug": False, "run_date": "2026-10-05", "force": False})()
            self.assertEqual(execute(config, args, collector=FakeCollector(), generator=FakeGenerator()), 1)

    def test_no_news_creates_summary_without_lesson(self) -> None:
        class FakeCollector:
            def collect_all(self):
                return [], {"test": {"name": "Test News", "status": "empty", "discovered": 0, "error_code": None, "error": None}}

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            args = type("Args", (), {"language": "English", "level": "B1", "debug": False, "run_date": "2026-10-05", "force": False})()
            result = execute(config, args, collector=FakeCollector())
            result_dir = config.output_dir / "2026-10-05"
            summary = json.loads((result_dir / "run-summary.json").read_text(encoding="utf-8"))
            lesson_exists = (result_dir / "English.md").exists()
        self.assertEqual(result, 0)
        self.assertFalse(lesson_exists)
        self.assertEqual(summary["status"], "no_news")

    def test_different_languages_coexist_in_same_date_folder(self) -> None:
        class FakeCollector:
            def collect_all(self):
                return [build_item("A story", "https://news.test/a")], {
                    "test": {"name": "Test News", "status": "success", "discovered": 1, "error_code": None, "error": None}
                }

        class FakeGenerator:
            def generate(self, items):
                return valid_lesson("https://news.test/a"), 260

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            result_dir = config.output_dir / "2026-10-05"
            for language, level in (("English", "B1"), ("French", "A2")):
                args = type("Args", (), {"language": language, "level": level, "debug": False, "run_date": "2026-10-05", "force": False})()
                result = execute(config, args, collector=FakeCollector(), generator=FakeGenerator())
                self.assertEqual(result, 0)
            self.assertTrue((result_dir / "English.md").is_file())
            french_lesson = (result_dir / "French.md").read_text(encoding="utf-8")
            self.assertIn("- Language / Level: French / A2", french_lesson)

    def test_existing_lesson_is_skipped_without_collecting(self) -> None:
        class NeverCollector:
            def collect_all(self):
                raise AssertionError("existing daily result must be skipped")

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            result_dir = config.output_dir / "2026-10-05"
            result_dir.mkdir(parents=True)
            (result_dir / "English.md").write_text("existing", encoding="utf-8")
            args = type("Args", (), {"language": "English", "level": "B1", "debug": False, "run_date": "2026-10-05", "force": False})()
            result = execute(config, args, collector=NeverCollector())
            summary = json.loads((result_dir / "run-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertEqual(summary["status"], "skipped_existing")
        self.assertEqual(summary["counts"]["skipped"], 1)

    def test_force_failure_preserves_existing_lesson(self) -> None:
        class FakeCollector:
            def collect_all(self):
                return [build_item("A story", "https://news.test/a")], {
                    "test": {"name": "Test News", "status": "success", "discovered": 1, "error_code": None, "error": None}
                }

        class FailingGenerator:
            def generate(self, items):
                raise LessonError("invalid response")

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            result_dir = config.output_dir / "2026-10-05"
            result_dir.mkdir(parents=True)
            lesson_path = result_dir / "English.md"
            lesson_path.write_text("previous lesson", encoding="utf-8")
            args = type("Args", (), {"language": "English", "level": "B1", "debug": False, "run_date": "2026-10-05", "force": True})()
            result = execute(config, args, collector=FakeCollector(), generator=FailingGenerator())
            preserved_lesson = lesson_path.read_text(encoding="utf-8")
        self.assertEqual(result, 1)
        self.assertEqual(preserved_lesson, "previous lesson")


class LLMRequestTests(unittest.TestCase):
    def test_chat_completions_request_retries_rate_limit(self) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                return self.payload

        calls = []

        def fake_opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                headers = Message()
                headers["Retry-After"] = "0"
                raise HTTPError(request.full_url, 429, "rate limit", headers, None)
            response = {"choices": [{"message": {"content": "lesson"}}]}
            return FakeResponse(json.dumps(response).encode("utf-8"))

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            config = AppConfig(**{**config.__dict__, "http_max_retries": 1})
            generator = LessonGenerator(config, opener=fake_opener, sleeper=lambda _: None)
            result = generator._request("prompt")
        self.assertEqual(result, "lesson")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].full_url, "https://llm.example/v1/chat/completions")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer test-secret-key")
        self.assertEqual(calls[0].get_header("User-agent"), "TeachMeNewsDigest/0.1.0")

    def test_output_token_quota_429_is_not_retried(self) -> None:
        calls = []

        def oversized_opener(request, timeout):
            calls.append(request)
            headers = Message()
            headers["Content-Type"] = "application/json"
            body = json.dumps({
                "error": {
                    "message": "Request too large on output tokens per minute (OTPM): Limit 1000, Requested 1008; reduce max_tokens.",
                    "code": "rate_limit_exceeded",
                }
            }).encode("utf-8")
            raise HTTPError(request.full_url, 429, "Too Many Requests", headers, BytesIO(body))

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            config = AppConfig(**{**config.__dict__, "http_max_retries": 3, "llm_max_tokens": 900})
            generator = LessonGenerator(config, opener=oversized_opener, sleeper=lambda _: None)
            with self.assertRaisesRegex(LessonError, "output token limit exceeded"):
                generator._request("prompt")
        self.assertEqual(len(calls), 1)
        sent_payload = json.loads(calls[0].data)
        self.assertEqual(sent_payload["max_tokens"], 900)

    def test_empty_content_error_reports_finish_reason_and_usage(self) -> None:
        calls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                payload = {
                    "model": "vendor/reasoning-model",
                    "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                    "usage": {"completion_tokens": 900},
                }
                return json.dumps(payload).encode("utf-8")

        def opener(request, timeout):
            calls.append(request)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            generator = LessonGenerator(config, opener=opener, sleeper=lambda _: None)
            with self.assertRaises(LessonError) as caught:
                generator.generate([build_item("A story", "https://news.test/a")])
        message = str(caught.exception)
        self.assertIn("finish_reason=length", message)
        self.assertIn("model=vendor/reasoning-model", message)
        self.assertIn("completion_tokens=900", message)
        self.assertIn("Increase LLM_MAX_TOKENS", message)
        self.assertEqual(len(calls), 1)

    def test_short_reading_is_retried_with_target_length_hint(self) -> None:
        class FakeResponse:
            def __init__(self, content: str):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                return json.dumps({"choices": [{"message": {"content": self.content}}]}).encode("utf-8")

        calls = []
        item = build_item("A story", "https://news.test/a")
        drafts = [valid_lesson(item.url, 248), valid_lesson(item.url, 300)]

        def fake_opener(request, timeout):
            calls.append(json.loads(request.data)["messages"][0]["content"])
            return FakeResponse(drafts[len(calls) - 1])

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            generator = LessonGenerator(config, opener=fake_opener, sleeper=lambda _: None)
            _, word_count = generator.generate([item])
        self.assertEqual(word_count, 300)
        self.assertEqual(len(calls), 2)
        self.assertIn("received 248. Aim for about 300 words.", calls[1])
        self.assertIn("<previous_draft>", calls[1])
        self.assertIn("word247", calls[1])
        self.assertEqual(calls[1].count("Your previous draft did not meet"), 1)

    def test_malformed_model_response_is_retried_once(self) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                return self.payload

        calls = []
        item = build_item("A story", "https://news.test/a")

        def fake_opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                payload = {"unexpected": "shape"}
            else:
                payload = {"choices": [{"message": {"content": valid_lesson(item.url)}}]}
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            generator = LessonGenerator(config, opener=fake_opener, sleeper=lambda _: None)
            markdown, word_count = generator.generate([item])
        self.assertEqual(len(calls), 2)
        self.assertEqual(word_count, 260)
        self.assertIn("## Reading", markdown)

    def test_result_file_records_model_reported_by_provider(self) -> None:
        item = build_item("A story", "https://news.test/a")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                payload = {
                    "model": "z-ai/glm-5.3-flash",
                    "choices": [{"message": {"content": valid_lesson(item.url)}}],
                }
                return json.dumps(payload).encode("utf-8")

        class FakeCollector:
            def collect_all(self):
                return [item], {
                    "test": {"name": "Test News", "status": "success", "discovered": 1, "error_code": None, "error": None}
                }

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            generator = LessonGenerator(config, opener=lambda request, timeout: FakeResponse(), sleeper=lambda _: None)
            args = type("Args", (), {"language": "English", "level": "B1", "debug": False, "run_date": "2026-10-05", "force": False})()
            result = execute(config, args, collector=FakeCollector(), generator=generator)
            lesson = (config.output_dir / "2026-10-05" / "English.md").read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertIn("- Model: z-ai/glm-5.3-flash (requested: test-model)\n", lesson)

    def test_generator_populates_sources_omitted_by_model(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                first_url = "https://news.test/a"
                payload = {"choices": [{"message": {"content": valid_lesson(first_url)}}]}
                return json.dumps(payload).encode("utf-8")

        first = build_item("First story", "https://news.test/a")
        second = build_item("Second story", "https://news.test/b")
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            generator = LessonGenerator(config, opener=lambda request, timeout: FakeResponse(), sleeper=lambda _: None)
            markdown, _ = generator.generate([first, second])
        self.assertIn("[Test News: First story](https://news.test/a)", markdown)
        self.assertIn("[Test News: Second story](https://news.test/b)", markdown)

    def test_forbidden_response_explains_provider_error_without_leaking_key(self) -> None:
        def forbidden_opener(request, timeout):
            body = json.dumps({
                "error": {
                    "message": "Model access denied for test-secret-key",
                    "type": "permission_error",
                    "code": "model_not_available",
                }
            }).encode("utf-8")
            raise HTTPError(request.full_url, 403, "Forbidden", Message(), BytesIO(body))

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            generator = LessonGenerator(config, opener=forbidden_opener, sleeper=lambda _: None)
            with self.assertRaises(LessonError) as caught:
                generator._request("prompt")
        self.assertIn("access forbidden (HTTP 403)", str(caught.exception))
        self.assertIn("model_not_available", str(caught.exception))
        self.assertNotIn("test-secret-key", str(caught.exception))

    def test_forbidden_non_json_response_includes_safe_request_metadata(self) -> None:
        def forbidden_opener(request, timeout):
            headers = Message()
            headers["Content-Type"] = "text/html; charset=utf-8"
            headers["x-groq-request-id"] = "req_test-123"
            raise HTTPError(request.full_url, 403, "Forbidden", headers, BytesIO(b"Forbidden"))

        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
            generator = LessonGenerator(config, opener=forbidden_opener, sleeper=lambda _: None)
            with self.assertRaises(LessonError) as caught:
                generator._request("prompt")
        self.assertIn("access forbidden (HTTP 403)", str(caught.exception))
        self.assertIn("content-type=text/html", str(caught.exception))
        self.assertIn("request-id=req_test-123", str(caught.exception))
        self.assertNotIn("test-secret-key", str(caught.exception))


class MailSettingsTests(unittest.TestCase):
    BASE = {"LLM_BASE_URL": "https://llm.example/v1", "LLM_API_KEY": "secret", "LLM_MODEL": "model"}

    def load(self, **extra: str):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {**self.BASE, **extra}, clear=True):
                return load_config(Path(directory))

    def test_mail_is_disabled_without_recipients(self) -> None:
        self.assertEqual(self.load().mail_to, ())

    def test_recipients_and_sender_defaults_load(self) -> None:
        config = self.load(NOTIFY_EMAIL="a@example.com, b@example.com;a@example.com", RESEND_API_KEY="re_test")
        self.assertEqual(config.mail_to, ("a@example.com", "b@example.com"))
        self.assertEqual((config.mail_from, config.resend_api_key), ("Teach Me <onboarding@resend.dev>", "re_test"))
        self.assertEqual(self.load(NOTIFY_EMAIL="a@example.com", RESEND_API_KEY="re_test", MAIL_FROM="bot@mydomain.com").mail_from, "bot@mydomain.com")
        self.assertEqual(
            self.load(NOTIFY_EMAIL="a@example.com", RESEND_API_KEY="re_test", MAIL_FROM="Teach Me <onboarding@resend.dev>").mail_from,
            "Teach Me <onboarding@resend.dev>",
        )

    def test_invalid_mail_settings_are_rejected_before_any_network_call(self) -> None:
        good = {"NOTIFY_EMAIL": "a@example.com", "RESEND_API_KEY": "re_test"}
        cases = [
            ({**good, "NOTIFY_EMAIL": "not-an-address"}, "NOTIFY_EMAIL"),
            ({**good, "NOTIFY_EMAIL": ",".join(f"u{i}@example.com" for i in range(51))}, "at most 50"),
            ({**good, "RESEND_API_KEY": ""}, "RESEND_API_KEY"),
            ({**good, "RESEND_API_KEY": "re bad key"}, "RESEND_API_KEY"),
            ({**good, "MAIL_FROM": "Bot <not-an-address>"}, "MAIL_FROM"),
            ({**good, "MAIL_FROM": "Teach Me <bot@example.com>\nBcc:evil@example.com"}, "control characters"),
        ]
        for values, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ConfigurationError, expected):
                    self.load(**values)


class MarkdownEmailTests(unittest.TestCase):
    LESSON = (
        "# Today's Digest\n\n"
        "- Generated: 2026-10-07\n- Model: vendor/model\n\n"
        "## Reading\nFirst line **bold** and *soft* with `code`.\nSecond line.\n\n"
        "## Comprehension\n1. First question?\n\n2. Second question?\n\n3. Third question?\n\n"
        "## Sources\n- [BBC: A story](https://news.test/a?x=1&y=2)\n"
    )

    def test_renders_headings_lists_and_emphasis(self) -> None:
        html_body = markdown_to_html(self.LESSON)
        self.assertIn("Today&#x27;s Digest</h1>", html_body)
        self.assertEqual(html_body.count("<h2"), 3)
        self.assertIn("<strong>bold</strong>", html_body)
        self.assertIn("<em>soft</em>", html_body)
        self.assertIn(">code</code>", html_body)
        self.assertIn("Second line.", html_body)
        self.assertIn("<br>", html_body)
        self.assertIn("<li", html_body)
        self.assertNotIn("**", html_body)

    def test_numbered_list_stays_one_list_across_blank_lines(self) -> None:
        html_body = markdown_to_html(self.LESSON)
        self.assertEqual(html_body.count("<ol"), 1)
        self.assertEqual(html_body.count("</li>"), 3 + 2 + 1)

    def test_numbered_list_keeps_its_start_number(self) -> None:
        self.assertIn('<ol start="3"', markdown_to_html("3. Third\n4. Fourth\n"))

    def test_links_are_clickable_and_urls_escaped(self) -> None:
        html_body = markdown_to_html(self.LESSON)
        self.assertIn('<a href="https://news.test/a?x=1&amp;y=2"', html_body)
        self.assertIn(">BBC: A story</a>", html_body)

    def test_escaped_brackets_in_link_labels_are_unescaped(self) -> None:
        html_body = markdown_to_html(r"- [BBC: Report \[live\]](https://news.test/a)")
        self.assertIn(">BBC: Report [live]</a>", html_body)

    def test_untrusted_markup_and_unsafe_links_are_not_rendered(self) -> None:
        html_body = markdown_to_html(
            '<script>alert(1)</script> <img src=x onerror=alert(2)>\n'
            "[click](javascript:alert(3)) [ok](https://news.test/\"onmouseover=\"x)\n"
        )
        self.assertNotIn("<script", html_body)
        self.assertNotIn("<img", html_body)
        self.assertNotIn('href="javascript', html_body)
        self.assertNotIn('"onmouseover="', html_body)
        self.assertIn("&lt;script&gt;", html_body)

    def test_thematic_break_and_empty_input(self) -> None:
        self.assertIn("<hr", markdown_to_html("above\n\n---\n\nbelow"))
        self.assertEqual(markdown_to_html(""), f'<div style="{__import__("teachme.mailer", fromlist=["x"]).WRAPPER_STYLE}"></div>')


class FakeResendResponse:
    def __init__(self, body: bytes = b'{"id": "email_123"}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        return self.body


class MailSendingTests(unittest.TestCase):
    def mail_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(Path(directory))
        return replace(config, mail_to=("reader@example.com", "other@example.com"), mail_from="bot@mydomain.com", resend_api_key="re_secret")

    def send(self, opener, *, language="French", markdown="# Digest\n\nBody\n"):
        return send_lesson(
            self.mail_config(), markdown, language=language, run_date="2026-10-07", opener=opener,
        )

    def test_posts_lesson_to_resend_as_html_body_without_attachment(self) -> None:
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return FakeResendResponse()

        count = self.send(opener)
        request, timeout = calls[0]
        payload = json.loads(request.data)
        self.assertEqual(count, 2)
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer re_secret")
        self.assertEqual(request.get_header("User-agent"), "TeachMeNewsDigest/0.1.0")
        self.assertEqual(timeout, 1)
        self.assertEqual(payload["from"], "bot@mydomain.com")
        self.assertEqual(payload["to"], ["reader@example.com", "other@example.com"])
        self.assertEqual(payload["subject"], "Teach Me: French reading lesson (2026-10-07)")
        self.assertEqual(payload["text"], "# Digest\n\nBody\n")
        self.assertIn("<h1", payload["html"])
        self.assertIn("Digest</h1>", payload["html"])
        self.assertIn("Body</p>", payload["html"])
        self.assertNotIn("attachments", payload)

    def test_language_cannot_add_lines_to_subject(self) -> None:
        calls = []
        self.send(lambda request, timeout: calls.append(request) or FakeResendResponse(), language="English\nBcc: evil@example.com")
        subject = json.loads(calls[0].data)["subject"]
        self.assertNotIn("\n", subject)

    def test_http_error_reports_provider_message_without_leaking_key(self) -> None:
        def opener(request, timeout):
            body = json.dumps({"statusCode": 403, "name": "validation_error", "message": "Domain not verified for re_secret"})
            raise HTTPError(request.full_url, 403, "Forbidden", Message(), BytesIO(body.encode("utf-8")))

        with self.assertRaises(MailError) as caught:
            self.send(opener)
        self.assertIn("HTTP 403: Domain not verified", str(caught.exception))
        self.assertNotIn("re_secret", str(caught.exception))

    def test_http_error_without_json_body_still_reports_status(self) -> None:
        def opener(request, timeout):
            raise HTTPError(request.full_url, 502, "Bad Gateway", Message(), BytesIO(b"<html>oops</html>"))

        with self.assertRaisesRegex(MailError, r"\(HTTP 502\)"):
            self.send(opener)

    def test_connection_errors_become_mail_errors(self) -> None:
        def opener(request, timeout):
            raise URLError(ConnectionRefusedError("refused"))

        with self.assertRaisesRegex(MailError, "URLError"):
            self.send(opener)

    def test_response_without_email_id_is_a_failure(self) -> None:
        with self.assertRaisesRegex(MailError, "no email id"):
            self.send(lambda request, timeout: FakeResendResponse(b"{}"))
        with self.assertRaisesRegex(MailError, "no email id"):
            self.send(lambda request, timeout: FakeResendResponse(b"not json"))


class TestEmailOptionTests(unittest.TestCase):
    def mail_config(self, directory: str, **overrides):
        values = {"mail_to": ("reader@example.com",), "mail_from": "bot@mydomain.com", "resend_api_key": "re_test"}
        return replace(build_config(Path(directory)), **{**values, **overrides})

    def args(self):
        return type("Args", (), {"debug": False})()

    def test_language_is_optional_only_with_test_email(self) -> None:
        self.assertTrue(parse_args(["--test-email"]).test_email)
        self.assertFalse(parse_args(["--language", "English", "--level", "B1"]).test_email)
        with self.assertRaises(SystemExit):
            parse_args(["--debug"])

    def test_sends_sample_email_without_writing_results(self) -> None:
        sent = []

        def mailer(config, markdown, **kwargs):
            sent.append((markdown, kwargs))
            return 1

        with tempfile.TemporaryDirectory() as directory:
            config = self.mail_config(directory)
            code = send_test_email(config, self.args(), mailer=mailer)
            wrote_results = config.output_dir.exists()
        self.assertEqual(code, 0)
        self.assertFalse(wrote_results)
        self.assertIn("test email", sent[0][0])
        self.assertEqual(sent[0][1]["language"], "Test")

    def test_missing_mail_settings_exit_with_configuration_error(self) -> None:
        def mailer(*args, **kwargs):
            raise AssertionError("must not send without recipients")

        with tempfile.TemporaryDirectory() as directory:
            config = self.mail_config(directory, mail_to=(), resend_api_key=None)
            self.assertEqual(send_test_email(config, self.args(), mailer=mailer), 2)

    def test_delivery_failure_exits_with_one(self) -> None:
        def mailer(*args, **kwargs):
            raise MailError("Email delivery failed (HTTP 401: API key is invalid).")

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(send_test_email(self.mail_config(directory), self.args(), mailer=mailer), 1)


class MailExecutionTests(unittest.TestCase):
    class Collector:
        def collect_all(self):
            return [build_item("A story", "https://news.test/a")], {
                "test": {"name": "Test News", "status": "success", "discovered": 1, "error_code": None, "error": None}
            }

    class Generator:
        def generate(self, items):
            return valid_lesson("https://news.test/a"), 260

    def run_cli(self, directory: str, mailer, *, existing: bool = False):
        config = replace(build_config(Path(directory)), mail_to=("reader@example.com",), mail_from="bot@example.com", resend_api_key="re_test")
        result_dir = config.output_dir / "2026-10-07"
        if existing:
            result_dir.mkdir(parents=True)
            (result_dir / "English.md").write_text("old", encoding="utf-8")
        args = type("Args", (), {"language": "English", "level": "B1", "debug": False, "run_date": "2026-10-07", "force": False})()
        code = execute(config, args, collector=self.Collector(), generator=self.Generator(), mailer=mailer)
        summary = json.loads((result_dir / "run-summary.json").read_text(encoding="utf-8"))
        return code, summary, result_dir / "English.md"

    def test_lesson_is_emailed_after_generation(self) -> None:
        sent = []

        def mailer(config, markdown, **kwargs):
            sent.append((markdown, kwargs))
            return 1

        with tempfile.TemporaryDirectory() as directory:
            code, summary, lesson_path = self.run_cli(directory, mailer)
            saved = lesson_path.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(sent[0][0], saved)
        self.assertEqual(sent[0][1]["language"], "English")
        self.assertEqual(summary["email"], {"status": "sent", "recipients": 1})

    def test_mail_failure_keeps_lesson_and_exits_nonzero(self) -> None:
        def mailer(config, markdown, **kwargs):
            raise MailError("Email delivery failed (HTTP 403: Domain not verified).")

        with tempfile.TemporaryDirectory() as directory:
            code, summary, lesson_path = self.run_cli(directory, mailer)
            lesson_exists = lesson_path.is_file()
        self.assertEqual(code, 1)
        self.assertTrue(lesson_exists)
        self.assertEqual(summary["email"], {"status": "failed"})
        self.assertEqual(summary["errors"][0]["code"], "email_error")
        self.assertEqual(summary["status"], "partial_failure")

    def test_skipped_existing_lesson_is_not_emailed(self) -> None:
        def mailer(*args, **kwargs):
            raise AssertionError("existing lesson must not be re-sent")

        with tempfile.TemporaryDirectory() as directory:
            code, summary, _ = self.run_cli(directory, mailer, existing=True)
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "skipped_existing")


if __name__ == "__main__":
    unittest.main()