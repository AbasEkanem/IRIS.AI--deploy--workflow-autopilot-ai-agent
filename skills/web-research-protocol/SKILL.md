---
name: web-research-protocol
description: >
  Standard operating procedure for live web research with Tavily, research caching
  via read_research_brief and save_research_brief, anti-looping rules, and inline citations.
---

# Web Research Protocol — SOP & Guardrails

> **Executor:** Tavia | **Tools:** `tavily_search`, `think_tool`, `read_research_brief`, `save_research_brief`, `datetime_tools`

---

# ⛔ HARD GUARDRAILS & ERROR RECOVERY MATRIX (P0)

| Error / Edge Case | Cause | Binding Recovery Invariant |
|---|---|---|
| **Duplicate Search Requests** | Searching without checking cache | **Cache Check First:** Always call `read_research_brief(filename="<slug>.md")` first. If hit, return cached report immediately. |
| **Unsaved Research** | Returning search without saving | **Save Cache Rule:** Always save fresh research via `save_research_brief(filename="<slug>.md", content=...)`. |
| **Infinite Search Loops** | Executing > 2 search queries | **Strict Anti-Looping Rule:** Execute at most 1–2 `tavily_search` calls on cache miss. |
| **Uncited Claims** | Hallucinating statistics/facts | **Citation Rule:** Every claim, stat, date, and figure MUST have an inline citation `[Source Title](URL)`. |

---

# ⚡ CORE OPERATIONAL RULES

1. **Cache Protocol:** Check `read_research_brief` first. On hit → return cached report. On miss → `tavily_search` → `save_research_brief`.
2. **Temporal Grounding:** Call `get_current_datetime()` first for time-bound queries ("today", "current quarter", "latest releases").
3. **Strategic Reflection:** Use `think_tool` after search iterations to evaluate factual gaps before proceeding.

---

# 🛠️ NATIVE TOOL MODULE REFERENCE

- `web_search.py`: `tavily_search`, `think_tool`, `read_research_brief`, `save_research_brief`
- `datetime_tools.py`: `get_current_datetime`, `calculate_future_datetime`
