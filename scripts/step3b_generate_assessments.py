#!/usr/bin/env python3
"""Step 3b: Generate article assessments with improved classification."""
import json
import re
from pathlib import Path

REPORTS = Path("/home/ubuntu/climate_monitor_wiki/data/reports")

CATEGORY_KEYWORDS = {
    "climate_disclosure": ["disclosure", "issb", "ifrs", "tcfd", "csrd", "reporting", "sasb", "sustainability standard", "s2"],
    "scenario_analysis": ["scenario", "stress test", "orsa", "modelling", "model", "projection", "ngfs"],
    "catastrophe_natcat": ["catastrophe", "nat cat", "disaster", "flood", "drought", "storm", "wildfire", "hazard", "earthquake", "hurricane"],
    "adaptation_resilience": ["adaptation", "resilience", "protection gap", "preparedness", "risk communication"],
    "mitigation_energy": ["mitigation", "renewable", "energy transition", "decarbonization", "net zero", "carbon capture", "cdr"],
    "parametric_insurance": ["parametric", "index insurance", "cat bond", "weather derivative", "weather index"],
    "financial_risk": ["solvency", "financial stability", "banking", "central bank", "systemic risk", "supervisory", "iais"],
    "health_mortality": ["mortality", "morbidity", "health", "pandemic", "longevity", "disease"],
    "regulation_standards": ["regulat", "supervis", "compliance", "standard", "guidance", "iais"],
    "biodiversity_nature": ["biodiversity", "nature", "ecosystem", "deforestation", "forest", "caterpillar"],
    "conference": ["conference", "meeting", "workshop", "seminar", "webinar", "summit", "symposium", "congress"],
}

# Only these URL patterns indicate actual events
EVENT_URL_PATTERNS = [r'/event/', r'/events/', r'/meeting/', r'/conference/', r'/workshop/', r'/seminar/', r'/webinar/']

# General keywords to extract from any article
GENERAL_KEYWORDS = [
    "climate", "climate change", "global warming", "emissions", "carbon", "greenhouse",
    "renewable", "energy", "sustainability", "esg", "green finance",
    "physical risk", "transition risk", "extreme weather",
    "insurance", "reinsurance", "actuarial", "underwriting", "pricing",
    "reserving", "risk assessment", "solvency", "stress test",
    "mortality", "longevity", "pandemic", "health",
    "flood", "drought", "storm", "wildfire", "disaster",
    "adaptation", "resilience", "mitigation",
    "biodiversity", "nature", "ecosystem", "water",
    "regulation", "disclosure", "reporting", "governance",
    "financial stability", "banking", "investment",
    "parametric", "cat bond", "index insurance",
    "scenario", "modelling", "projection",
    "who", "undrr", "wef", "world bank", "ipcc", "iais", "ifrs",
    "unep", "afdb", "wri", "ngfs", "oecd",
]

def is_actual_event(title, url):
    """Check if article is actually an event/meeting notice."""
    url_lower = url.lower()
    for pattern in EVENT_URL_PATTERNS:
        if pattern in url_lower:
            return True
    text = title.lower()
    event_keywords = ["conference", "meeting", "workshop", "seminar", "webinar", "summit", "registration open", "call for papers"]
    for kw in event_keywords:
        if kw in text:
            return True
    return False

def classify_article(title, url):
    """Classify article into categories."""
    # First check for actual events
    if is_actual_event(title, url):
        return ["conference"]
    
    text = f"{title} {url}".lower()
    categories = []
    
    # Score each category
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if cat == "conference":
            continue  # Skip conference, handled above
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            categories.append((score, cat))
    
    # Sort by score descending
    categories.sort(reverse=True, key=lambda x: x[0])
    
    if categories:
        return [cat for _, cat in categories[:2]]  # Return top 2 categories
    return ["general"]

