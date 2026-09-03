# Pipeline Configuration

## Cron Jobs (13 total)

| # | Time | Name | Type | Schedule | Prompt Config |
|---|---|---|---|---|---|
| 1 | 08:00 | Pillar A Site Check | Script | 0 8 * * 1 | N/A |
| 2 | 08:15 | Pillar B Web Search | LLM | 15 8 * * 1 | PIPELINE_CONFIG.md § Step 2 |
| 3 | 08:30 | Aggregate Report | Script | 30 8 * * 1 | N/A |
| 4 | 08:32 | Extract Conferences | Script | 32 8 * * 1 | N/A |
| 5 | 08:35 | Hermes Relevance + Classification | LLM | 35 8 * * 1 | PIPELINE_CONFIG.md § Step 3b |
| 6 | 09:00 | Build Markdown Report | Script | 0 9 * * 1 | N/A |
| 7 | 09:15 | Render PDF | Script | 15 9 * * 1 | N/A |
| 8 | 09:30 | Send Email | LLM | 30 9 * * 1 | PIPELINE_CONFIG.md § Step 7 |
| 9 | 09:45 | Sync Registry | Script | 45 9 * * 1 | N/A |
| 10 | 10:00 | Update Website | Script | 0 10 * * 1 | N/A |

## Data Flow

```
Step 1: Pillar A ──────────────→ article_changes_{DATE}.json
Step 2: Pillar B ──────────────→ pillar_b_{DATE}.json
                                      │
Step 2b: Save Pillar B State ──→ article_state.json (dedup baseline)
                                      │
Step 3: Aggregate ────────────→ aggregated_{DATE}.json
                                      │
Step 7b: Extract Conferences ──→ conferences_{DATE}.json
                                      │
Step 3b: Hermes LLM ────────→ hermes_assessments_{DATE}.json
                                      │
Step 3f: Filter ────────────→ filtered_{DATE}.json
                                      │
Step 5: Build MD ────────────→ climate-monitor-{DATE}.md (SINGLE SOURCE OF TRUTH)
                                      │
                                      ├──→ Step 6: Render PDF ──→ climate-monitor-{DATE}.pdf
                                      ├──→ Step 7: Send Email
                                      ├──→ Step 8: Sync Registry → article-registry.sqlite3
                                      └──→ Step 9: Update Website → wiki + RAG
```

## Prompt Templates

All Hermes prompts are stored in `PIPELINE_CONFIG.md` for easy modification without code changes.

### Step 2: Pillar B Web Search

```
Run Pillar B web search for climate-actuarial intelligence.

Use web_search tool with these queries (run all):
1. "climate change actuarial risk insurance disclosure {YEAR}"
2. "IFRS S2 ISSB climate disclosure actuary {YEAR}"
3. "parametric insurance climate adaptation {YEAR}"
4. "climate risk scenario actuarial {YEAR}"

Save to: data/reports/pillar_b_{REPORT_DATE}.json
Format: [{{"title":"...","url":"...","source":"web","summary":"..."}}]
```

### Step 3b: Hermes Relevance Filter + Classification

```
Step 3b: Hermes LLM relevance filter + classification + summary generation.

Read: data/reports/aggregated_{REPORT_DATE}.json
Also read: data/reports/conferences_{REPORT_DATE}.json (if exists, pre-extracted conference articles)

For each article, assess if it is TRULY relevant to BOTH climate change AND actuarial risk.

Use web_search to verify articles if needed.

For each article, provide:
- relevant: true/false (must be about BOTH climate AND actuarial/insurance topics)
- category: one of:
  * climate_disclosure (reporting standards, ISSB, IFRS S2, TCFD)
  * scenario_analysis (stress testing, ORSA, modelling)
  * catastrophe_natcat (natural disasters, floods, droughts, storms)
  * adaptation_resilience (adaptation, resilience, protection gap)
  * mitigation_energy (renewable, decarbonization, net zero)
  * parametric_insurance (index insurance, cat bonds, weather derivatives)
  * financial_risk (solvency, banking stability, systemic risk)
  * health_mortality (mortality, morbidity, longevity)
  * regulation_standards (regulation, supervision, compliance)
  * biodiversity_nature (biodiversity, nature, ecosystem)
  * conference (conference, meeting, workshop, seminar, event)
  * general (climate-related but not specific)
- summary: 2-4 sentences explaining the article's key points for actuaries
- keywords: 3-5 specific terms from article content

Also generate a 4-paragraph executive summary STRUCTURED BY CATEGORY:
1. Overall findings (total articles, key themes)
2. Category analysis (for each category with articles: category name, what issues are covered)
3. Actuarial implications (what this means for actuaries)
4. Recommendations for the working group

Save results to: data/reports/hermes_assessments_{REPORT_DATE}.json
```

### Step 7: Email

```
Send the weekly climate monitoring email.

Read: data/reports/climate-monitor-{REPORT_DATE}.md
PDF: climate_delivery_artifacts/{REPORT_DATE}/{SHA}/climate-monitor-{REPORT_DATE}.pdf

Send email with:
- Subject: Weekly Climate & Actuarial Monitor — {REPORT_DATE}
- Body: Executive Summary from MD + link to PDF
- Attachment: PDF
```
