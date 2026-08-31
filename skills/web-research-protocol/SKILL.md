---
name: web-research-protocol
description: >
  Standard operating procedure for live web research with Tavily: distinguishing a tool
  outage from a genuinely empty result, reading URLs with tavily_extract, research
  caching via read_research_brief and save_research_brief (with force_refresh on
  disputes), anti-looping rules, and inline citations.
---

# Web Research Protocol — SOP & Guardrails

> **Executor:** Tavia | **Tools:** `tavily_search`, `tavily_extract`, `think_tool`, `read_research_brief`, `save_research_brief`, `datetime_tools`

---

# ⛔ HARD GUARDRAILS & ERROR RECOVERY MATRIX (P0)

| Error / Edge Case | Cause | Binding Recovery Invariant |
|---|---|---|
| **Tool outage reported as a finding** | Treating a provider rejection as "nothing exists" | **Outage Rule:** `[TOOL_OUTAGE]` in any result ⇒ `STATUS: BLOCKED`, zero citations, no `save_research_brief`. Overrides every other rule here. |
| **Fabricated answer survives push-back** | Cache-first replaying a bad brief | **Dispute Rule:** When the user contradicts or asks you to re-verify, call `read_research_brief(..., force_refresh=True)`. |
| **"Check this link" answered by searching** | Using `tavily_search` on a URL | **URL Rule:** A URL in the task ⇒ `tavily_extract(urls=[...])`, passed verbatim, never repaired or guessed. |
| **Page reported empty when it is not** | Reading raw HTML of a client-rendered app | **Render Rule:** Only `tavily_extract` renders JavaScript. Never call a dashboard/leaderboard empty without extract output showing it empty. |
| **Duplicate Search Requests** | Searching without checking cache | **Cache Check First:** Always call `read_research_brief(filename="<slug>.md")` first. If hit, return cached report immediately, noting its age. |
| **Unsaved Research** | Returning search without saving | **Save Cache Rule:** Save fresh, verified research via `save_research_brief(filename="<slug>.md", content=...)`. A `REFUSED` return is the outage guard — honour it, do not retry. |
| **Infinite Search Loops** | Executing > 2 search queries | **Strict Anti-Looping Rule:** Execute at most 1–2 `tavily_search` calls on cache miss. |
| **Uncited Claims** | Hallucinating statistics/facts | **Citation Rule:** Every claim, stat, date, and figure MUST have an inline citation `[Source Title](URL)`. |
| **Absence stated as fact** | Treating a failure as a negative result | **Absence Rule:** "not found", "0 results", "not participating" are claims needing the same evidence as any other claim. |

---

# 🚨 THREE TOOL-RESULT STATES

| Result contains | Means | Correct response |
|---|---|---|
| `[TOOL_OUTAGE]` | Provider rejected the call; you learned nothing | `STATUS: BLOCKED`, name the reason, save nothing |
| `[SYSTEM_GOVERNOR_WARNING]` | Provider worked, genuinely returned nothing | `STATUS: COMPLETED`, finding = "no results for `<query>`" |
| Actual content | Provider worked and returned data | Synthesize, citing only URLs present in the result |

Never convert the first into the second. If you cannot tell which state you are in, you
are in the first.

---

# ⚡ CORE OPERATIONAL RULES

1. **Classify first:** URL in the request → `tavily_extract`. Otherwise → cache check → `tavily_search`.
2. **Cache Protocol:** Check `read_research_brief` first (`force_refresh=True` on disputes/re-verification). On hit → return cached report with its age. On miss/stale/bypass → search or extract → `save_research_brief`.
3. **Temporal Grounding:** Call `get_current_datetime()` first for time-bound queries ("today", "current quarter", "latest releases", any live standing or ranking).
4. **Strategic Reflection:** Use `think_tool` after search iterations to evaluate factual gaps before proceeding.
5. **Outage is terminal:** Never dress up a `[TOOL_OUTAGE]` as a result. Report it and stop.

---

# 🛠️ NATIVE TOOL MODULE REFERENCE

- `web_search.py`: `tavily_search`, `tavily_extract`, `think_tool`, `read_research_brief`, `save_research_brief`
- `datetime_tools.py`: `get_current_datetime`, `calculate_future_datetime`

---

# 📎 LEARNINGS

- **2026-08-30 — a quota outage became four confident lies.** The Tavily key hit
  `Error 432: This request exceeds your plan's set usage limit`. `tavily_search` returned
  its outage notice correctly, but the SOP had no outage branch, so the contract block was
  filled in from nothing: `STATUS: COMPLETED`, invented citations, and the claim that a
  live leaderboard was empty. The fabricated brief was then cached, so every re-check —
  including after the user said "that's not true, I just opened it" — replayed it. Ground
  truth: the entry was ranked 23rd with 75 votes. Three fixes came out of this: the outage
  branch above, `force_refresh`, and a code-level refusal in `save_research_brief` that
  makes an outage un-cacheable.
- **Client-rendered pages have no data in their HTML.** The target page's rows came from a
  browser-side database query; `curl` returned a 13 KB empty shell. Only `tavily_extract`
  renders JavaScript. A plain HTTP fetch would have "confirmed" the wrong answer.