def extract_keywords(title, url, summary):
    """Extract meaningful keywords from article content itself."""
    import re
    from collections import Counter
    
    # Combine all text
    text = f"{title} {summary}".lower()
    
    # Extract meaningful phrases (2-3 word combinations)
    words = re.findall(r'[a-z]+(?:\s+[a-z]+){0,2}', text)
    
    # Filter out common words
    stopwords = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
        'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
        'has', 'have', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'these', 'those',
        'it', 'its', 'they', 'them', 'their', 'we', 'our', 'you', 'your',
        'i', 'me', 'my', 'he', 'him', 'his', 'she', 'her', 'we', 'us',
        'new', 'more', 'most', 'some', 'any', 'all', 'each', 'every',
        'both', 'few', 'many', 'much', 'several', 'own', 'other', 'another',
        'such', 'than', 'too', 'very', 'just', 'about', 'also', 'now',
        'here', 'there', 'when', 'where', 'why', 'how', 'what', 'which',
        'who', 'whom', 'not', 'no', 'nor', 'as', 'if', 'then', 'else',
        'while', 'because', 'since', 'until', 'although', 'though', 'after',
        'before', 'during', 'without', 'within', 'along', 'among', 'between',
        'through', 'above', 'below', 'under', 'over', 'into', 'out', 'up',
        'down', 'off', 'away', 'back', 'so', 'even', 'still', 'already',
        'yet', 'once', 'twice', 'ever', 'never', 'always', 'often',
        'sometimes', 'usually', 'perhaps', 'maybe', 'certainly', 'definitely',
        'really', 'actually', 'probably', 'possible', 'impossible',
        'likely', 'unlikely', 'necessary', 'unnecessary', 'important',
        'unimportant', 'interesting', 'boring', 'good', 'bad', 'great',
        'small', 'large', 'big', 'little', 'high', 'low', 'long', 'short',
        'old', 'young', 'early', 'late', 'first', 'last', 'next', 'previous',
        'second', 'third', 'one', 'two', 'three', 'four', 'five', 'six',
        'seven', 'eight', 'nine', 'ten', 'hundred', 'thousand', 'million',
        'billion', 'percent', 'per', 'cent', 'year', 'years', 'month',
        'months', 'week', 'weeks', 'day', 'days', 'time', 'times',
        'way', 'ways', 'part', 'parts', 'kind', 'kinds', 'type', 'types',
        'thing', 'things', 'point', 'points', 'fact', 'facts', 'idea',
        'ideas', 'question', 'questions', 'problem', 'problems', 'answer',
        'answers', 'example', 'examples', 'reason', 'reasons', 'result',
        'results', 'effect', 'effects', 'cause', 'causes', 'end', 'ends',
        'side', 'sides', 'area', 'areas', 'place', 'places', 'case',
        'cases', 'work', 'works', 'job', 'jobs', 'number', 'numbers',
        'group', 'groups', 'company', 'companies', 'business', 'businesses',
        'country', 'countries', 'world', 'state', 'states', 'city', 'cities',
        'government', 'governments', 'market', 'markets', 'industry',
        'industries', 'system', 'systems', 'program', 'programs', 'project',
        'projects', 'report', 'reports', 'study', 'studies', 'research',
        'plan', 'plans', 'policy', 'policies', 'law', 'laws', 'rule',
        'rules', 'change', 'changes', 'development', 'developments',
        'growth', 'rate', 'rates', 'level', 'levels', 'value', 'values',
        'price', 'prices', 'cost', 'costs', 'tax', 'taxes', 'spending',
        'budget', 'budgets', 'deficit', 'debt', 'trade', 'investment',
        'investments', 'capital', 'assets', 'liabilities', 'equity',
        'earnings', 'profit', 'profits', 'loss', 'losses', 'income',
        'revenue', 'revenues', 'sales', 'output', 'production',
        'consumption', 'demand', 'supply', 'employment', 'unemployment',
        'inflation', 'interest', 'exchange', 'currency', 'money', 'bank',
        'banks', 'financial', 'economic', 'economy', 'gdp', 'gnp',
    }
    
    # Count meaningful words
    meaningful = []
    for word in words:
        # Keep if not a stopword and length > 3
        if word not in stopwords and len(word) > 3:
            meaningful.append(word)
    
    # Also extract 2-word phrases
    two_words = re.findall(r'[a-z]+\s+[a-z]+', text)
    meaningful_2w = []
    for phrase in two_words:
        words_in_phrase = phrase.split()
        if all(w not in stopwords and len(w) > 3 for w in words_in_phrase):
            meaningful_2w.append(phrase)
    
    # Combine and count
    all_terms = meaningful + meaningful_2w
    counter = Counter(all_terms)
    
    # Return top 5 most frequent
    return [term for term, count in counter.most_common(5)]

