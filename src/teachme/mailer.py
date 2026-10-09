import html
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from teachme.config import AppConfig

RESEND_ENDPOINT = "https://api.resend.com/emails"
LOGGER = logging.getLogger(__name__)
WRAPPER_STYLE = (
    "font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:16px;"
    "line-height:1.6;color:#222;max-width:640px;margin:0 auto;padding:16px;"
)
HEADING_STYLES = {
    1: "font-size:24px;margin:0 0 8px;",
    2: "font-size:19px;margin:28px 0 8px;padding-bottom:4px;border-bottom:1px solid #e5e7eb;",
    3: "font-size:16px;margin:16px 0 6px;",
}
LINK_PATTERN = re.compile(r"\[((?:\\.|[^\]\\])+)\]\((https?://[^)\s]+)\)")
CODE_PATTERN = re.compile(r"`([^`]+)`")
BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
ITALIC_PATTERN = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
HEADING_PATTERN = re.compile(r"(#{1,6})\s+(.*?)\s*#*\s*")
BULLET_PATTERN = re.compile(r"\s*[-*+]\s+(.*)")
NUMBERED_PATTERN = re.compile(r"\s*(\d+)[.)]\s+(.*)")
RULE_PATTERN = re.compile(r"\s*([-*_])(\s*\1){2,}\s*")


class MailError(Exception):
    pass


def _emphasis(escaped: str) -> str:
    escaped = CODE_PATTERN.sub(r'<code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">\1</code>', escaped)
    escaped = BOLD_PATTERN.sub(r"<strong>\1</strong>", escaped)
    return ITALIC_PATTERN.sub(r"<em>\1</em>", escaped)


def _inline(text: str) -> str:
    # 기사·LLM에서 온 텍스트이므로 모든 조각을 이스케이프하고 http(s) 링크만 허용한다.
    parts: list[str] = []
    last = 0
    for match in LINK_PATTERN.finditer(text):
        parts.append(_emphasis(html.escape(text[last:match.start()])))
        label = re.sub(r"\\([\\\[\]])", r"\1", match.group(1))
        parts.append(f'<a href="{html.escape(match.group(2))}" style="color:#1a56db;">{_emphasis(html.escape(label))}</a>')
        last = match.end()
    parts.append(_emphasis(html.escape(text[last:])))
    return "".join(parts)


def markdown_to_html(markdown: str) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    list_tag: str | None = None
    list_start = 1

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append('<p style="margin:0 0 12px;">' + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_tag:
            start = f' start="{list_start}"' if list_tag == "ol" and list_start != 1 else ""
            blocks.append(f'<{list_tag}{start} style="margin:0 0 12px;padding-left:24px;">' + "".join(items) + f"</{list_tag}>")
            items.clear()
            list_tag = None

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            # 목록 항목 사이의 빈 줄이 번호를 1로 되돌리지 않도록 목록은 열어 둔다.
            flush_paragraph()
            continue
        if RULE_PATTERN.fullmatch(line):
            flush_paragraph()
            flush_list()
            blocks.append('<hr style="border:0;border-top:1px solid #e5e7eb;margin:20px 0;">')
            continue
        heading = HEADING_PATTERN.fullmatch(line)
        if heading:
            flush_paragraph()
            flush_list()
            level = min(len(heading.group(1)), 3)
            blocks.append(f'<h{level} style="{HEADING_STYLES[level]}">{_inline(heading.group(2))}</h{level}>')
            continue
        bullet = BULLET_PATTERN.fullmatch(line)
        numbered = NUMBERED_PATTERN.fullmatch(line)
        if bullet or numbered:
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                flush_paragraph()
                flush_list()
                list_tag = tag
                list_start = int(numbered.group(1)) if numbered else 1
            text = bullet.group(1) if bullet else numbered.group(2)
            items.append(f'<li style="margin:4px 0;">{_inline(text)}</li>')
            continue
        flush_list()
        paragraph.append(line.strip())
    flush_paragraph()
    flush_list()
    return f'<div style="{WRAPPER_STYLE}">' + "\n".join(blocks) + "</div>"


def _provider_message(error: HTTPError, api_key: str) -> str | None:
    try:
        body = json.loads(error.read(8_192))
    except (OSError, ValueError):
        return None
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str):
        return None
    message = message.replace(api_key, "[REDACTED]")
    return re.sub(r"[\x00-\x1f\x7f]+", " ", message).strip()[:200] or None


def send_lesson(
    config: AppConfig,
    markdown: str,
    *,
    language: str,
    run_date: str,
    opener=urlopen,
) -> int:
    # 사용자 입력이 제목에 들어가므로 개행 등 공백 문자를 하나로 합친다.
    safe_language = re.sub(r"\s+", " ", language).strip()[:60]
    payload = {
        "from": config.mail_from,
        "to": list(config.mail_to),
        "subject": f"Teach Me: {safe_language} reading lesson ({run_date})",
        "html": markdown_to_html(markdown),
        "text": markdown,
    }
    request = Request(
        RESEND_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + (config.resend_api_key or ""),
            "Content-Type": "application/json",
            # Resend는 User-Agent 헤더가 없는 요청을 거부한다.
            "User-Agent": "TeachMeNewsDigest/0.1.0",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=config.http_timeout_seconds) as response:
            raw = response.read(65_537)
    except HTTPError as exc:
        detail = _provider_message(exc, config.resend_api_key or "")
        raise MailError(f"Email delivery failed (HTTP {exc.code}{': ' + detail if detail else ''}).") from exc
    except (URLError, OSError) as exc:
        raise MailError(f"Email delivery failed ({type(exc).__name__}).") from exc

    try:
        email_id = json.loads(raw).get("id")
    except (ValueError, AttributeError):
        email_id = None
    if not isinstance(email_id, str) or not email_id:
        raise MailError("Email delivery failed (Resend response had no email id).")
    LOGGER.debug("Resend accepted the email (id %s).", email_id)
    return len(config.mail_to)
