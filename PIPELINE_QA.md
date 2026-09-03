# Pipeline Q&A — 2026-09-02

## Q1: article_state.json 包含 Pillar A and B，right？

**答：只包含 Pillar A。**

- `article_state.json` 有 57 个机构、1349 个 URL
- 这些 URL 全部来自 `web_listening` 系统对 57 个机构网站的监控（Pillar A）
- Pillar B 的 URL 来自 Hermes `web_search`，不写入 `article_state.json`

**这意味着**：基线去重只对 Pillar A 有效。Pillar B 的 URL 可能和 Pillar A 重复（如 IPCC 文章既被网站监控又被搜索到），需要在 Step 3 聚合时去重。

---

## Q2: Step 4 为什么 Pillar A 没有 per-article summary？Step 3b 和 Step 4 能整合吗？

**问题根因**：
- Step 3b（Hermes）和 Step 4（Hermes）是两个独立的 cron job
- 但 Step 3b 的 `hermes_assessments_YYYY-MM-DD.json` 没有生成（job 还没跑）
- 所以 Step 3 Filter 用了 keyword fallback，没有 LLM 生成的 summary
- Step 4 只生成 Executive Summary，不生成每篇 article 的 summary

**解决方案：合并为一个 Hermes LLM 调用**

| 原步骤 | 新步骤 | 说明 |
|---|---|---|
| Step 3b: Hermes Relevance Filter | → 合并为 Step 3b | 一次 LLM 调用完成所有工作 |
| Step 4: LLM Summary Generation | → 合并为 Step 3b | 同上 |

**新 Step 3b 输出**：
```json
{
  "assessments": [
    {"id": 0, "relevant": true, "category": "climate_disclosure", "summary": "...", "keywords": [...]},
    ...
  ],
  "executive_summary": "4段叙述性摘要..."
}
```

**LLM 调用次数**：从 2 次（Step 3b + Step 4）→ 1 次

---

## Q3: Step 5 MD 是之后所有流程的唯一数据来源

**修复后的 MD 结构**：

```markdown
# 🌡️ Weekly Climate & Actuarial Monitor (Supranational Orgs)

**Report Date:** 2026-09-07
**Generated:** 2026-09-02T00:00:00Z
**Scope:** 57 supranational organization sites monitored

---

## 📋 Executive Summary

- Sites checked: **57**, succeeded: **57**, failed: **0**
- Monitored window: last 7 days
- Pillar B search window: last 3 months
- Total detected changes: **23** → After relevance filter: **23**

- Across 23 filtered items, evidence concentrated on climate disclosure frameworks...

---

## 🔎 Pillar A — Climate & Actuarial Site Changes

### Climate/actuarial-relevant site changes: 18

**IPCC**
- **IWMI/IPCC co-sponsored Expert Meeting on Addressing Water Risks...**
  - Keywords: water risk, climate resilience, adaptation
  - Category: Adaptation & Resilience
  🔗 https://www.ipcc.ch/event/...

**WEF**
- **2026 triple cop year business**
  - Keywords: COP, climate policy, business
  - Category: Mitigation & Energy
  🔗 https://www.weforum.org/...

... (更多机构)

---

## 🌍 Pillar B — Climate & Actuarial Intelligence (last 3 months)

- **SOA Research: Climate Risk and Insurance Actuarial Perspectives 2026** (web)
  - Keywords: SOA, climate risk, insurance pricing
  - Category: Climate Disclosure
  🔗 https://www.soa.org/...

... (更多条目)

---

## 🔗 Original Links

- https://www.ipcc.ch/event/...
- https://www.weforum.org/...
...
```

**MD 中包含的信息**：
- ✅ 每篇文章的标题、URL、分类、关键词
- ✅ 叙述性 Executive Summary（4 段）
- ✅ 过滤统计（总数、相关数、非相关数）
- ✅ 原始链接列表

**下游流程**：
- Step 6 (PDF) → 从 MD 渲染
- Step 7 (Email) → 从 MD 生成邮件内容
- Step 8 (Registry) → 从 MD 提取文章信息同步到数据库
- Step 9 (Website) → 从 MD 生成 wiki 页面
