import argparse
import logging
import re
import sys
import traceback
import uuid
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from teachme.collectors import NewsCollector
from teachme.config import AppConfig, CEFR_LEVELS, ConfigurationError, load_config
from teachme.lesson import LessonError, LessonGenerator, add_lesson_metadata
from teachme.mailer import MailError, send_lesson
from teachme.storage import atomic_write_text, iso_timestamp, write_summary


EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FATAL = 2
EXIT_INTERRUPTED = 130


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create one daily reading lesson from public news feeds.")
    parser.add_argument("--language", help="Learning language for this run, e.g. English or French. Required unless --test-email is used.")
    parser.add_argument("--level", help="CEFR level for this run: A1, A2, B1, B2, C1, or C2. Required unless --test-email is used.")
    parser.add_argument("--test-email", action="store_true", help="Send a sample email with the configured Resend settings and exit.")
    parser.add_argument("--debug", action="store_true", help="Enable detailed diagnostics.")
    parser.add_argument("--date", dest="run_date", help="Output date in YYYY-MM-DD format.")
    parser.add_argument("--force", action="store_true", help="Regenerate an existing daily lesson.")
    args = parser.parse_args(argv)
    if not args.test_email and (not args.language or not args.level):
        missing = []
        if not args.language:
            missing.append("--language")
        if not args.level:
            missing.append("--level")
        parser.error("the following arguments are required: " + ", ".join(missing))
    return args


def _parse_date(raw: str | None, config: AppConfig) -> date:
    if raw is None:
        return datetime.now(config.timezone).date()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ConfigurationError("--date must use YYYY-MM-DD format.")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigurationError("--date must use YYYY-MM-DD format.") from exc


def _lesson_filename(language: str) -> str:
    safe_name = re.sub(r"[^\w-]+", "-", language.strip(), flags=re.UNICODE).strip("-_")
    if not safe_name or safe_name in {".", ".."}:
        raise ConfigurationError("--language must contain at least one letter or number.")
    return safe_name + ".md"


def _configure_logging(config: AppConfig, debug: bool) -> None:
    level = logging.DEBUG if debug else getattr(logging, config.log_level)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)


def _log_debug_traceback(logger: logging.Logger, error: BaseException, debug: bool) -> None:
    if debug:
        details = "".join(traceback.format_exception(type(error), error, error.__traceback__, chain=False))
        logger.debug("Diagnostic traceback (exception chaining omitted):\n%s", details)


def _base_summary(config: AppConfig, run_id: str, started_at: datetime, run_date: date) -> dict[str, object]:
    prompt_version = "unknown"
    try:
        prompt_text = config.prompt_file.read_text(encoding="utf-8")
        match = re.search(r"(?m)^<!--\s*prompt-version:\s*([^\s]+)\s*-->$", prompt_text)
        if match:
            prompt_version = match.group(1)
    except OSError:
        pass
    return {
        "run_id": run_id,
        "started_at": iso_timestamp(started_at),
        "finished_at": None,
        "date": run_date.isoformat(),
        "timezone": config.timezone_name,
        "language": config.learning_language,
        "level": config.cefr_level,
        "model": config.llm_model,
        "prompt_file": str(config.prompt_file.relative_to(config.project_dir)) if config.prompt_file.is_relative_to(config.project_dir) else config.prompt_file.name,
        "prompt_version": prompt_version,
        "counts": {"discovered": 0, "deduplicated": 0, "generated": 0, "skipped": 0, "failed": 0},
        "sources": {},
        "errors": [],
    }


