from __future__ import annotations

from dataclasses import replace

from .models import CandidateItem, RunConfig

CLIMATE_SIGNAL_TERMS = {
    "physical_risk": ("physical risk", "flood", "wildfire", "heatwave", "natural catastrophe"),
    "transition_risk": ("transition risk", "net zero", "emissions", "decarbonization"),
    "adaptation_resilience": ("adaptation", "resilience"),
    "general_climate": ("climate", "warming"),
}

ACTUARIAL_SIGNAL_TERMS = {
    "insurance_risk": ("insurance", "reinsurance", "underwriting", "pricing"),
    "capital_solvency": ("capital", "solvency", "reserving"),
    "supervision_disclosure": ("supervision", "disclosure"),
    "actuarial_modeling": ("actuarial", "actuary", "catastrophe model", "mortality", "pension"),
}

CATEGORY_LABELS = {
    "physical_risk": "Physical Risk",
    "transition_risk": "Transition Risk",
    "adaptation_resilience": "Adaptation & Resilience",
    "general_climate": "Climate Risk",
    "insurance_risk": "Insurance Risk",
    "capital_solvency": "Capital & Solvency",
    "supervision_disclosure": "Supervision & Disclosure",
    "actuarial_modeling": "Actuarial Modelling",
}


def classify_candidate(item: CandidateItem, config: RunConfig) -> CandidateItem:
    text = " ".join([item.title, item.summary, item.source_name, item.evidence_text]).casefold()
    climate_matches = _matched_terms(text, config.climate_keywords)
    actuarial_matches = _matched_terms(text, config.actuarial_keywords)
    topics = tuple(sorted(set(climate_matches + actuarial_matches)))
    climate_signal = _best_signal(text, CLIMATE_SIGNAL_TERMS) if climate_matches else "none"
    actuarial_signal = _best_signal(text, ACTUARIAL_SIGNAL_TERMS) if actuarial_matches else "none"
    categories = tuple(
        CATEGORY_LABELS[signal]
        for signal in (climate_signal, actuarial_signal)
        if signal in CATEGORY_LABELS
    )
    confidence = _confidence(climate_matches=climate_matches, actuarial_matches=actuarial_matches, evidence_text=item.evidence_text)
    reason_parts: list[str] = []
    if climate_matches:
        reason_parts.append(f"Climate signal `{climate_signal}` from terms: {', '.join(climate_matches)}")
    if actuarial_matches:
        reason_parts.append(f"Actuarial signal `{actuarial_signal}` from terms: {', '.join(actuarial_matches)}")
    return replace(
        item,
        climate_related=bool(climate_matches),
        actuarial_related=bool(actuarial_matches),
        relevance_reason="; ".join(reason_parts),
        climate_signal=climate_signal,
        actuarial_signal=actuarial_signal,
        confidence=confidence,
        evidence_snippet=_snippet(item.evidence_text or " ".join([item.title, item.summary])),
        topics=topics,
        categories=categories,
        keywords=topics,
    )


def _matched_terms(text: str, keywords: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for keyword in keywords:
        term = keyword.strip().casefold()
        if term and term in text and term not in matches:
            matches.append(term)
    return matches


def _best_signal(text: str, signal_terms: dict[str, tuple[str, ...]]) -> str:
    for signal, terms in signal_terms.items():
        if any(term in text for term in terms):
            return signal
    return "general"


def _confidence(*, climate_matches: list[str], actuarial_matches: list[str], evidence_text: str) -> float:
    score = 0.45 if climate_matches else 0.0
    score += min(len(climate_matches), 3) * 0.1
    score += min(len(actuarial_matches), 3) * 0.05
    if evidence_text.strip():
        score += 0.1
    return min(round(score, 2), 0.95)


def _snippet(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:240] + ("..." if len(cleaned) > 240 else "")
