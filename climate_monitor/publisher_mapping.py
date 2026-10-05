"""Deterministic display names for known institutions and source publishers."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml


_DISPLAY_OVERRIDES = {
    "world-bank": "World Bank Group (WBG)",
}

_SUPPLEMENTAL_HOSTS = (
    ("actuaries.org", "International Actuarial Association (IAA)"),
    ("actuary.org", "American Academy of Actuaries (AAA)"),
    ("actuariesclimateindex.org", "Actuaries Climate Index (ACI)"),
    ("aon.com", "Aon"),
    ("ceres.org", "Ceres"),
    ("cia-ica.ca", "Canadian Institute of Actuaries (CIA)"),
    ("climate.copernicus.eu", "Copernicus Climate Change Service (C3S)"),
    ("earthdata.nasa.gov", "NASA Earthdata"),
    ("insurance.ca.gov", "California Department of Insurance (CDI)"),
    ("mapfre.com", "MAPFRE"),
    ("munichre.com", "Munich Re"),
    ("osfi-bsif.gc.ca", "Office of the Superintendent of Financial Institutions (OSFI)"),
    ("research.reading.ac.uk", "University of Reading"),
    ("soa.org", "Society of Actuaries (SOA)"),
    ("swissre.com", "Swiss Re"),
    ("undrr.org", "United Nations Office for Disaster Risk Reduction (UNDRR)"),
    ("unepfi.org", "United Nations Environment Programme Finance Initiative (UNEP FI)"),
    ("ifrs.org", "IFRS Foundation (IFRS)"),
    ("bis.org", "Bank for International Settlements (BIS)"),
    ("artemis.bm", "Artemis"),
    ("alliedoffsets.com", "AlliedOffsets"),
    ("fao.org", "Food and Agriculture Organization of the United Nations (FAO)"),
)

_SUPPLEMENTAL_PATHS = (
    ("ifrs.org", "/groups/international-sustainability-standards-board", "International Sustainability Standards Board (ISSB)"),
    ("ifrs.org", "/issued-standards/ifrs-sustainability-standards-navigator", "International Sustainability Standards Board (ISSB)"),
    ("ifrs.org", "/content/ifrs/home/issued-standards/ifrs-sustainability-standards-navigator", "International Sustainability Standards Board (ISSB)"),
)

_SUPPLEMENTAL_ALIASES = {
    "International Actuarial Association": "International Actuarial Association (IAA)",
    "Actuaries Climate Index": "Actuaries Climate Index (ACI)",
    "American Academy of Actuaries": "American Academy of Actuaries (AAA)",
    "American Academy of Actuaries (AAA)": "American Academy of Actuaries (AAA)",
    "Canadian Institute of Actuaries": "Canadian Institute of Actuaries (CIA)",
    "Copernicus Climate Change Service": "Copernicus Climate Change Service (C3S)",
    "California Department of Insurance": "California Department of Insurance (CDI)",
    "Office of the Superintendent of Financial Institutions": "Office of the Superintendent of Financial Institutions (OSFI)",
    "Society of Actuaries": "Society of Actuaries (SOA)",
    "United Nations Office for Disaster Risk Reduction": "United Nations Office for Disaster Risk Reduction (UNDRR)",
    "IFRS": "IFRS Foundation (IFRS)",
    "IFRS Foundation": "IFRS Foundation (IFRS)",
    "ISSB": "International Sustainability Standards Board (ISSB)",
    "WBG": "World Bank Group (WBG)",
    "World Bank Group (WBG)": "World Bank Group (WBG)",
    "UNEP FI": "United Nations Environment Programme Finance Initiative (UNEP FI)",
    "Artemis": "Artemis",
    "Artemis (cat bond news)": "Artemis",
    "AlliedOffsets": "AlliedOffsets",
}


def _host(value: str) -> str:
    host = (urlsplit(value if "://" in value else "https://" + value).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _path_prefix_matches(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return not prefix or path == prefix or path.startswith((prefix + "/", prefix + "."))


def _alias_key(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold(), flags=re.UNICODE).split())


@lru_cache(maxsize=1)
def _mapping_data():
    from climate_monitor.config import load_sources

    config_path = Path(__file__).resolve().parents[1] / "monitoring" / "supranational_sources.yaml"
    sources = load_sources(config_path) if config_path.is_file() else []
    aliases: dict[str, str] = {}
    host_names: dict[str, set[str]] = {}
    host_paths: dict[str, list[str]] = {}
    rules: list[tuple[str, str, str]] = []

    for source in sources:
        label = _DISPLAY_OVERRIDES.get(source.key)
        if not label:
            abbreviation = source.abbreviation.strip()
            label = source.full_name
            if abbreviation and abbreviation.casefold() not in label.casefold():
                label = f"{label} ({abbreviation})"
        host = _host(source.url)
        path = urlsplit(source.url).path or "/"
        rules.append((host, path, label))
        host_names.setdefault(host, set()).add(label)
        host_paths.setdefault(host, []).append(path)
        for alias in (source.key, source.abbreviation, source.full_name, label):
            if alias:
                aliases[_alias_key(alias)] = label

    if config_path.is_file():
        source_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        for source in source_config.get("missing_url_notes", []):
            abbreviation = str(source.get("abbreviation", "")).strip()
            full_name = str(source.get("full_name", "")).strip()
            label = f"{full_name} ({abbreviation})" if abbreviation and abbreviation.casefold() not in full_name.casefold() else full_name
            for alias in (abbreviation, full_name, label):
                if alias:
                    aliases[_alias_key(alias)] = label

    # A configured institution owns its host when that host has only one
    # configured institution. Shared domains need an explicit default below.
    shared_hosts = {"ifrs.org", "bis.org", "unepfi.org"}
    for host, labels in host_names.items():
        has_parent_source = any(host.endswith("." + parent) for parent in host_names if parent != host)
        dedicated_subdomain = not has_parent_source or any(path == "/" for path in host_paths[host])
        if len(labels) == 1 and host not in shared_hosts and dedicated_subdomain:
            rules.append((host, "/", next(iter(labels))))

    for host, label in _SUPPLEMENTAL_HOSTS:
        rules.append((host, "/", label))
    rules.extend(_SUPPLEMENTAL_PATHS)
    for alias, label in _SUPPLEMENTAL_ALIASES.items():
        aliases[_alias_key(alias)] = label

    rules.sort(key=lambda rule: (len(rule[0]), len(rule[1])), reverse=True)
    return tuple(rules), aliases


def publisher_name(value: str | None, source_urls=()) -> str:
    """Resolve a publisher from governed source identity, then exact aliases.

    Known source URL ownership takes precedence over an inherited display label.
    An unknown publisher remains as supplied; article titles are never inputs.
    """
    rules, aliases = _mapping_data()
    urls = [url for url in source_urls if isinstance(url, str) and url.strip()]
    for url in urls:
        parsed = urlsplit(url if "://" in url else "https://" + url)
        host = _host(url)
        path = parsed.path or "/"
        for rule_host, prefix, label in rules:
            if (host == rule_host or host.endswith("." + rule_host)) and _path_prefix_matches(path, prefix):
                return label

    original = str(value or "").strip()
    if original:
        normalized_host = _host(original)
        if "." in normalized_host:
            mapped = publisher_name_from_url(original, rules)
            if mapped:
                return mapped
        alias = aliases.get(_alias_key(original))
        if alias:
            return alias
        return original
    return "Publisher not recorded"


def publisher_name_from_url(value: str, rules=None) -> str | None:
    """Resolve a single URL or hostname without recursing through aliases."""
    rules = rules if rules is not None else _mapping_data()[0]
    parsed = urlsplit(value if "://" in value else "https://" + value)
    host = _host(value)
    path = parsed.path or "/"
    for rule_host, prefix, label in rules:
        if (host == rule_host or host.endswith("." + rule_host)) and _path_prefix_matches(path, prefix):
            return label
    return None
