import pytest

from climate_monitor.publisher_mapping import publisher_name
from climate_registry.publication import _pdf_dto, _public_dto
from climate_registry.read_api import _publisher_label


@pytest.mark.parametrize(("url", "expected"), [
    ("https://actuary.org/article/example", "American Academy of Actuaries"),
    ("https://www.bis.org/committees/bcbs/overview", "The Basel Committee on Banking Supervision (BCBS)"),
    ("https://www.bis.org/publ/bcbs239.pdf", "The Basel Committee on Banking Supervision (BCBS)"),
    ("https://www.bis.org/about/index.htm", "Bank for International Settlements (BIS)"),
    ("https://www.ifrs.org/groups/international-sustainability-standards-board/", "International Sustainability Standards Board (ISSB)"),
    ("https://www.ifrs.org/about-us/", "IFRS Foundation (IFRS)"),
    ("https://www.ifrs.org/supporting-implementation/supporting-materials-for-ifrs-sustainability-disclosure-standards/ifrs-s2/example", "International Sustainability Standards Board (ISSB)"),
    ("https://www.unepfi.org/insurance/insurance/", "Principles for Sustainable Insurance (PSI)"),
    ("https://www.unepfi.org/forum-for-insurance-transition-to-net-zero/", "Forum for Insurance Transition to Net Zero (FIT)"),
    ("https://sdgfinance.undp.org/sustainable-insurance-forum", "Sustainable Insurance Forum (SIF)"),
    ("https://sdgfinance.undp.org/about", "UNDP Sustainable Finance Hub"),
    ("https://www.osfi-bsif.gc.ca/en/oca/about-office-chief-actuary", "Office of the Chief Actuary (OCA)"),
    ("https://www.osfi-bsif.gc.ca/en/about-osfi", "Office of the Superintendent of Financial Institutions (OSFI)"),
    ("https://research.reading.ac.uk/climate-and-finance/example", "Climate and Finance Research Cluster (University of Reading)"),
    ("https://research.reading.ac.uk/other-group/example", "University of Reading"),
    ("https://blogs.worldbank.org/en/voices/example", "World Bank Group (WBG) — Blogs"),
    ("https://documents.worldbank.org/en/publication/documents-reports/example", "World Bank Group (WBG) — Documents & Reports"),
    ("https://climatepromise.undp.org/news/example", "United Nations Development Programme (UNDP) — Climate Promise"),
    ("https://blog.alliedoffsets.com/example", "AlliedOffsets — Blog"),
    ("https://unctad.org/about", "UN Trade and Development (UNCTAD)"),
    ("https://www.mapfre.com/en/we-are-mapfre/our-brand/", "Mapfre"),
])
def test_shared_hosts_resolve_article_owner_by_canonical_path(url, expected):
    assert publisher_name("inherited publisher", [url]) == expected


def test_dropdown_host_label_does_not_inherit_shared_path_owner():
    assert _publisher_label("bis.org", "The Basel Committee on Banking Supervision") == \
        "Bank for International Settlements (BIS)"
    assert publisher_name("AAA") == "American Academy of Actuaries"


def test_article_and_pdf_dtos_map_display_name_without_changing_identity_fields():
    core = _public_dto({
        "article_id": "article-bcbs",
        "canonical_url": "https://bis.org/committees/bcbs/overview",
        "source": "bis.org",
        "source_id": "source-bis",
        "publisher": "Bank for International Settlements",
    })
    pdf = _pdf_dto({
        "article_id": "pdf-oca",
        "canonical_url": "https://osfi-bsif.gc.ca/en/oca/about-office-chief-actuary",
        "source": "osfi-bsif.gc.ca",
        "publisher": "osfi-bsif.gc.ca",
    })

    assert (core["publisher"], core["source"], core["source_id"]) == (
        "The Basel Committee on Banking Supervision (BCBS)", "bis.org", "source-bis")
    assert (pdf["publisher"], pdf["source"]) == (
        "Office of the Chief Actuary (OCA)", "osfi-bsif.gc.ca")


def test_unknown_publisher_uses_full_hostname_instead_of_inherited_name():
    assert publisher_name("Regional Climate Institute", ["https://unknown.example.org/report"]) == \
        "unknown.example.org"
