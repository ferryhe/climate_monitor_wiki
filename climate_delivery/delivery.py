import html
import hashlib
import json
import re
import smtplib
import ssl
from datetime import date, datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr
from pathlib import Path
from typing import Any, Callable

from .config import DeliveryConfig
from .errors import DeliveryError, InputError, LockStateError
from .io import atomic_write_json, exclusive_lock


class _AttemptFailure(Exception):
    def __init__(self, *, unknown: bool):
        super().__init__("SMTP attempt failed")
        self.unknown = unknown


def _validate_summary(summary: dict[str, Any]) -> None:
    if not isinstance(summary, dict) or summary.get("schema_version") != 1:
        raise InputError("summary schema_version must be 1")
    report = summary.get("report")
    if not isinstance(report, dict) or not all(report.get(key) for key in ("date", "title", "sha256")):
        raise InputError("summary report metadata is incomplete")
    try:
        report_date = date.fromisoformat(report["date"])
    except (TypeError, ValueError) as exc:
        raise InputError("summary report date is invalid") from exc
    if report_date.weekday() != 0 or not isinstance(report["title"], str):
        raise InputError("summary must describe a Monday weekly report")
    if not isinstance(report["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", report["sha256"]):
        raise InputError("summary report sha256 is invalid")
    if not isinstance(summary.get("executive_summary"), list) or not isinstance(summary.get("highlights"), list):
        raise InputError("summary content is incomplete")
    if not all(isinstance(item, str) for item in summary["executive_summary"]):
        raise InputError("summary executive items must be strings")
    for item in summary["highlights"]:
        if not isinstance(item, dict) or set(item) != {"pillar", "title", "summary", "url"}:
            raise InputError("summary highlight schema is invalid")
        if not isinstance(item["pillar"], str) or item["pillar"] not in {"A", "B"}:
            raise InputError("summary highlight values are invalid")
        if not all(isinstance(item[key], str) for key in ("title", "summary", "url")):
            raise InputError("summary highlight values are invalid")
        if not re.fullmatch(r"https?://[^\s]+", item["url"]):
            raise InputError("summary highlight URL must be HTTP(S)")
    original_links = summary.get("original_links")
    if not isinstance(original_links, list) or not original_links:
        raise InputError("summary original_links must be a non-empty list")
    if any(not isinstance(item, str) or not re.fullmatch(r"https?://[^\s]+", item) for item in original_links):
        raise InputError("summary original_links entries must be HTTP(S) strings")


def load_summary_with_sha256(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError("summary must be a readable JSON file") from exc
    _validate_summary(value)
    return value, hashlib.sha256(raw).hexdigest()


def load_summary(path: Path) -> dict[str, Any]:
    return load_summary_with_sha256(path)[0]


def _featured_highlights(summary: dict[str, Any]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for pillar in ("A", "B"):
        selected.extend([item for item in summary["highlights"] if item["pillar"] == pillar][:3])
    return selected


def _scope_line(summary: dict[str, Any]) -> str:
    sites = summary["report"].get("sites", {})
    if all(isinstance(sites.get(key), int) for key in ("checked", "succeeded", "failed")):
        return (
            f"{sites['checked']} sites checked - "
            f"{sites['succeeded']} succeeded - {sites['failed']} failed"
        )
    return "Weekly report"


def _plain_body(summary: dict[str, Any]) -> str:
    report = summary["report"]
    featured = _featured_highlights(summary)
    lines = [
        report["title"],
        f"Report week of {report['date']}",
        _scope_line(summary),
        "",
        "Executive Summary",
    ]
    lines.extend(f"- {item}" for item in summary["executive_summary"])
    lines.extend(["", "Highlights this week"])
    for item in featured:
        lines.extend([f"- Pillar {item['pillar']}: {item['title']}", f"  {item['summary']}", f"  {item['url']}"])
    lines.extend(
        [
            "",
            f"The attached PDF contains all {len(summary['highlights'])} report highlights.",
            "This is a deterministic summary of the canonical weekly report.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _html_body(summary: dict[str, Any]) -> str:
    report = summary["report"]
    executive = "".join(
        '<li style="margin:0 0 8px 0;">{}</li>'.format(html.escape(item))
        for item in summary["executive_summary"]
    )
    highlights = "".join(
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 14px 0;background:#f4f7f9;border-left:4px solid #1f6f8b;">'
        '<tr><td style="padding:16px 18px;">'
        '<p style="margin:0 0 6px 0;font-size:11px;line-height:16px;font-weight:700;'
        'letter-spacing:.06em;text-transform:uppercase;color:#1f6f8b;">Pillar {pillar}</p>'
        '<h3 style="margin:0 0 8px 0;font-size:15px;line-height:21px;color:#0b3d62;">{title}</h3>'
        '<p style="margin:0 0 10px 0;font-size:14px;line-height:21px;color:#33414f;">{body}</p>'
        '<p style="margin:0;font-size:13px;line-height:19px;">'
        '<a href="{url}" style="color:#1a73e8;text-decoration:none;">View original source</a>'
        "</p></td></tr></table>".format(
            pillar=html.escape(item["pillar"]),
            title=html.escape(item["title"]),
            body=html.escape(item["summary"]),
            url=html.escape(item["url"], quote=True),
        )
        for item in _featured_highlights(summary)
    )
    title = html.escape(report["title"])
    report_date = html.escape(report["date"])
    scope = html.escape(_scope_line(summary))
    total = len(summary["highlights"])
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f4f6f8;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;background:#f4f6f8;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:640px;background:#ffffff;border-radius:12px;overflow:hidden;'
        'box-shadow:0 4px 18px rgba(11,61,98,.10);font-family:-apple-system,BlinkMacSystemFont,'
        'Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        '<tr><td style="padding:28px 32px;background:#0b3d62;'
        'background-image:linear-gradient(135deg,#0b3d62,#1f6f8b);">'
        '<p style="margin:0 0 8px 0;font-size:11px;line-height:16px;font-weight:700;'
        'letter-spacing:.12em;text-transform:uppercase;color:#a9d6e5;">IAA Weekly Climate Newsletter</p>'
        f'<h1 style="margin:0 0 10px 0;font-size:22px;line-height:29px;color:#ffffff;">{title}</h1>'
        f'<p style="margin:0;font-size:13px;line-height:20px;color:#cfe6ee;">Report week of {report_date}'
        f" &bull; {scope}</p></td></tr>"
        '<tr><td style="padding:26px 32px 8px 32px;color:#33414f;">'
        '<p style="margin:0 0 16px 0;font-size:14px;line-height:22px;">'
        "This weekly briefing connects current climate developments to actuarial monitoring. "
        "The attached PDF contains the complete set of report highlights and source links.</p>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 24px 0;background:#f0f4f7;border-radius:8px;">'
        '<tr><td style="padding:18px 20px;">'
        '<h2 style="margin:0 0 10px 0;font-size:17px;line-height:23px;color:#0b3d62;">Executive Summary</h2>'
        f'<ul style="margin:0;padding-left:20px;font-size:14px;line-height:21px;color:#33414f;">{executive}</ul>'
        "</td></tr></table>"
        '<h2 style="margin:0 0 16px 0;padding-left:12px;border-left:4px solid #1f6f8b;'
        'font-size:18px;line-height:24px;color:#0b3d62;">Highlights this week</h2>'
        f"{highlights}</td></tr>"
        '<tr><td style="padding:18px 32px;background:#f0f4f7;">'
        f'<p style="margin:0;font-size:12px;line-height:18px;color:#617181;">Automated, deterministic summary '
        f"of the canonical weekly report. The attached PDF contains all {total} report highlights. "
        "Links point to the original sources recorded in the report.</p></td></tr>"
        "</table></td></tr></table></body></html>"
    )


def _summary_sha256(summary: dict[str, Any]) -> str:
    payload = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    return hashlib.sha256(payload).hexdigest()


def _address_fingerprint(address: str) -> str:
    normalized = address.strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _read_pdf(pdf_path: Path, *, existing_state: bool = False) -> bytes:
    try:
        return Path(pdf_path).read_bytes()
    except OSError as exc:
        if existing_state:
            raise LockStateError("PDF artifact changed or disappeared; manual reconciliation required") from exc
        raise InputError("PDF attachment does not exist or is unreadable") from exc


def prepare_messages(
    summary: dict[str, Any],
    pdf_path: Path,
    config: DeliveryConfig,
    *,
    pdf_data: bytes | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[tuple[str, bytes]]:
    _validate_summary(summary)
    if pdf_data is None:
        pdf_data = _read_pdf(pdf_path)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None or now.utcoffset() is None:
        raise InputError("message clock must return a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    output: list[tuple[str, bytes]] = []
    for recipient in config.recipients:
        message = EmailMessage()
        message["Subject"] = f"Weekly Climate Monitor — {summary['report']['date']}"
        message["From"] = formataddr((config.smtp.from_name, config.smtp.from_address))
        message["To"] = recipient.address
        message["Date"] = format_datetime(now, usegmt=True)
        message["Message-ID"] = (
            f"<climate-delivery.{summary['report']['sha256']}.{recipient.id}@climate.aiinforsearch.com>"
        )
        message.set_content(_plain_body(summary))
        message.add_alternative(_html_body(summary), subtype="html")
        message.add_attachment(
            pdf_data,
            maintype="application",
            subtype="pdf",
            filename=f"climate-monitor-{summary['report']['date']}.pdf",
        )
        output.append((recipient.id, message.as_bytes(policy=SMTP)))
    return output


def _initial_state(
    summary: dict[str, Any],
    config: DeliveryConfig,
    *,
    summary_sha256: str,
    pdf_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "report_sha256": summary["report"]["sha256"],
        "artifacts": {"summary_sha256": summary_sha256, "pdf_sha256": pdf_sha256},
        "recipients": {
            item.id: {"address_fingerprint": _address_fingerprint(item.address), "status": "pending"}
            for item in config.recipients
        },
    }


def _read_state(
    path: Path,
    summary: dict[str, Any],
    config: DeliveryConfig,
    *,
    summary_sha256: str,
    pdf_sha256: str,
) -> dict[str, Any]:
    if not path.exists():
        return _initial_state(
            summary,
            config,
            summary_sha256=summary_sha256,
            pdf_sha256=pdf_sha256,
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LockStateError("delivery state is unreadable; manual reconciliation required") from exc
    expected_ids = {item.id for item in config.recipients}
    if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("recipients"), dict):
        raise LockStateError("delivery state is invalid; manual reconciliation required")
    if state.get("report_sha256") != summary["report"]["sha256"] or set(state["recipients"]) != expected_ids:
        raise LockStateError("delivery state does not match config; manual reconciliation required")
    expected_artifacts = {"summary_sha256": summary_sha256, "pdf_sha256": pdf_sha256}
    if state.get("artifacts") != expected_artifacts:
        raise LockStateError("delivery artifact binding changed; manual reconciliation required")
    valid = {"pending", "sending", "sent", "failed", "unknown"}
    if any(not isinstance(item, dict) or item.get("status") not in valid for item in state["recipients"].values()):
        raise LockStateError("delivery state is invalid; manual reconciliation required")
    for recipient in config.recipients:
        if state["recipients"][recipient.id].get("address_fingerprint") != _address_fingerprint(recipient.address):
            raise LockStateError("delivery recipient binding changed; manual reconciliation required")
    return state


def _recipient_results(state: dict[str, Any], config: DeliveryConfig) -> list[dict[str, str]]:
    return [
        {
            "id": recipient.id,
            "status": state["recipients"][recipient.id]["status"],
        }
        for recipient in config.recipients
    ]


def _persist_state(path: Path, state: dict[str, Any], transition: str) -> None:
    try:
        atomic_write_json(path, state)
    except Exception as exc:
        raise LockStateError(
            f"could not persist {transition} delivery state; manual reconciliation required"
        ) from exc


def _smtp_send(raw: bytes, config: DeliveryConfig, smtp_factory: Callable[..., Any] | None) -> None:
    message = message_from_bytes(raw, policy=SMTP)
    accepted = False
    try:
        if smtp_factory is not None:
            client_context = smtp_factory(config.smtp.host, config.smtp.port, timeout=30)
        elif config.smtp.security == "ssl":
            client_context = smtplib.SMTP_SSL(
                config.smtp.host,
                config.smtp.port,
                timeout=30,
                context=ssl.create_default_context(),
            )
        else:
            client_context = smtplib.SMTP(config.smtp.host, config.smtp.port, timeout=30)
        with client_context as client:
            if config.smtp.security == "starttls":
                client.starttls(context=ssl.create_default_context())
            client.login(config.smtp.username, config.smtp.password)
            try:
                refused = client.send_message(message)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                accepted = True
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPResponseException) as exc:
                raise _AttemptFailure(unknown=False) from exc
            except Exception as exc:
                raise _AttemptFailure(unknown=True) from exc
    except _AttemptFailure:
        raise
    except Exception as exc:
        # Setup failures occur before DATA is submitted. If SMTP accepted DATA
        # and only connection cleanup failed, delivery is already confirmed.
        if accepted:
            return
        raise _AttemptFailure(unknown=False) from exc


def deliver(
    summary: dict[str, Any],
    pdf_path: Path,
    config: DeliveryConfig,
    state_dir: Path,
    *,
    dry_run: bool = False,
    smtp_factory: Callable[..., Any] | None = None,
    acquire_lock: bool = True,
    summary_artifact_sha256: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    _validate_summary(summary)
    summary_hash = summary_artifact_sha256 or _summary_sha256(summary)
    if not re.fullmatch(r"[0-9a-f]{64}", summary_hash):
        raise InputError("summary artifact sha256 is invalid")
    digest = summary["report"]["sha256"]
    state_dir = Path(state_dir)
    if dry_run:
        state_path = state_dir / f"{digest}.json"

        def dry_execute() -> dict[str, Any]:
            pdf_data = _read_pdf(pdf_path, existing_state=state_path.exists())
            if state_path.exists():
                state = _read_state(
                    state_path,
                    summary,
                    config,
                    summary_sha256=summary_hash,
                    pdf_sha256=hashlib.sha256(pdf_data).hexdigest(),
                )
                if any(value["status"] in {"sending", "unknown"} for value in state["recipients"].values()):
                    raise LockStateError("ambiguous sending/unknown state; manual reconciliation required")
            messages = prepare_messages(summary, pdf_path, config, pdf_data=pdf_data, clock=clock)
            return {
                "status": "dry-run",
                "messages": len(messages),
                "recipients": [
                    {
                        "id": item.id,
                        "status": "pending",
                    }
                    for item in config.recipients
                ],
            }

        if acquire_lock:
            with exclusive_lock(state_dir, digest):
                return dry_execute()
        return dry_execute()

    def execute() -> dict[str, Any]:
        state_path = state_dir / f"{digest}.json"
        pdf_data = _read_pdf(pdf_path, existing_state=state_path.exists())
        pdf_hash = hashlib.sha256(pdf_data).hexdigest()
        state = _read_state(
            state_path,
            summary,
            config,
            summary_sha256=summary_hash,
            pdf_sha256=pdf_hash,
        )
        ambiguous = [key for key, value in state["recipients"].items() if value["status"] in {"sending", "unknown"}]
        if ambiguous:
            raise LockStateError("ambiguous sending/unknown state; manual reconciliation required")
        if all(value["status"] == "sent" for value in state["recipients"].values()):
            return {
                "status": "already-sent",
                "messages": 0,
                "recipients": _recipient_results(state, config),
            }
        messages = prepare_messages(summary, pdf_path, config, pdf_data=pdf_data, clock=clock)
        for recipient_id, raw in messages:
            if state["recipients"][recipient_id]["status"] == "sent":
                continue
            state["recipients"][recipient_id]["status"] = "sending"
            state["recipients"][recipient_id].pop("error", None)
            _persist_state(state_path, state, "sending")
            try:
                _smtp_send(raw, config, smtp_factory)
            except _AttemptFailure as exc:
                state["recipients"][recipient_id]["status"] = "unknown" if exc.unknown else "failed"
                state["recipients"][recipient_id]["error"] = (
                    type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__
                )
                transition = "unknown" if exc.unknown else "failed"
                _persist_state(state_path, state, transition)
                if exc.unknown:
                    raise LockStateError(
                        f"delivery outcome is unknown for recipient id {recipient_id}; manual reconciliation required"
                    ) from exc
                raise DeliveryError(f"delivery failed for recipient id {recipient_id}") from exc
            state["recipients"][recipient_id]["status"] = "sent"
            state["recipients"][recipient_id].pop("error", None)
            _persist_state(state_path, state, "sent")
        return {
            "status": "sent",
            "messages": sum(value["status"] == "sent" for value in state["recipients"].values()),
            "recipients": _recipient_results(state, config),
        }

    if acquire_lock:
        with exclusive_lock(state_dir, digest):
            return execute()
    return execute()
