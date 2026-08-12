from pathlib import Path

from .errors import InputError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def external_absolute_path(path: Path, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise InputError(f"{label} path must be absolute")
    try:
        resolved = value.resolve(strict=False)
    except OSError as exc:
        raise InputError(f"{label} path could not be resolved") from exc
    if resolved == REPOSITORY_ROOT or resolved.is_relative_to(REPOSITORY_ROOT):
        raise InputError(f"{label} path must be outside the application repository")
    return resolved


def external_file_path(path: Path, label: str) -> Path:
    resolved = external_absolute_path(path, label)
    if resolved.exists() and not resolved.is_file():
        raise InputError(f"{label} path must be a file, not a directory")
    return resolved


def external_directory_root(path: Path, label: str) -> Path:
    resolved = external_absolute_path(path, label)
    if resolved.exists() and not resolved.is_dir():
        raise InputError(f"{label} must be a directory root, not a file")
    return resolved


def require_distinct_files(first: Path, second: Path, first_label: str, second_label: str) -> None:
    if first == second:
        raise InputError(f"{first_label} and {second_label} paths must be separate")


def require_separate_trees(first: Path, second: Path, first_label: str, second_label: str) -> None:
    if first == second or first.is_relative_to(second) or second.is_relative_to(first):
        raise InputError(f"{first_label} and {second_label} must be separate, non-nested directories")


def validate_run_paths(
    report_path: Path,
    output_dir: Path,
    state_dir: Path,
    config_path: Path,
) -> tuple[Path, Path, Path, Path]:
    report = external_file_path(report_path, "report")
    output = external_directory_root(output_dir, "output-dir")
    state = external_directory_root(state_dir, "state-dir")
    config = external_file_path(config_path, "config")
    require_separate_trees(output, state, "output-dir", "state-dir")
    return report, output, state, config
