import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from teachme.models import Source


DEFAULT_SOURCES = [
    {"id": "bbc", "name": "BBC", "url": "https://www.bbc.com/news"},
    {"id": "guardian", "name": "The Guardian", "url": "https://www.theguardian.com/international"},
    {"id": "cnn", "name": "CNN", "url": "https://www.cnn.com"},
    {"id": "ap", "name": "Associated Press", "url": "https://apnews.com"},
    {"id": "lemonde", "name": "Le Monde", "url": "https://www.lemonde.fr"},
    {"id": "brusselstimes", "name": "The Brussels Times", "url": "https://www.brusselstimes.com/"},
]
CEFR_LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}
EMAIL_PATTERN = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+")
DEFAULT_MAIL_FROM = "Teach Me <onboarding@resend.dev>"


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class AppConfig:
    project_dir: Path
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    learning_language: str
    cefr_level: str
    max_articles_per_source: int
    max_total_input_chars: int
    timezone: ZoneInfo
    timezone_name: str
    http_timeout_seconds: float
    http_max_retries: int
    llm_max_tokens: int | None
    output_dir: Path
    prompt_file: Path
    log_level: str
    sources: tuple[Source, ...]
    mail_to: tuple[str, ...] = ()
    mail_from: str = DEFAULT_MAIL_FROM
    resend_api_key: str | None = None


