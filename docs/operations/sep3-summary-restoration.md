# September 3 summary restoration

Reviewed on 2026-09-14. Historical `c66b7ab` supplied article prose; `0d221d2` removed it because its claims could not be verified. This revision reconstructs descriptions from checked source material, rather than reverting that evidence correction.

- Before report SHA-256: `244e65b0c5b4b775e72c46c1ae61490d703d38a79c6a507f0bf1e2f98f76aea2`
- Revised report SHA-256: `102480ebc694b9e9aad38dd4b7f010866e6a8f76b20397de35cb86cf9fedad5f`
- Membership remains 22 records: 18 Pillar A and 4 Pillar B. Titles, original URLs, categories, keywords, and unknown acquisition counts are preserved.
- Current publisher pages were checked retrospectively. No claim is made that their complete current contents were already available on September 3.
- Removed the unsubstantiated “last 3 months” presentation: the retained set includes a 2018 World Bank note, a 2024 WRI article, and January/June 2026 intelligence.
- No production database or website checkpoint is changed by this content PR. The normal Registry update and weekly-sync correctly reject a changed hash for an existing report. Deployment therefore requires a separately validated historical-revision database candidate, preserving all existing content, fetches, enrichment and validated fallback evidence, and the existing exact-SHA backup/restore gate. Do not use weekly-sync to bypass that identity check, or promote a Markdown-only rebuild that loses retained evidence. Old report/PDF identities are not promoted as the new version.

## Sources and scope of each description

