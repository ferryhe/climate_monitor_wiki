"""Pure validation and subset checks for Request budgets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields

from web_listening.request.model import Budgets, RequestValidationError

BUDGET_FIELDS = tuple(field.name for field in fields(Budgets))


def validate_budgets(value: Budgets) -> Budgets:
    """Return a valid immutable budget or reject it without coercion."""
    if not isinstance(value, Budgets):
        raise RequestValidationError("budget.invalid")
    for name in BUDGET_FIELDS:
        limit = getattr(value, name)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise RequestValidationError("budget.invalid")
    return value


def budgets_from_mapping(value: object) -> Budgets:
    """Build strict budgets from one JSON-compatible object."""
    if not isinstance(value, Mapping):
        raise RequestValidationError("budgets.invalid")
    keys = set(value)
    if not all(isinstance(key, str) for key in keys):
        raise RequestValidationError("budgets.unknown_field")
    if keys - set(BUDGET_FIELDS):
        raise RequestValidationError("budgets.unknown_field")
    if set(BUDGET_FIELDS) - keys:
        raise RequestValidationError("budgets.missing")
    return validate_budgets(Budgets(**{name: value[name] for name in BUDGET_FIELDS}))


def budgets_are_subset(candidate: Budgets, original: Budgets) -> bool:
    """Return whether every candidate limit preserves or narrows the original."""
    return all(
        getattr(candidate, name) <= getattr(original, name) for name in BUDGET_FIELDS
    )
