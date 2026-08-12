import argparse
import json
import re
from pathlib import Path
from typing import Sequence

from .config import load_delivery_config
from .delivery import deliver, load_summary, load_summary_with_sha256
from .errors import DeliveryError, GenerationError, InputError, LockStateError
from .pdf import render_pdf
from .pipeline import run_delivery
from .paths import external_directory_root, external_file_path, require_distinct_files, validate_run_paths
from .report import parse_weekly_report
from .summary import build_summary, write_summary


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError(message)


def _parser() -> Parser:
    parser = Parser(prog="climate-delivery")
    subcommands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    summarize = subcommands.add_parser("summarize")
    summarize.add_argument("--report", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)

    pdf = subcommands.add_parser("render-pdf")
    pdf.add_argument("--summary", type=Path, required=True)
    pdf.add_argument("--output", type=Path, required=True)

    email = subcommands.add_parser("send-email")
    email.add_argument("--summary", type=Path, required=True)
    email.add_argument("--pdf", type=Path, required=True)
    email.add_argument("--config", type=Path, required=True)
    email.add_argument("--state-dir", type=Path, required=True)
    email.add_argument("--dry-run", action="store_true")

    run = subcommands.add_parser("run")
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--dry-run", action="store_true")
    return parser


def _redact(message: str) -> str:
    return re.sub(r"[^\s@]+@[^\s@]+", "[redacted-email]", message)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "summarize":
            report_path = external_file_path(args.report, "report")
            output_path = external_file_path(args.output, "output")
            require_distinct_files(report_path, output_path, "report", "output")
            report = parse_weekly_report(report_path)
            write_summary(build_summary(report), output_path)
            result = {"status": "success", "report_date": report.report_date, "report_sha256": report.sha256}
        elif args.command == "render-pdf":
            summary_path = external_file_path(args.summary, "summary")
            output_path = external_file_path(args.output, "output")
            require_distinct_files(summary_path, output_path, "summary", "output")
            summary = load_summary(summary_path)
            render_pdf(summary, output_path)
            result = {
                "status": "success",
                "report_date": summary["report"]["date"],
                "report_sha256": summary["report"]["sha256"],
            }
        elif args.command == "send-email":
            summary_path = external_file_path(args.summary, "summary")
            pdf_path = external_file_path(args.pdf, "pdf")
            config_path = external_file_path(args.config, "config")
            state_dir = external_directory_root(args.state_dir, "state-dir")
            require_distinct_files(summary_path, pdf_path, "summary", "pdf")
            summary, summary_sha256 = load_summary_with_sha256(summary_path)
            result = deliver(
                summary,
                pdf_path,
                load_delivery_config(config_path),
                state_dir,
                dry_run=args.dry_run,
                summary_artifact_sha256=summary_sha256,
            )
        else:
            report_path, output_dir, state_dir, config_path = validate_run_paths(
                args.report,
                args.output_dir,
                args.state_dir,
                args.config,
            )
            result = run_delivery(
                report_path,
                output_dir,
                state_dir,
                config_path,
                dry_run=args.dry_run,
            )
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except InputError as exc:
        payload, code = {"status": "error", "kind": "input", "message": _redact(str(exc))}, 2
    except GenerationError as exc:
        payload, code = {"status": "error", "kind": "generation", "message": _redact(str(exc))}, 3
    except DeliveryError as exc:
        payload, code = {"status": "error", "kind": "delivery", "message": _redact(str(exc))}, 4
    except LockStateError as exc:
        payload, code = {"status": "error", "kind": "lock-state", "message": _redact(str(exc))}, 5
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
