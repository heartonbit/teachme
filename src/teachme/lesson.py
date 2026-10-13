import json
import logging
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from teachme.collectors import normalize_url
from teachme.config import AppConfig
from teachme.models import NewsItem


REQUIRED_HEADINGS = ("## Reading", "## Vocabulary", "## Comprehension", "## Answer Key", "## Sources")
WORD_PATTERN = re.compile(r"[\w]+(?:['’][\w]+)*", re.UNICODE)
SOURCE_LINK_PATTERN = re.compile(r"\[[^\]]+\]\((https?://[^)]+)\)")
MAX_GENERATION_ATTEMPTS = 3
LOGGER = logging.getLogger(__name__)


class LessonError(Exception):
    pass


class InvalidModelResponse(LessonError):
    pass


def _is_output_token_limit_error(detail: str | None) -> bool:
    if not detail:
        return False
    message = detail.casefold()
    return (
        "output tokens per minute" in message
        or "otpm" in message
        or "request too large" in message
        or ("reduce max_tokens" in message and "limit" in message)
        or ("requested" in message and "limit" in message and "token" in message)
    )


def render_prompt(template: str, config: AppConfig, news_items: list[NewsItem]) -> str:
    payload_items = [item.as_prompt_dict() for item in news_items]
    serialized_news = json.dumps(payload_items, ensure_ascii=False, indent=2)
    while len(serialized_news) > config.max_total_input_chars:
        if not any(item["summary"] for item in payload_items):
            raise LessonError("News metadata exceeds MAX_TOTAL_INPUT_CHARS.")
        for item in payload_items:
            summary = item["summary"]
            if summary:
                item["summary"] = summary[: max(0, len(summary) * 3 // 4)]
        serialized_news = json.dumps(payload_items, ensure_ascii=False, indent=2)
    values = {
        "learning_language": config.learning_language,
        "cefr_level": config.cefr_level,
        "news_items": serialized_news,
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    if re.search(r"{{\s*[A-Za-z_][A-Za-z0-9_]*\s*}}", template):
        raise LessonError("Prompt contains an unresolved template variable.")
    return template


def extract_reading(markdown: str) -> str:
    match = re.search(r"(?ms)^## Reading\s*\n(.*?)(?=^##\s|\Z)", markdown)
    if not match:
        raise LessonError("Generated lesson is missing the ## Reading section.")
    return match.group(1).strip()


def validate_lesson(markdown: str, news_items: list[NewsItem]) -> int:
    if not markdown.strip().startswith("# "):
        raise LessonError("Generated lesson must start with a Markdown title.")
    for heading in REQUIRED_HEADINGS:
        if not re.search(r"(?m)^" + re.escape(heading) + r"\s*$", markdown):
            raise LessonError(f"Generated lesson is missing the {heading} section.")

    reading = extract_reading(markdown)
    word_count = len(WORD_PATTERN.findall(reading))
    if not 250 <= word_count <= 350:
        raise LessonError(f"Reading section must contain 250-350 words; received {word_count}. Aim for about 300 words.")

    supplied_urls = {
        normalize_url(reference.url)
        for item in news_items
        for reference in item.sources
    }
    linked_urls = {normalize_url(url) for url in SOURCE_LINK_PATTERN.findall(markdown)}
    if not linked_urls.issubset(supplied_urls):
        raise LessonError("Sources contain a URL that is not present in the collected news.")
    return word_count


def populate_sources_section(markdown: str, news_items: list[NewsItem]) -> str:
    match = re.search(r"(?m)^## Sources\s*$", markdown)
    if not match:
        raise LessonError("Generated lesson is missing the ## Sources section.")

    references = []
    seen_urls: set[str] = set()
    for item in news_items:
        for reference in item.sources:
            normalized_url = normalize_url(reference.url)
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            label = f"{reference.source_name}: {reference.title}"
            label = re.sub(r"\s+", " ", label).strip().replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            safe_url = quote(reference.url, safe=":/?#@!$&'*+,;=%-._~")
            references.append(f"- [{label}]({safe_url})")
    if not references:
        for item in news_items:
            normalized_url = normalize_url(item.url)
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            label = re.sub(r"\s+", " ", item.title).strip().replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            safe_url = quote(item.url, safe=":/?#@!$&'*+,;=%-._~")
            references.append(f"- [{label}]({safe_url})")
    if not references:
        raise LessonError("Cannot render a Sources section without collected article URLs.")

    return markdown[:match.start()].rstrip() + "\n\n## Sources\n" + "\n".join(references) + "\n"


def _model_label(requested: str, responded: str | None) -> str:
    # 응답 값은 외부 입력이므로 제어 문자를 제거하고 길이를 제한한다.
    actual = re.sub(r"[\x00-\x1f\x7f]+", " ", responded or "").strip()[:120]
    if not actual or actual == requested:
        return requested
    return f"{actual} (requested: {requested})"


def add_lesson_metadata(markdown: str, config: AppConfig, generated_at: datetime, model: str | None = None) -> str:
    title, separator, body = markdown.partition("\n")
    if not title.startswith("# ") or not separator:
        raise LessonError("Generated lesson must start with a Markdown title.")
    metadata = (
        f"- Generated: {generated_at.isoformat(timespec='seconds')}\n"
        f"- Language / Level: {config.learning_language} / {config.cefr_level}\n"
        f"- Model: {_model_label(config.llm_model, model)}\n"
        "- Estimated study time: 3-5 minutes"
    )
    body = body.lstrip("\n")
    return title + "\n\n" + metadata + ("\n\n" + body if body else "")


class LessonGenerator:
    def __init__(self, config: AppConfig, *, opener=urlopen, sleeper=time.sleep):
        self.config = config
        self.opener = opener
        self.sleeper = sleeper
        self.last_model: str | None = None

    def _endpoint(self) -> str:
        base = self.config.llm_base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if urlsplit(base).path.rstrip("/").endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def _provider_error_detail(self, error: HTTPError) -> str | None:
        provider_detail: str | None = None
        try:
            raw_body = error.read(8_193)
        except OSError:
            raw_body = b""
        if len(raw_body) > 8_192:
            raw_body = raw_body[:8_192]
        if raw_body:
            try:
                payload = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            provider_error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(provider_error, str):
                provider_detail = provider_error
            elif isinstance(provider_error, dict):
                message = provider_error.get("message")
                code = provider_error.get("code")
                parts = [str(value) for value in (code, message) if isinstance(value, str) and value.strip()]
                provider_detail = ": ".join(parts) or None

        if provider_detail:
            provider_detail = provider_detail.replace(self.config.llm_api_key, "[REDACTED]")
            provider_detail = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", provider_detail)
            provider_detail = re.sub(
                r"(?i)(api[_-]?key|token|secret|password)(\s*[:=]\s*)[^\s,;]+",
                r"\1\2[REDACTED]",
                provider_detail,
            )
            provider_detail = re.sub(r"[\x00-\x1f\x7f]+", " ", provider_detail).strip()[:400]

        metadata: list[str] = []
        content_type = error.headers.get("Content-Type") if error.headers else None
        if content_type:
            metadata.append("content-type=" + content_type.split(";", 1)[0][:80])
        if error.headers:
            for header in ("x-groq-request-id", "x-request-id", "request-id"):
                request_id = error.headers.get(header)
                if request_id and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
                    metadata.append("request-id=" + request_id)
                    break
        if provider_detail:
            metadata.insert(0, provider_detail)
        elif not metadata:
            metadata.append("provider supplied no structured error details")
        return "; ".join(metadata)[:500]

    def _request(self, prompt: str) -> str:
        payload: dict[str, object] = {
            "model": self.config.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }
        if self.config.llm_max_tokens is not None:
            payload["max_tokens"] = self.config.llm_max_tokens
        request = Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.config.llm_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "TeachMeNewsDigest/0.1.0",
            },
            method="POST",
        )
        endpoint = urlsplit(self._endpoint())
        LOGGER.debug("Sending Chat Completions request to %s%s with model %s.", endpoint.hostname, endpoint.path, self.config.llm_model)
        raw_response = b""
        for attempt in range(self.config.http_max_retries + 1):
            try:
                with self.opener(request, timeout=self.config.http_timeout_seconds) as response:
                    raw_response = response.read(2_000_001)
                break
            except HTTPError as exc:
                detail = self._provider_error_detail(exc)
                permanent_token_limit = exc.code == 429 and _is_output_token_limit_error(detail)
                retryable = (exc.code == 429 and not permanent_token_limit) or 500 <= exc.code <= 599
                if not retryable or attempt >= self.config.http_max_retries:
                    suffix = f": {detail}" if detail else ""
                    if exc.code in {401, 403}:
                        reason = "authentication failed" if exc.code == 401 else "access forbidden"
                        raise LessonError(f"LLM {reason} (HTTP {exc.code}){suffix}") from exc
                    if permanent_token_limit:
                        raise LessonError(f"LLM output token limit exceeded (HTTP {exc.code}){suffix}") from exc
                    raise LessonError(f"LLM request failed (HTTP {exc.code}){suffix}") from exc
                LOGGER.debug("Retrying LLM request after HTTP %d (retry %d/%d).", exc.code, attempt + 1, self.config.http_max_retries)
                self.sleeper(self._retry_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.http_max_retries:
                    raise LessonError(f"LLM network request failed ({type(exc).__name__}).") from exc
                LOGGER.debug("Retrying LLM request after %s (retry %d/%d).", type(exc).__name__, attempt + 1, self.config.http_max_retries)
                self.sleeper(min(30.0, 2.0 ** attempt))
        if len(raw_response) > 2_000_000:
            raise LessonError("LLM response exceeded the 2 MB size limit.")
        try:
            response_json = json.loads(raw_response)
            content = response_json["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise InvalidModelResponse("LLM response did not match the Chat Completions response format.") from exc
        responded_model = response_json.get("model")
        self.last_model = responded_model if isinstance(responded_model, str) else None
        if not isinstance(content, str) or not content.strip():
            choice = response_json["choices"][0]
            usage = response_json.get("usage") if isinstance(response_json.get("usage"), dict) else {}
            details = [
                f"finish_reason={choice.get('finish_reason')}",
                f"model={response_json.get('model')}",
                f"completion_tokens={usage.get('completion_tokens')}",
                f"max_tokens={self.config.llm_max_tokens}",
            ]
            message = "LLM returned an empty lesson (" + ", ".join(details) + ")."
            if choice.get("finish_reason") == "length":
                # 같은 요청을 재시도해도 같은 한도에 다시 걸린다.
                raise LessonError(message + " Increase LLM_MAX_TOKENS or use a non-reasoning model.")
            raise InvalidModelResponse(message)
        return content.strip()

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    return min(30.0, max(0.0, (retry_at - datetime.now(retry_at.tzinfo)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(30.0, 2.0 ** attempt)

    def generate(self, news_items: list[NewsItem]) -> tuple[str, int]:
        try:
            template = self.config.prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise LessonError("Prompt file could not be read.") from exc
        prompt = render_prompt(template, self.config, news_items)
        base_prompt = prompt
        last_error: LessonError | None = None
        for _ in range(MAX_GENERATION_ATTEMPTS):
            try:
                markdown = self._request(prompt)
            except InvalidModelResponse as exc:
                last_error = exc
                prompt = base_prompt + "\n\nThe previous API response was invalid. Return a complete lesson as plain Markdown only."
                continue
            try:
                word_count = validate_lesson(markdown, news_items)
                markdown = populate_sources_section(markdown, news_items)
                return markdown, word_count
            except LessonError as exc:
                last_error = exc
                # 이전 초안을 함께 주어 처음부터 다시 쓰지 않고 수정하게 한다.
                prompt = (
                    base_prompt
                    + "\n\nYour previous draft did not meet this validation rule: " + str(exc)
                    + " Revise that draft and return only the complete corrected Markdown.\n\n<previous_draft>\n"
                    + markdown
                    + "\n</previous_draft>"
                )
        raise last_error or LessonError("Could not generate a valid lesson.")