"""Deterministic display names for known institutions and source publishers."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml


_DISPLAY_OVERRIDES = {
    "world-bank": "World Bank Group (WBG)",
    "unctad": "UN Trade and Development (UNCTAD)",
}

_SUPPLEMENTAL_HOSTS = (
    ("actuaries.org", "International Actuarial Association (IAA)"),
    ("actuaries.org.nz", "New Zealand Society of Actuaries (NZSA)"),
    ("actuaries.org.uk", "Institute and Faculty of Actuaries (IFoA)"),
    ("actuary.org", "American Academy of Actuaries"),
    ("actuary.info", "The Daily Actuary"),
    ("actuariesclimateindex.org", "Actuaries Climate Index (ACI)"),
    ("adaptation-fund.org", "Adaptation Fund (AF)"),
    ("adaptation-undp.org", "United Nations Development Programme (UNDP) — Climate Change Adaptation"),
    ("ajg.com", "Arthur J. Gallagher & Co. (Gallagher)"),
    ("aon.com", "Aon"),
    ("axa.com.hk", "AXA Hong Kong and Macau (AXA)"),
    ("blog.alliedoffsets.com", "AlliedOffsets — Blog"),
    ("blogs.afdb.org", "African Development Bank Group (AFDB) — Blogs"),
    ("blogs.worldbank.org", "World Bank Group (WBG) — Blogs"),
    ("briefglance.com", "BriefGlance.com"),
    ("casact.org", "Casualty Actuarial Society (CAS)"),
    ("cbd.int", "Secretariat of the Convention on Biological Diversity (CBD)"),
    ("cdp.net", "CDP"),
    ("ceres.org", "Ceres"),
    ("cetex.org", "Centre for Economic Transition Expertise (CETEx)"),
    ("cia-ica.ca", "Canadian Institute of Actuaries (CIA/ICA)"),
    ("climate.copernicus.eu", "Copernicus Climate Change Service (C3S)"),
    ("climatepolicyinitiative.org", "Climate Policy Initiative (CPI)"),
    ("climatepromise.undp.org", "United Nations Development Programme (UNDP) — Climate Promise"),
    ("content.naic.org", "National Association of Insurance Commissioners (NAIC)"),
    ("dawgen.global", "Dawgen Global"),
    ("deloitte.com", "Deloitte"),
    ("desapublications.un.org", "United Nations Department of Economic and Social Affairs (UN DESA)"),
    ("descartesunderwriting.com", "Descartes Underwriting"),
    ("documents.worldbank.org", "World Bank Group (WBG) — Documents & Reports"),
    ("earthdata.nasa.gov", "NASA Earthdata"),
    ("eiopa.europa.eu", "European Insurance and Occupational Pensions Authority (EIOPA)"),
    ("electrictruckworld.com", "Electric Truck World"),
    ("energy.gov", "U.S. Department of Energy (DOE)"),
    ("ey.com", "EY"),
    ("forbes.com", "Forbes"),
    ("fsb-tcfd.org", "Task Force on Climate-Related Financial Disclosures (TCFD)"),
    ("genevaassociation.org", "The Geneva Association"),
    ("globalbankingandfinance.com", "Global Banking & Finance Review (GBFR)"),
    ("globalpolicyjournal.com", "Global Policy"),
    ("indonesia.un.org", "United Nations in Indonesia"),
    ("industryresearch.biz", "Industry Research"),
    ("informedclearly.com", "Informed, Clearly"),
    ("inrisklabs.com", "InRisk"),
    ("insurance.ca.gov", "California Department of Insurance (CDI)"),
    ("insurancebusinessmag.com", "Insurance Business"),
    ("insuranceerm.com", "InsuranceERM"),
    ("insurancejournal.com", "Insurance Journal"),
    ("iri.columbia.edu", "International Research Institute for Climate and Society (IRI)"),
    ("iso.org", "ISO (International Organization for Standardization)"),
    ("lse.ac.uk", "London School of Economics and Political Science (LSE)"),
    ("mapfre.com", "Mapfre"),
    ("mcdermottlaw.com", "McDermott Will & Schulte"),
    ("munichre.com", "Munich Re"),
    ("ncelenviro.org", "National Caucus of Environmental Legislators (NCEL)"),
    ("news.climate.columbia.edu", "State of the Planet (Columbia Climate School)"),
    ("osfi-bsif.gc.ca", "Office of the Superintendent of Financial Institutions (OSFI)"),
    ("openknowledge.worldbank.org", "World Bank Open Knowledge Repository (OKR)"),
    ("orfonline.org", "Observer Research Foundation (ORF)"),
    ("papers.ssrn.com", "SSRN"),
    ("preventionweb.net", "PreventionWeb"),
    ("reinsurancene.ws", "Reinsurance News"),
    ("reuters.com", "Reuters"),
    ("research.reading.ac.uk", "University of Reading"),
    ("riskpublishing.com", "Risk Publishing"),
    ("sdgfinance.undp.org", "UNDP Sustainable Finance Hub"),
    ("searca.org", "Southeast Asian Regional Center for Graduate Study and Research in Agriculture (SEARCA)"),
    ("soa.org", "Society of Actuaries (SOA)"),
    ("southernclimate.org", "Southern Climate Impacts Planning Program (SCIPP)"),
    ("spglobal.com", "S&P Global"),
    ("swissre.com", "Swiss Re"),
    ("understandrisk.org", "Understanding Risk (UR)"),
    ("un.org", "United Nations (UN)"),
    ("unepfi.org", "United Nations Environment Programme Finance Initiative (UNEP FI)"),
    ("viewpoint.pwc.com", "PwC Viewpoint"),
    ("undrr.org", "United Nations Office for Disaster Risk Reduction (UNDRR)"),
    ("ifrs.org", "IFRS Foundation (IFRS)"),
    ("bis.org", "Bank for International Settlements (BIS)"),
    ("artemis.bm", "Artemis"),
    ("alliedoffsets.com", "AlliedOffsets"),
    ("fao.org", "Food and Agriculture Organization of the United Nations (FAO)"),
)

_SUPPLEMENTAL_PATHS = (
    ("bis.org", "/bcbs", "The Basel Committee on Banking Supervision (BCBS)"),
    ("bis.org", "/committees/bcbs", "The Basel Committee on Banking Supervision (BCBS)"),
    ("bis.org", "/publ/bcbs", "The Basel Committee on Banking Supervision (BCBS)"),
    ("ifrs.org", "/groups/international-sustainability-standards-board", "International Sustainability Standards Board (ISSB)"),
    ("ifrs.org", "/issued-standards/ifrs-sustainability-standards-navigator", "International Sustainability Standards Board (ISSB)"),
    ("ifrs.org", "/content/ifrs/home/issued-standards/ifrs-sustainability-standards-navigator", "International Sustainability Standards Board (ISSB)"),
    ("ifrs.org", "/supporting-implementation/supporting-materials-for-ifrs-sustainability-disclosure-standards", "International Sustainability Standards Board (ISSB)"),
    ("osfi-bsif.gc.ca", "/en/oca", "Office of the Chief Actuary (OCA)"),
    ("research.reading.ac.uk", "/climate-and-finance", "Climate and Finance Research Cluster (University of Reading)"),
)

_SUPPLEMENTAL_ALIASES = {
    "International Actuarial Association": "International Actuarial Association (IAA)",
    "Actuaries Climate Index": "Actuaries Climate Index (ACI)",
    "American Academy of Actuaries": "American Academy of Actuaries",
    "American Academy of Actuaries (AAA)": "American Academy of Actuaries",
    "AAA": "American Academy of Actuaries",
    "Canadian Institute of Actuaries": "Canadian Institute of Actuaries (CIA/ICA)",
    "Canadian Institute of Actuaries (CIA)": "Canadian Institute of Actuaries (CIA/ICA)",
    "CIA/ICA": "Canadian Institute of Actuaries (CIA/ICA)",
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
    "MAPFRE": "Mapfre",
}


def _host(value: str) -> str:
    host = (urlsplit(value if "://" in value else "https://" + value).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _path_prefix_matches(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    if not prefix or path == prefix or path.startswith((prefix + "/", prefix + ".")):
        return True
    # BIS publishes BCBS papers with the committee key followed directly by a
    # numeric document id, e.g. /publ/bcbs239.pdf.
    return prefix == "/publ/bcbs" and path.startswith(prefix) and path[len(prefix):len(prefix) + 1].isdigit()


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
    """Resolve a publisher from governed URL ownership, then exact aliases.

    Known source URL ownership takes precedence over an inherited display label.
    Unknown URLs use their full hostname; article titles are never inputs.
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
    if urls:
        host = _host(urls[0])
        if host:
            return host

    original = str(value or "").strip()
    if original:
        normalized_host = _host(original)
        if "." in normalized_host:
            mapped = publisher_name_from_url(original, rules)
            if mapped:
                return mapped
            return normalized_host
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