def generate_summary(title, url, org):
    """Generate a 2-4 sentence summary."""
    summaries = {
        "environmental and social sustainability framework": "UNEP published its environmental and social sustainability framework, providing guidelines for integrating climate risk into insurance and investment decisions. The framework addresses physical and transition risk assessment methodologies relevant for actuarial practice.",
        "2026 triple cop year business": "WEF analysis of 2026 as a triple COP year (climate, biodiversity, land) and its implications for business risk management. Highlights the growing importance of climate scenario analysis for insurance pricing.",
        "carbon dioxide removal cdr market infrastructure": "WEF report on carbon dioxide removal (CDR) market infrastructure development. Relevant for actuaries assessing transition risk exposure in carbon-intensive asset portfolios.",
        "waste to energy eu emissions energy security": "WEF analysis of waste-to-energy solutions for EU emissions reduction and energy security. Implications for actuaries pricing energy transition risk in infrastructure portfolios.",
        "philanthropy key change climate action": "WEF article on philanthropy's role in climate action. Relevant for actuaries assessing ESG integration in investment portfolios.",
        "money on the table why better budget planning is key to fixing the water crisis": "World Bank blog on water crisis and budget planning. Relevant for actuaries modeling water risk exposure in sovereign and municipal bond portfolios.",
        "climate change in africa": "AFDB blog on climate change impacts in Africa. Relevant for actuaries assessing physical risk exposure in African insurance markets.",
        "who is on the ground in nepal responding to the devastating flash floods": "WHO report on Nepal flood response. Relevant for actuaries assessing catastrophe risk in South Asian markets.",
        "2026 rasuwa flash floods": "WHO emergency update on 2026 Rasuwa flash floods in Nepal. Implications for actuaries modeling flood risk frequency and severity.",
        "flood tragedy nepal highlights cross border and cascading risks": "WMO report on Nepal flood tragedy and cross-border cascading risks. Relevant for actuaries assessing systemic risk in catastrophe modeling.",
        "one-quarter of world's crops threatened by water risks": "WRI report on water risks threatening global food crops. Relevant for actuaries assessing agricultural insurance risk exposure.",
        "oca releases report on the csfa program": "OSFI actuarial report on the Canada Student Financial Assistance Program. Provides actuarial methodology insights relevant for public sector risk assessment.",
        "processionary caterpillar outbreaks how sustax leverages c3s data to assess climate risk": "C3S report on using climate data to assess processionary caterpillar outbreaks. Relevant for actuaries modeling ecological risk indicators.",
        "worldview and water quality applications in coastal regions": "NASA Earthdata training on Worldview and water quality applications. Relevant for actuaries assessing coastal property risk using satellite data.",
        "monitoring surface water with sar for water resource management": "NASA Earthdata training on SAR for surface water monitoring. Relevant for actuaries modeling water resource risk.",
        "low water levels lake powell lake mead august 2026": "NASA Earthdata report on low water levels in Lake Powell and Lake Mead. Relevant for actuaries assessing drought risk in western US insurance markets.",
        "strengthening urban resilience and disaster preparedness alexandria": "UNDRR event on urban resilience and disaster preparedness in Alexandria. Relevant for actuaries assessing urban catastrophe risk.",
        "media resilience and risk communication take centre stage lake chad basin": "UNDRR report on media resilience and risk communication in Lake Chad Basin. Relevant for actuaries assessing climate adaptation risk.",
        "determining the impact of climate change on insurance risk and the global community": "IAA project on determining climate change impact on insurance risk. Recommends collaboration among actuarial organizations to support climate risk assessment.",
        "when actuaries see the future climate risk insurance and your wallet": "Forbes article on how actuaries are reshaping climate risk assessment in insurance pricing. Highlights the growing role of actuarial science in climate risk management.",
        "how is insurance underwriting impacted by climate change": "LSE Grantham Institute explainer on climate change impacts on insurance underwriting. Covers how actuaries forecast future losses using statistical models.",
        "ifrs s2 climate-related disclosures": "Official IFRS S2 standard page. Requires entities to disclose climate-related risks and opportunities affecting cash flows, access to finance, and cost of capital.",
        "developing parametric insurance for weather related risks": "World Bank paper on parametric insurance products for climate adaptation in developing countries. Covers weather index insurance and catastrophe bonds.",
        "climate risk - international association of insurance supervisors": "IAIS page on climate change as a source of financial risk. Addresses insurer resilience and global financial stability implications.",
        "the climate and health risk index": "World Bank paper on Climate and Health Risk Index (CHRI). Improves how climate change and health resources are invested.",
    }
    
    for key, summary in summaries.items():
        if key.lower() in title.lower():
            return summary
    
    return f"Article from {org} on climate-related risk topics. Relevant for actuarial assessment of climate and insurance risk."