def execute(config: AppConfig, args: argparse.Namespace, *, collector: NewsCollector | None = None, generator: LessonGenerator | None = None, mailer=send_lesson) -> int:
    language = args.language.strip()
    lesson_filename = _lesson_filename(language)
    if not language:
        raise ConfigurationError("--language must not be empty.")
    level = args.level.strip().upper()
    if level not in CEFR_LEVELS:
        raise ConfigurationError("--level must be one of A1, A2, B1, B2, C1, or C2.")
    config = replace(config, learning_language=language, cefr_level=level)
    _configure_logging(config, args.debug)
    logger = logging.getLogger("teachme")
    run_id = str(uuid.uuid4())
    started_at = datetime.now(config.timezone)
    run_date = _parse_date(args.run_date, config)
    output_dir = config.output_dir / run_date.isoformat()
    lesson_path = output_dir / lesson_filename
    summary_path = output_dir / "run-summary.json"
    summary = _base_summary(config, run_id, started_at, run_date)

    if lesson_path.exists() and not args.force:
        summary["counts"]["skipped"] = 1
        summary["status"] = "skipped_existing"
        summary["finished_at"] = iso_timestamp(datetime.now(config.timezone))
        write_summary(summary_path, summary)
        logger.info("Existing daily lesson found; skipped. Use --force to regenerate.")
        return EXIT_SUCCESS
    if lesson_path.exists() and args.force:
        logger.info("--force enabled; the existing daily lesson will be replaced after successful generation.")

    collector = collector or NewsCollector(config)
    generator = generator or LessonGenerator(config)
    logger.info("Run %s started for %s in %s.", run_id, run_date.isoformat(), language)
    news_items, source_results = collector.collect_all()
    for source_id, result in source_results.items():
        if result.get("status") == "failed":
            logger.warning("Source %s failed: %s", source_id, result.get("error"))
        else:
            logger.info("Source %s: %s (%s item(s)).", source_id, result.get("status"), result.get("discovered"))
    summary["sources"] = source_results
    discovered = sum(int(result.get("discovered", 0)) for result in source_results.values())
    summary["counts"]["discovered"] = discovered
    summary["counts"]["deduplicated"] = len(news_items)
    source_failures = sum(result.get("status") == "failed" for result in source_results.values())
    summary["counts"]["failed"] = source_failures
    if source_failures:
        summary["errors"].extend(
            {"source_id": source_id, "code": result.get("error_code"), "message": result.get("error")}
            for source_id, result in source_results.items()
            if result.get("status") == "failed"
        )

    status = "success"
    exit_code = EXIT_SUCCESS
    if news_items:
        try:
            markdown, word_count = generator.generate(news_items)
            markdown = add_lesson_metadata(markdown, config, datetime.now(config.timezone), getattr(generator, "last_model", None))
            atomic_write_text(lesson_path, markdown.rstrip() + "\n")
            summary["counts"]["generated"] = 1
            summary["reading_word_count"] = word_count
            if config.mail_to:
                try:
                    recipient_count = mailer(
                        config,
                        markdown.rstrip() + "\n",
                        language=language,
                        run_date=run_date.isoformat(),
                    )
                    summary["email"] = {"status": "sent", "recipients": recipient_count}
                    logger.info("Lesson emailed to %d recipient(s).", recipient_count)
                except MailError as exc:
                    summary["email"] = {"status": "failed"}
                    summary["counts"]["failed"] += 1
                    summary["errors"].append({"code": "email_error", "message": str(exc)})
                    logger.error("%s", exc)
                    _log_debug_traceback(logger, exc, args.debug)
                    exit_code = EXIT_PARTIAL
        except LessonError as exc:
            summary["counts"]["failed"] += 1
            summary["errors"].append({"code": "lesson_generation_error", "message": str(exc)})
            logger.error("Daily lesson generation failed: %s", exc)
            _log_debug_traceback(logger, exc, args.debug)
            exit_code = EXIT_PARTIAL
    elif not source_failures:
        status = "no_news"

    if source_failures or summary["counts"]["failed"] > source_failures:
        status = "partial_failure"
        exit_code = EXIT_PARTIAL

    summary["status"] = status
    summary["finished_at"] = iso_timestamp(datetime.now(config.timezone))
    write_summary(summary_path, summary)
    logger.info(
        "Run %s finished: discovered=%d deduplicated=%d generated=%d failed=%d.",
        run_id,
        summary["counts"]["discovered"],
        summary["counts"]["deduplicated"],
        summary["counts"]["generated"],
        summary["counts"]["failed"],
    )
    return exit_code


def send_test_email(config: AppConfig, args: argparse.Namespace, *, mailer=send_lesson) -> int:
    logger = logging.getLogger("teachme")
    if not config.mail_to:
        logger.error("Cannot send a test email: set NOTIFY_EMAIL and RESEND_API_KEY in .env.")
        return EXIT_FATAL
    now = datetime.now(config.timezone)
    body = (
        "# Teach Me test email\n\n"
        "This is a test message from Teach Me. If you can read it, email delivery works.\n\n"
        f"- Sent: {now.isoformat(timespec='seconds')}\n"
        f"- Sender: {config.mail_from}\n"
    )
    try:
        count = mailer(config, body, language="Test", run_date=now.date().isoformat())
    except MailError as exc:
        logger.error("%s", exc)
        _log_debug_traceback(logger, exc, args.debug)
        return EXIT_PARTIAL
    logger.info("Test email sent to %d recipient(s).", count)
    return EXIT_SUCCESS


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_dir = Path(__file__).resolve().parents[2]
    try:
        config = load_config(project_dir)
        _parse_date(args.run_date, config)
        _configure_logging(config, args.debug)
        if args.test_email:
            return send_test_email(config, args)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        return execute(config, args)
    except (ConfigurationError, OSError) as exc:
        logger = logging.getLogger("teachme")
        logger.error("Cannot start: %s", exc)
        _log_debug_traceback(logger, exc, args.debug)
        return EXIT_FATAL
    except KeyboardInterrupt:
        logging.getLogger("teachme").warning("Run interrupted.")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())