import hashlib
import json
import re
import smtplib
import ssl
from dataclasses import replace
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

from climate_delivery.config import load_delivery_config
from climate_delivery.delivery import deliver, prepare_messages
from climate_delivery.errors import DeliveryError, InputError, LockStateError


SUMMARY = {
    "schema_version": 1,
    "report": {
        "date": "2026-08-10",
        "title": "Climate <Monitor>",
        "sha256": "a" * 64,
        "sites": {"checked": 3, "succeeded": 2, "failed": 1},
    },
    "executive_summary": ["Evidence & interpretation"],
    "highlights": [
        {"pillar": "A", "title": "A <finding>", "summary": "Safe & useful", "url": "https://example.test/a"}
    ],
    "original_links": ["https://example.test/a"],
}


def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "delivery.yaml"
    path.write_text(
        """version: 1
smtp:
  host_env: TEST_SMTP_HOST
  port_env: TEST_SMTP_PORT
  username_env: TEST_SMTP_USER
  password_env: TEST_SMTP_PASSWORD
  from_address_env: TEST_FROM_ADDRESS
  from_name: IAA Weekly Climate Newsletter
  security: starttls
recipients:
  - id: alpha
    address: alpha@example.test
  - id: beta
    address: beta@example.test
  - id: gamma
    address: gamma@example.test
  - id: delta
    address: delta@example.test
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def configured(monkeypatch, tmp_path):
    values = {
        "TEST_SMTP_HOST": "smtp.example.test",
        "TEST_SMTP_PORT": "587",
        "TEST_SMTP_USER": "sender-user",
        "TEST_SMTP_PASSWORD": "not-a-real-password",
        "TEST_FROM_ADDRESS": "sender@example.test",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return load_delivery_config(config_file(tmp_path))


def test_mime_is_plain_plus_escaped_html_with_pdf_and_no_fake_unsubscribe(configured, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    messages = prepare_messages(SUMMARY, pdf, configured)

    assert len(messages) == 4
    recipient_id, raw = messages[0]
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    assert recipient_id == "alpha"
    assert parsed["To"] == "alpha@example.test"
    assert parsed["From"].addresses[0].display_name == "IAA Weekly Climate Newsletter"
    assert parsed["From"].addresses[0].addr_spec == "sender@example.test"
    assert parsed["List-Unsubscribe"] is None
    plain = parsed.get_body(preferencelist=("plain",)).get_content()
    html = parsed.get_body(preferencelist=("html",)).get_content()
    assert "Climate <Monitor>" in plain
    assert "Climate &lt;Monitor&gt;" in html
    assert "Evidence &amp; interpretation" in html
    assert 'role="presentation"' in html
    assert "max-width:640px" in html
    assert "Highlights this week" in html
    assert "3 sites checked" in html
    attachment = next(parsed.iter_attachments())
    assert attachment.get_filename() == "climate-monitor-2026-08-10.pdf"
    assert attachment.get_content() == b"%PDF-test"


def test_email_features_three_items_per_pillar_in_report_order(configured, tmp_path):
    summary = json.loads(json.dumps(SUMMARY))
    summary["highlights"] = [
        {
            "pillar": pillar,
            "title": f"{pillar} finding {number}",
            "summary": f"{pillar} summary {number}",
            "url": f"https://example.test/{pillar.lower()}/{number}",
        }
        for pillar in ("A", "B")
        for number in range(1, 5)
    ]
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")

    parsed = BytesParser(policy=policy.default).parsebytes(prepare_messages(summary, pdf, configured)[0][1])
    plain = parsed.get_body(preferencelist=("plain",)).get_content()
    html = parsed.get_body(preferencelist=("html",)).get_content()

    for pillar in ("A", "B"):
        for number in range(1, 4):
            assert f"{pillar} finding {number}" in plain
            assert f"{pillar} finding {number}" in html
        assert f"{pillar} finding 4" not in plain
        assert f"{pillar} finding 4" not in html
    assert "The attached PDF contains all 8 report highlights." in plain


@pytest.mark.parametrize("from_name", ["", "   ", "Bad\nName", "Bad\rName", "Bad\x00Name"])
def test_sender_display_name_must_be_nonempty_and_header_safe(monkeypatch, tmp_path, from_name):
    for key, value in {
        "TEST_SMTP_HOST": "smtp.example.test",
        "TEST_SMTP_PORT": "587",
        "TEST_SMTP_USER": "sender-user",
        "TEST_SMTP_PASSWORD": "not-a-real-password",
        "TEST_FROM_ADDRESS": "sender@example.test",
    }.items():
        monkeypatch.setenv(key, value)
    path = config_file(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "from_name: IAA Weekly Climate Newsletter",
        f"from_name: {json.dumps(from_name)}",
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(InputError, match="from_name"):
        load_delivery_config(path)


def test_mime_has_utc_date_and_payload_bound_stable_unique_address_free_message_ids(configured, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    now = datetime(2026, 8, 12, 3, 4, 5, tzinfo=timezone.utc)

    first = prepare_messages(SUMMARY, pdf, configured, clock=lambda: now)
    second = prepare_messages(SUMMARY, pdf, configured, clock=lambda: now.replace(hour=4))
    parsed_first = [BytesParser(policy=policy.default).parsebytes(raw) for _recipient_id, raw in first]
    parsed_second = [BytesParser(policy=policy.default).parsebytes(raw) for _recipient_id, raw in second]

    message_ids = [str(message["Message-ID"]) for message in parsed_first]
    assert len(set(message_ids)) == len(configured.recipients)
    assert message_ids == [str(message["Message-ID"]) for message in parsed_second]
    for recipient, message in zip(configured.recipients, parsed_first):
        parsed_date = parsedate_to_datetime(str(message["Date"]))
        assert parsed_date == now
        assert parsed_date.tzinfo == timezone.utc
        assert re.fullmatch(
            rf"<climate-delivery\.{'a' * 64}\.{recipient.id}\.[0-9a-f]{{24}}@climate\.aiinforsearch\.com>",
            str(message["Message-ID"]),
        )
        assert recipient.address not in str(message["Message-ID"])


def test_message_id_changes_when_rendered_payload_or_envelope_changes(configured, tmp_path, monkeypatch):
    import climate_delivery.delivery as delivery_module

    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")

    def first_id(summary=SUMMARY, config=configured):
        raw = prepare_messages(summary, pdf, config)[0][1]
        return str(BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"])

    baseline = first_id()

    changed_summary = json.loads(json.dumps(SUMMARY))
    changed_summary["executive_summary"].append("New evidence")
    assert first_id(changed_summary) != baseline

    pdf.write_bytes(b"%PDF-changed")
    assert first_id() != baseline

    pdf.write_bytes(b"%PDF-test")

    changed_sender = replace(configured, smtp=replace(configured.smtp, from_name="Changed sender"))
    assert first_id(config=changed_sender) != baseline

    changed_recipient = replace(
        configured,
        recipients=(replace(configured.recipients[0], address="changed@example.test"), *configured.recipients[1:]),
    )
    assert first_id(config=changed_recipient) != baseline

    original_html = delivery_module._html_body
    monkeypatch.setattr(delivery_module, "_html_body", lambda summary: original_html(summary) + "<!-- template-v2 -->")
    assert first_id() != baseline


@pytest.mark.parametrize("dry_run", [False, True])
def test_deliver_renders_message_material_once(configured, tmp_path, monkeypatch, dry_run):
    import climate_delivery.delivery as delivery_module

    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    counts = {"plain": 0, "html": 0}
    original_plain = delivery_module._plain_body
    original_html = delivery_module._html_body

    def counted_plain(summary):
        counts["plain"] += 1
        return original_plain(summary)

    def counted_html(summary):
        counts["html"] += 1
        return original_html(summary)

    monkeypatch.setattr(delivery_module, "_plain_body", counted_plain)
    monkeypatch.setattr(delivery_module, "_html_body", counted_html)

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            return {}

    deliver(
        SUMMARY,
        pdf,
        configured,
        tmp_path / "state",
        dry_run=dry_run,
        smtp_factory=SMTP if not dry_run else lambda *args, **kwargs: pytest.fail("SMTP used in dry-run"),
    )
    assert counts == {"plain": 1, "html": 1}


@pytest.mark.parametrize(
    "original_links",
    [None, [], ["ftp://example.test/source"], ["https://example.test/source", "mailto:test@example.test"], [123]],
)
def test_external_summary_requires_nonempty_http_original_links(configured, tmp_path, original_links):
    summary = json.loads(json.dumps(SUMMARY))
    if original_links is None:
        summary.pop("original_links")
    else:
        summary["original_links"] = original_links
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    with pytest.raises(InputError, match="original_links"):
        prepare_messages(summary, pdf, configured)


def test_config_rejects_unsupported_keys_and_duplicate_recipient_addresses(monkeypatch, tmp_path):
    path = config_file(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("address: alpha@example.test", "address: alpha@example.test\n    label: forbidden"), encoding="utf-8")
    with pytest.raises(InputError, match="keys"):
        load_delivery_config(path)

    path = config_file(tmp_path)
    for key, value in {
        "TEST_SMTP_HOST": "smtp.example.test",
        "TEST_SMTP_PORT": "587",
        "TEST_SMTP_USER": "user",
        "TEST_SMTP_PASSWORD": "secret",
        "TEST_FROM_ADDRESS": "sender@example.test",
    }.items():
        monkeypatch.setenv(key, value)
    path.write_text(path.read_text(encoding="utf-8").replace("beta@example.test", "alpha@example.test"), encoding="utf-8")
    with pytest.raises(InputError, match="unique"):
        load_delivery_config(path)


def test_dry_run_builds_mime_without_instantiating_smtp_or_changing_state(configured, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    state_dir = tmp_path / "state"

    result = deliver(SUMMARY, pdf, configured, state_dir, dry_run=True, smtp_factory=lambda *a, **k: pytest.fail("SMTP used"))

    assert result["status"] == "dry-run"
    assert result["messages"] == 4
    assert not list(state_dir.glob("*.json"))
    assert not list(state_dir.rglob("*.lock"))


def test_dry_run_honors_existing_lock_even_without_delivery_state(configured, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    state_dir = tmp_path / "state"
    locks = state_dir / "locks"
    locks.mkdir(parents=True)
    (locks / f"{'a' * 64}.lock").write_text("crashed process", encoding="ascii")

    with pytest.raises(LockStateError, match="locked"):
        deliver(
            SUMMARY,
            pdf,
            configured,
            state_dir,
            dry_run=True,
            smtp_factory=lambda *args, **kwargs: pytest.fail("SMTP instantiated after lock conflict"),
        )
    assert not (state_dir / f"{'a' * 64}.json").exists()


def test_default_smtp_ssl_uses_verifying_context(monkeypatch, configured, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    ssl_config = replace(configured, smtp=replace(configured.smtp, security="ssl"), recipients=(configured.recipients[0],))
    contexts = []

    class SMTPSSL:
        def __init__(self, host, port, *, timeout, context):
            contexts.append(context)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, username, password):
            return None

        def send_message(self, message):
            return {}

    monkeypatch.setattr("climate_delivery.delivery.smtplib.SMTP_SSL", SMTPSSL)
    result = deliver(SUMMARY, pdf, ssl_config, tmp_path / "state")
    assert result["status"] == "sent"
    assert len(contexts) == 1
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname is True


def test_delivery_records_sending_before_smtp_and_retry_skips_sent(configured, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    state_dir = tmp_path / "state"
    attempts = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            self.current = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            recipient = str(message["To"])
            state = json.loads(next(state_dir.glob("*.json")).read_text(encoding="utf-8"))
            recipient_id = recipient.split("@", 1)[0]
            assert state["recipients"][recipient_id]["status"] == "sending"
            attempts.append(recipient_id)
            if recipient_id == "gamma" and attempts.count("gamma") == 1:
                raise smtplib.SMTPRecipientsRefused({recipient: (550, b"rejected")})

    with pytest.raises(DeliveryError, match="gamma"):
        deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=SMTP)

    first = json.loads(next(state_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert [first["recipients"][key]["status"] for key in ("alpha", "beta", "gamma", "delta")] == [
        "sent",
        "sent",
        "failed",
        "pending",
    ]

    result = deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=SMTP)
    assert result["status"] == "sent"
    assert attempts == ["alpha", "beta", "gamma", "gamma", "delta"]

    again = deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=lambda *a, **k: pytest.fail("SMTP used"))
    assert again["status"] == "already-sent"

    pdf.unlink()
    with pytest.raises(LockStateError, match="artifact"):
        deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=lambda *a, **k: pytest.fail("SMTP used"))


def test_returned_recipient_refusal_is_a_known_failure(configured, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    state_dir = tmp_path / "state"

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            return {str(message["To"]): (550, b"rejected")}

    with pytest.raises(DeliveryError):
        deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=SMTP)
    state = json.loads(next(state_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert state["recipients"]["alpha"]["status"] == "failed"


@pytest.mark.parametrize("mutation", ["summary", "summary-bytes", "pdf", "address", "from-name", "html-template"])
def test_partial_delivery_state_binds_artifacts_recipients_and_message_payload(
    configured, tmp_path, monkeypatch, mutation
):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    state_dir = tmp_path / "state"

    class PartialSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            if str(message["To"]).startswith("gamma@"):
                raise smtplib.SMTPRecipientsRefused({str(message["To"]): (550, b"rejected")})
            return {}

    with pytest.raises(DeliveryError):
        deliver(
            SUMMARY,
            pdf,
            configured,
            state_dir,
            smtp_factory=PartialSMTP,
            summary_artifact_sha256="b" * 64 if mutation == "summary-bytes" else None,
        )

    changed_summary = json.loads(json.dumps(SUMMARY))
    changed_config = configured
    if mutation == "summary":
        changed_summary["executive_summary"].append("changed")
    elif mutation == "pdf":
        pdf.write_bytes(b"%PDF-changed")
    elif mutation == "address":
        recipients = (replace(configured.recipients[0], address="changed@example.test"), *configured.recipients[1:])
        changed_config = replace(configured, recipients=recipients)
    elif mutation == "from-name":
        changed_config = replace(configured, smtp=replace(configured.smtp, from_name="Changed sender"))
    elif mutation == "html-template":
        import climate_delivery.delivery as delivery_module

        original_html = delivery_module._html_body
        monkeypatch.setattr(delivery_module, "_html_body", lambda summary: original_html(summary) + "<!-- changed -->")

    with pytest.raises(LockStateError, match="artifact|recipient|payload|state"):
        deliver(
            changed_summary,
            pdf,
            changed_config,
            state_dir,
            dry_run=mutation == "address",
            smtp_factory=lambda *args, **kwargs: pytest.fail("SMTP used after state binding changed"),
            summary_artifact_sha256="c" * 64 if mutation == "summary-bytes" else None,
        )

    state = json.loads(next(state_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert state["schema_version"] == 2
    assert set(state["artifacts"]) == {"summary_sha256", "pdf_sha256"}
    assert all(len(value["address_fingerprint"]) == 64 for value in state["recipients"].values())
    assert all(len(value["message_fingerprint"]) == 64 for value in state["recipients"].values())
    assert "example.test" not in json.dumps(state)


def test_legacy_state_without_payload_binding_fails_closed(configured, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy = {
        "schema_version": 1,
        "report_sha256": "a" * 64,
        "artifacts": {
            "summary_sha256": hashlib.sha256(
                json.dumps(SUMMARY, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
            ).hexdigest(),
            "pdf_sha256": hashlib.sha256(b"%PDF-test").hexdigest(),
        },
        "recipients": {
            recipient.id: {
                "address_fingerprint": hashlib.sha256(recipient.address.casefold().encode()).hexdigest(),
                "status": "pending",
            }
            for recipient in configured.recipients
        },
    }
    (state_dir / f"{'a' * 64}.json").write_text(json.dumps(legacy), encoding="utf-8")

    with pytest.raises(LockStateError, match="legacy.*schema.*manual reconciliation"):
        deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=lambda *args, **kwargs: pytest.fail("SMTP used"))


def test_ambiguous_smtp_failure_is_recorded_unknown_and_fails_closed(configured, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    state_dir = tmp_path / "state"

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            raise smtplib.SMTPServerDisconnected("outcome unknown")

    with pytest.raises(LockStateError, match="alpha|manual reconciliation"):
        deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=SMTP)
    state = json.loads(next(state_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert state["recipients"]["alpha"]["status"] == "unknown"

    with pytest.raises(LockStateError, match="manual reconciliation"):
        deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=lambda *a, **k: pytest.fail("SMTP used"))


def test_state_persistence_failure_before_smtp_is_lock_state_and_sends_nothing(configured, tmp_path, monkeypatch):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    calls = []
    monkeypatch.setattr(
        "climate_delivery.delivery.atomic_write_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(LockStateError, match="persist sending") as raised:
        deliver(SUMMARY, pdf, configured, tmp_path / "state", smtp_factory=lambda *args, **kwargs: calls.append(1))
    assert calls == []
    assert isinstance(raised.value.__cause__, OSError)


@pytest.mark.parametrize("failure_kind", ["known", "unknown"])
def test_failure_state_persistence_error_is_lock_state(configured, tmp_path, monkeypatch, failure_kind):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    real_write = __import__("climate_delivery.delivery", fromlist=["atomic_write_json"]).atomic_write_json
    writes = []

    def fail_second_write(path, value):
        writes.append(json.loads(json.dumps(value)))
        if len(writes) == 2:
            raise OSError("fsync failed")
        return real_write(path, value)

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            if failure_kind == "known":
                raise smtplib.SMTPRecipientsRefused({str(message["To"]): (550, b"rejected")})
            raise smtplib.SMTPServerDisconnected("unknown outcome")

    monkeypatch.setattr("climate_delivery.delivery.atomic_write_json", fail_second_write)
    with pytest.raises(LockStateError, match="persist failed|persist unknown") as raised:
        deliver(SUMMARY, pdf, configured, tmp_path / "state", smtp_factory=SMTP)
    assert isinstance(raised.value.__cause__, OSError)


def test_sent_state_persistence_failure_is_lock_state_for_manual_reconciliation(configured, tmp_path, monkeypatch):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    real_write = __import__("climate_delivery.delivery", fromlist=["atomic_write_json"]).atomic_write_json
    writes = 0
    sends = 0

    def fail_sent_write(path, value):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("post-replace directory fsync failed")
        return real_write(path, value)

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            nonlocal sends
            sends += 1
            return {}

    monkeypatch.setattr("climate_delivery.delivery.atomic_write_json", fail_sent_write)
    with pytest.raises(LockStateError, match="persist sent") as raised:
        deliver(SUMMARY, pdf, configured, tmp_path / "state", smtp_factory=SMTP)
    assert sends == 1
    assert isinstance(raised.value.__cause__, OSError)


@pytest.mark.parametrize("blocked", ["sending", "unknown"])
def test_ambiguous_delivery_state_fails_closed(configured, tmp_path, blocked):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    summary_bytes = json.dumps(SUMMARY, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    state = {
        "schema_version": 1,
        "report_sha256": "a" * 64,
        "artifacts": {
            "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
            "pdf_sha256": hashlib.sha256(b"%PDF-test").hexdigest(),
        },
        "recipients": {
            recipient.id: {
                "address_fingerprint": hashlib.sha256(recipient.address.casefold().encode()).hexdigest(),
                "status": blocked if recipient.id == "alpha" else "pending",
            }
            for recipient in configured.recipients
        },
    }
    (state_dir / f"{'a' * 64}.json").write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(LockStateError, match="manual reconciliation"):
        deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=lambda *a, **k: pytest.fail("SMTP used"))


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps({"schema_version": 1, "report_sha256": "a" * 64, "recipients": ["alpha", "beta", "gamma", "delta"]}),
        json.dumps({"schema_version": 2, "report_sha256": "a" * 64, "recipients": {}}),
    ],
)
def test_corrupt_delivery_state_fails_closed(configured, tmp_path, payload):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / f"{'a' * 64}.json").write_text(payload, encoding="utf-8")

    with pytest.raises(LockStateError, match="manual reconciliation"):
        deliver(SUMMARY, pdf, configured, state_dir, smtp_factory=lambda *a, **k: pytest.fail("SMTP used"))