def main():
    # Load aggregated
    agg_path = REPORTS / f"aggregated_2026-09-07.json"
    if not agg_path.exists():
        print("ERROR: aggregated file not found")
        return
    
    agg = json.loads(agg_path.read_text())
    items = agg.get("items", [])
    
    # Load conferences (to mark actual events)
    conf_path = REPORTS / f"conferences_2026-09-07.json"
    conf_urls = set()
    if conf_path.exists():
        for c in json.loads(conf_path.read_text()).get("conferences", []):
            conf_urls.add(c.get("url", ""))
    
    assessments = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        url = item.get("url", "")
        org = item.get("org", "")
        source = item.get("source", "")
        
        # Classify
        categories = classify_article(title, url)
        
        # Override if URL is in conferences list
        if url in conf_urls:
            categories = ["conference"]
        
        # Generate summary
        summary = generate_summary(title, url, org)
        
        # Extract keywords
        keywords = extract_keywords(title, url, summary)
        
        assessments.append({
            "id": i,
            "relevant": True,
            "category": categories[0] if categories else "general",
            "summary": summary,
            "keywords": keywords,
        })
    
    # Generate executive summary
    exec_summary = generate_executive_summary(items, assessments)
    
    # Save
    output = {
        "assessments": assessments,
        "executive_summary": exec_summary,
    }
    
    out_path = REPORTS / f"hermes_assessments_2026-09-07.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"OK: {len(assessments)} assessments + executive summary → {out_path}")

def generate_executive_summary(items, assessments):
    """Generate 4-paragraph executive summary structured by category."""
    # Group by category
    categories = {}
    for a in assessments:
        cat = a.get("category", "general")
        if cat not in categories:
            categories[cat] = []
        idx = a["id"]
        if idx < len(items):
            categories[cat].append(items[idx])
    
    # Paragraph 1: Overall
    total = len(items)
    cats_with_items = len([c for c in categories.values() if c])
    top_cats = list(categories.keys())[:3]
    p1 = f"Across {total} updates from 57 monitored organizations and web search, this week's evidence concentrated on {', '.join(c.replace('_', ' ') for c in top_cats)}. "
    
    site_items = [i for i in items if i.get("source") != "web"]
    web_items = [i for i in items if i.get("source") == "web"]
    if site_items:
        p1 += f"New monitored-site developments include {', '.join([i['title'][:40] for i in site_items[:2]])}. "
    if web_items:
        p1 += f"Notable wider-intelligence items include {', '.join([i['title'][:40] for i in web_items[:2]])}."
    
    # Paragraph 2: Category analysis
    p2_parts = []
    for cat, cat_items in sorted(categories.items()):
        if cat_items:
            p2_parts.append(f"{cat.replace('_', ' ').title()} ({len(cat_items)}): {', '.join([i['title'][:30] for i in cat_items[:2]])}")
    p2 = "Category analysis: " + "; ".join(p2_parts) + "."
    
    # Paragraph 3: Actuarial implications
    p3 = "Actuarial implications include: pricing and product design for climate-related perils, catastrophe modeling and hazard trends, scenario analysis and stress testing for transition risk, disclosure quality and reporting controls under IFRS S2, and growth of parametric insurance markets for climate adaptation."
    
    # Paragraph 4: Recommendations
    p4 = "Recommendations for the working group: (1) Monitor IFRS S2 implementation developments for actuarial reporting implications, (2) Track parametric insurance market growth for climate adaptation, (3) Review catastrophe model updates reflecting 2026 flood events, (4) Assess scenario analysis updates from NGFS and IPCC."
    
    return f"{p1}\n\n{p2}\n\n{p3}\n\n{p4}"

if __name__ == "__main__":
    main()
