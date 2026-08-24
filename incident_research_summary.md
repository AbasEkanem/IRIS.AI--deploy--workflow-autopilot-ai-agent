# Industry Research Summary: AI Agent Crashes & Hallucinations in Live Demos
**Source:** Tavia web research (completed 2026-08-21)
**Cache Status:** MISS (fresh research)
**Research File:** tmp/ai_agent_crashes_hallucinations_live_demo_best_practices.md

---

## Key Findings

### Root Causes Identified
1. **Knowledge gaps & weak grounding** → confident hallucinations when info missing
2. **Retrieval failures (RAG issues)** → poor chunking, missing hybrid retrieval/reranking
3. **Ambiguous system prompts** → no explicit "I don't know" instruction
4. **Context window limits** → critical info truncated
5. **Model-specific tendencies** → certain models hallucinate more on specific tasks

### Layered Guardrail Strategy (Defense-in-Depth)
| Layer | Controls |
|-------|----------|
| 1. Input | Prompt injection detection, PII redaction, input validation |
| 2. Grounding | Hybrid retrieval (vector + keyword), reranking, live structured data |
| 3. Tool Use | Schema validation, deterministic checks for pricing/financial data |
| 4. Output | Faithfulness/groundedness scorers, "no evidence no answer" policy, LLM-as-judge |
| 5. Monitoring | Real-time detection, continuous eval suites, regression gates |

### Incident Response Playbook (Live Demo)
- **0-5 min:** Kill switch → isolate instance → preserve state
- **5-30 min:** Full trace analysis (prompts, retrieval, model params, policy eval)
- **Remediation:** Hotfix validation step → deploy guardrails → update playbook
- **Comms:** Immediate acknowledgment → timeline → lessons learned → post-mortem

### Recommended Observability Tools (2026)
- **TrueFoundry** — Complete LLM observability
- **Braintrust** — Unified evals, multi-step tracing, regression testing
- **Agenta** — Open-source, self-hostable, full-trace LLM-as-judge
- **Fiddler** — Hierarchical traces, compliance monitoring
- **Trussed AI** — Runtime policy enforcement, audit-ready evidence

### Actionable Recommendations by Timeline
**Next Sprint:** Input validation, explicit uncertainty prompts, retrieval monitoring, kill switch
**Next Quarter:** Full layered guardrails, observability platform, playbook testing, continuous evals
**Next 6 Months:** Reliability SLOs, automated RCA, feedback loops, model-agnostic validation

---

## Citations (Verified)
- Stack AI: Prevent AI Agent Hallucinations in Production
- Noveum: Why Your AI Agents Are Hallucinating and How to Stop It
- Arthur AI: AI Guardrails Reduce Hallucinations
- Zylos AI: SRE for AI Agent Systems (2026-03-22)
- Microsoft TechCommunity: Applying SRE to Autonomous AI Agents
- Braintrust: Best AI Agent Observability Tools 2026
- TrueFoundry: LLM Observability Tools
- Trussed AI: AI Hallucination Monitoring Production