| Record | Verification basis |
|---|---|
| [Media Resilience and Risk Communication Take Centre Stage Lake Chad Basin](https://www.undrr.org/news/media-resilience-and-risk-communication-take-centre-stage-lake-chad-basin) | Checked publisher article or report body. |
| [Philanthropy Key Change Climate Action](https://www.weforum.org/stories/nature-and-biodiversity/philanthropy-key-change-climate-action) | Checked publisher article or report body. |
| [Processionary caterpillar outbreaks: how Sustax leverages C3S data to assess climate risk](https://climate.copernicus.eu/processionary-caterpillar-outbreaks-how-sustax-leverages-c3s-data-assess-climate-risk) | Checked publisher article or report body. |
| [WHO is on the Ground in Nepal Responding to the Devastating Flash Floods](https://www.who.int/multi-media/details/who-is-on-the-ground-in-nepal-responding-to-the-devastating-flash-floods) | Publisher video metadata; description limited to stated subject. |
| [2026 Rasuwa Flash Floods](https://www.who.int/nepal/emergencies/2026-rasuwa-flash-floods) | Checked publisher article or report body. |
| [Flood Tragedy Nepal Highlights Cross Border and Cascading Risks](https://wmo.int/media/news/flood-tragedy-nepal-highlights-cross-border-and-cascading-risks) | Checked publisher article or report body. |
| [Strengthening Urban Resilience and Disaster Preparedness Alexandria](https://www.undrr.org/event/strengthening-urban-resilience-and-disaster-preparedness-alexandria) | Checked publisher article or report body. |
| [Environmental and Social Sustainability Framework](https://www.unep.org/about-un-environment-programme/environmental-and-social-sustainability-framework) | Checked publisher article or report body. |
| [2026 Triple Cop Year Business](https://www.weforum.org/stories/climate-action/2026-triple-cop-year-business) | Checked publisher article or report body. |
| [Waste to Energy EU Emissions Energy Security](https://www.weforum.org/stories/energy-transition/waste-to-energy-eu-emissions-energy-security) | Checked publisher article or report body. |
| [Money on the Table Why Better Budget Planning IsKey to Fixing the Water Crisis](https://blogs.worldbank.org/en/voices/Money-on-the-Table-Why-Better-Budget-Planning-IsKey-to-Fixing-the-Water-Crisis) | Checked publisher article or report body. |
| [Climate Change in Africa](https://blogs.afdb.org/climate-change-in-africa) | Retained AfDB homepage snapshot 803: collection link, not article body. |
| [One-Quarter of World’s Crops Threatened by Water Risks](https://www.wri.org/insights/growing-water-risks-food-crops) | Checked publisher article or report body. |
| [OCA releases Report on the CSFA Program](https://www.osfi-bsif.gc.ca/en/oca/actuarial-reports/actuarial-report-canada-student-financial-assistance-program-31-july-2025) | Checked publisher article or report body. |
| [Worldview and Water Quality Applications in Coastal Regions](https://www.earthdata.nasa.gov/learn/trainings/worldview-water-quality-applications-coastal-regions) | Retained NASA homepage snapshot 834: training description. |
| [Monitoring Surface Water with SAR for Water Resource Management](https://www.earthdata.nasa.gov/learn/trainings/monitoring-surface-water-sar-water-resource-management) | Retained NASA homepage snapshot 834: training description. |
| [Low Water Levels Lake Powell Lake Mead August 2026](https://www.earthdata.nasa.gov/news/worldview-image-archive/low-water-levels-lake-powell-lake-mead-august-2026) | Retained NASA homepage snapshot 834: image title and listing. |
| [Carbon Dioxide Removal CDR Market Infrastructure](https://www.weforum.org/stories/climate-action/carbon-dioxide-removal-cdr-market-infrastructure) | Checked publisher article or report body. |
| [When Actuaries See The Future: Climate Risk, Insurance, And Your Wallet](https://www.forbes.com/sites/billfrist/2026/01/21/when-actuaries-see-the-future-climate-risk-insurance-and-your-wallet/) | Checked publisher article or report body. |
| [How is insurance underwriting impacted by climate change? - LSE](https://www.lse.ac.uk/granthaminstitute/explainers/how-is-insurance-underwriting-impacted-by-climate-change/) | Checked publisher article or report body. |
| [The Climate and Health Risk Index](https://openknowledge.worldbank.org/entities/publication/70e9088c-3183-4651-a559-3782a12b8352) | [Publisher PDF](https://documents1.worldbank.org/curated/en/099031326094030586/pdf/P501993-2d5b1fc0-ed34-48a7-8113-08f2167cd67e.pdf); same publication title as the retained record. |
| [Developing Parametric Insurance for Weather Related Risks](https://openknowledge.worldbank.org/entities/publication/97cce0ec-4645-51eb-9584-0190028bfc59) | [Publisher PDF](https://documents1.worldbank.org/curated/en/704171524632990898/pdf/Technical-Note.pdf); same publication title as the retained record. |

## Retained snapshot provenance

Database: `<web-listening-db>` (host path, read-only). Only the relevant listing descriptions were used; a homepage excerpt was not treated as a full article.

- Snapshot 803: `https://www.afdb.org/en`, captured `2026-09-01T23:34:53.873927+00:00`, retained markdown SHA-256 `948add740b3cf2da061ba35dfe4de245fab405778c63b70087d529236498d84b`.
- Snapshot 834: `https://www.earthdata.nasa.gov/`, captured `2026-09-01T23:36:36.161392+00:00`, retained markdown SHA-256 `3427ec87273df0353be59257b3ecd8fbc9f842d2ad2fabbedf7a31427801b945`.

## Isolated database rehearsal

The one-off candidate was built from a consistent SQLite backup, preserving schema v6. It is not installed in production. The normal append-only planner now reports no identity conflicts or new reports against the revised source directory.

- Report membership stays at 196 articles / 28 reports. Append 22 article-summary versions (299 → 321); retain every prior version.
- Update only the September 3 report hash, its 22 discovery summaries/version IDs, 22 appearance version IDs, and the corresponding current summary-version pointers.
- Full row comparisons preserve all other data, including 26 retained content versions, 30 fetches, 26 enrichments, 9 validated capture resolutions, all display/content pointers and the earlier August publication corrections. A plain Markdown rebuild would lose those corrections.
- SQLite integrity/foreign keys pass. The actual Registry reader displays all 22 revised summaries and the revised executive summary; unknown monitoring totals remain unknown. Outputs for the other 27 reports are identical to the backup.
- Production database SHA-256 stayed `52fa67bb4618136febfe1183d56b3ab4a9e713896ba8a77a9e42eb17ff91a985`.
- Consistent backup SHA-256: `355bf20cce3f67de54bdeb717e9947c70b44a24bf7c26e5fc9fa670fe4f0e7e4`.
- Validated candidate SHA-256: `9d42941737264eb83c6686e4a56842931f7b58cb40eb3c8da8137eadc03bcf54`.
- Server receipt and snapshots: `<host-data-dir>/manual-recovery/sep3-summary-restoration-v5/validated.json`. One-off preparation script: `/tmp/sep3_candidate.py` (retained with the recovery bundle). Earlier v1–v4 directories are aborted validation attempts and must not be promoted.

Before a future deployment, recheck the live baseline. If another sync has written to it, rebuild and validate from the fresh backup; never overwrite newer data with this candidate. Source and Registry identity must be coordinated with the existing exact-SHA backup/restore gate and a service reload. This content PR does not authorize that deployment or email delivery.

Validation: Python 3.11 on Windows, `tests/test_climate_registry_reports.py` and `tests/test_climate_registry_audit.py`: 18 passed; `node --check showcase/app.js` and `git diff --check` passed. Linux-only coverage is supplied by the PR CI. Independent source-content review passed after removing an unsupported statement about the WHO video contents.