def _dotenv_value_complete(value: str) -> bool:
    quote: str | None = None
    escaped = False
    bracket_depth = 0
    for character in value:
        if escaped:
            escaped = False
            continue
        if quote:
            if character == "\\" and quote == '"':
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in "[{":
            bracket_depth += 1
        elif character in "]}":
            bracket_depth -= 1
    return quote is None and bracket_depth <= 0


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line_number = index + 1
        raw_line = lines[index]
        line = raw_line.strip()
        if not line or line.startswith("#"):
            index += 1
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"Invalid .env syntax on line {line_number}.")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigurationError(f"Invalid .env variable name on line {line_number}.")
        while not _dotenv_value_complete(value):
            index += 1
            if index >= len(lines):
                raise ConfigurationError(f"Unterminated multiline .env value for {key} starting on line {line_number}.")
            value += "\n" + lines[index]
        index += 1
        if key in os.environ:
            continue
        if key == "NEWS_SOURCES_JSON" and value.lstrip().startswith(("[", "{")):
            os.environ[key] = value.strip()
            continue
        try:
            parsed = shlex.split(value, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid .env value for {key}.") from exc
        os.environ[key] = " ".join(parsed) if parsed else ""


def _positive_int(name: str, default: str, *, allow_zero: bool = False) -> int:
    raw = os.environ.get(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}.")
    return value


def _positive_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number.") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    return value


def _resolve_path(project_dir: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (project_dir / path).resolve()


def _parse_sources(raw: str | None) -> tuple[Source, ...]:
    try:
        records = json.loads(raw) if raw else DEFAULT_SOURCES
    except json.JSONDecodeError as exc:
        raise ConfigurationError("NEWS_SOURCES_JSON must be valid JSON.") from exc
    if not isinstance(records, list) or not records:
        raise ConfigurationError("NEWS_SOURCES_JSON must be a non-empty JSON array.")

    sources: list[Source] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ConfigurationError(f"NEWS_SOURCES_JSON item {index + 1} must be an object.")
        source_id = record.get("id")
        name = record.get("name")
        url = record.get("url")
        if not all(isinstance(value, str) and value.strip() for value in (source_id, name, url)):
            raise ConfigurationError(f"NEWS_SOURCES_JSON item {index + 1} needs non-empty id, name, and url.")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", source_id):
            raise ConfigurationError(f"Invalid source id at NEWS_SOURCES_JSON item {index + 1}.")
        if source_id in seen_ids:
            raise ConfigurationError(f"Duplicate source id in NEWS_SOURCES_JSON: {source_id}.")
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ConfigurationError(f"Source {source_id} URL must use http or https.")
        if parsed_url.username or parsed_url.password or any(
            re.search(r"token|secret|password|api[_-]?key|auth", key, re.IGNORECASE)
            for key, _ in parse_qsl(parsed_url.query, keep_blank_values=True)
        ):
            raise ConfigurationError(f"Source {source_id} URL must not contain credentials or secret query parameters.")
        seen_ids.add(source_id)
        sources.append(Source(id=source_id, name=name.strip(), url=url.strip()))
    return tuple(sources)


def _load_mail_settings() -> dict[str, object]:
    recipients = tuple(dict.fromkeys(part for part in re.split(r"[,;\s]+", os.environ.get("NOTIFY_EMAIL", "")) if part))
    if not recipients:
        return {}
    if not all(EMAIL_PATTERN.fullmatch(address) for address in recipients):
        raise ConfigurationError("NOTIFY_EMAIL contains an invalid email address.")
    if len(recipients) > 50:
        raise ConfigurationError("NOTIFY_EMAIL supports at most 50 recipients.")

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key or re.search(r"\s", api_key):
        raise ConfigurationError("RESEND_API_KEY is required when NOTIFY_EMAIL is set.")
    sender = os.environ.get("MAIL_FROM", "").strip() or DEFAULT_MAIL_FROM
    if any(ord(character) < 32 or ord(character) == 127 for character in sender):
        raise ConfigurationError("MAIL_FROM must not contain control characters.")
    mailbox = re.fullmatch(r"(?:(?P<name>[^<>]+?)\s*)?<(?P<address>[^<>]+)>", sender)
    sender_address = mailbox.group("address").strip() if mailbox else sender
    if not EMAIL_PATTERN.fullmatch(sender_address):
        raise ConfigurationError("MAIL_FROM must be an email address or a display name followed by an email address.")
    if mailbox and not mailbox.group("name").strip():
        raise ConfigurationError("MAIL_FROM display name must not be empty.")

    return {"mail_to": recipients, "mail_from": sender, "resend_api_key": api_key}


def load_config(project_dir: Path) -> AppConfig:
    project_dir = project_dir.resolve()
    _load_dotenv(project_dir / ".env")

    required = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigurationError("Missing required environment variable(s): " + ", ".join(missing))

    base_url = os.environ["LLM_BASE_URL"].strip().rstrip("/")
    parsed_base_url = urlsplit(base_url)
    if parsed_base_url.scheme not in {"https", "http"} or not parsed_base_url.hostname:
        raise ConfigurationError("LLM_BASE_URL must use http or https.")
    if parsed_base_url.username or parsed_base_url.password or any(
        re.search(r"token|secret|password|api[_-]?key|auth", key, re.IGNORECASE)
        for key, _ in parse_qsl(parsed_base_url.query, keep_blank_values=True)
    ):
        raise ConfigurationError("LLM_BASE_URL must not contain credentials or secret query parameters.")

    timezone_name = os.environ.get("TIMEZONE", "UTC").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"TIMEZONE is not a known IANA timezone: {timezone_name}.") from exc

    max_tokens_raw = os.environ.get("LLM_MAX_TOKENS", "").strip()
    max_tokens = None
    if max_tokens_raw:
        try:
            max_tokens = int(max_tokens_raw)
        except ValueError as exc:
            raise ConfigurationError("LLM_MAX_TOKENS must be an integer.") from exc
        if max_tokens < 1:
            raise ConfigurationError("LLM_MAX_TOKENS must be greater than zero.")

    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigurationError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR.")

    return AppConfig(
        project_dir=project_dir,
        llm_base_url=base_url,
        llm_api_key=os.environ["LLM_API_KEY"].strip(),
        llm_model=os.environ["LLM_MODEL"].strip(),
        learning_language="English",
        cefr_level="B1",
        max_articles_per_source=_positive_int("MAX_ARTICLES_PER_SOURCE", "3"),
        max_total_input_chars=_positive_int("MAX_TOTAL_INPUT_CHARS", "24000"),
        timezone=timezone,
        timezone_name=timezone_name,
        http_timeout_seconds=_positive_float("HTTP_TIMEOUT_SECONDS", "20"),
        http_max_retries=_positive_int("HTTP_MAX_RETRIES", "3", allow_zero=True),
        llm_max_tokens=max_tokens,
        output_dir=_resolve_path(project_dir, os.environ.get("OUTPUT_DIR", "result")),
        prompt_file=_resolve_path(project_dir, os.environ.get("PROMPT_FILE", "prompts/news_to_lesson.md")),
        log_level=log_level,
        sources=_parse_sources(os.environ.get("NEWS_SOURCES_JSON")),
        **_load_mail_settings(),
    )