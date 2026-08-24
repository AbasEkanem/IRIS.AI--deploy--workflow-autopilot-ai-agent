# Google Meet Agent Architecture: Automated Meeting Intelligence Pipeline

## Executive Summary

This document synthesizes research across four domains into a unified architecture for an agent that:
1. **Joins Google Meet** meetings automatically
2. **Extracts key discussions** via transcription + LLM summarization
3. **Creates structured reports** with action items, decisions, topics
4. **Generates Jira tickets** assigned to attendees based on discussion ownership
5. **Posts summaries** to Slack `#technical-update` channel

---

## Component Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        MEETING INTELLIGENCE PIPELINE                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐  │
│  │  SCHEDULER   │───▶│  MEET BOT    │───▶│  TRANSCRIBE  │───▶│  STORE   │  │
│  │  (Calendar)  │    │  (Join/Rec)  │    │  (Whisper +  │    │  (Raw    │  │
│  └──────────────┘    └──────────────┘    │   Diarize)   │    │  Audio/  │  │
│                                           └──────────────┘    │  Trans)  │  │
│                                                    │           └──────────┘  │
│                                                    ▼                    │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐              │
│  │   SLACK      │◀───│  JIRA SYNC   │◀───│  LLM EXTRACT │              │
│  │  POSTER      │    │  (Tickets)   │    │  (Summary +  │              │
│  └──────────────┘    └──────────────┘    │  Action Items)             │
│                                           └──────────────┘              │
│                                                    │                    │
│                                                    ▼                    │
│                                           ┌──────────────┐              │
│                                           │  STATE STORE │              │
│                                           │  (PostgreSQL)│              │
│                                           └──────────────┘              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow Specification

### 1. Trigger & Scheduling
| Component | Technology | Details |
|-----------|------------|---------|
| Calendar Polling | Google Calendar API | Watch for meetings with `meet_bot@domain.com` as attendee |
| Event Filter | Custom logic | Only meetings with `conferenceData.entryPoints[].meetingCode` |
| Lead Time | Configurable | Join 2 min before start; record until end |

### 2. Google Meet Bot (Join & Record)
| Approach | Pros | Cons | Recommendation |
|----------|------|------|----------------|
| **Google Meet REST API** (beta) | Official, stable | Limited availability, no real-time audio | Use when GA |
| **Puppeteer/Playwright + Meet UI** | Works today, full control | Fragile, maintenance heavy | **Current best option** |
| **Chrome Extension + WebRTC** | Native audio capture | Complex, extension review | Future |
| **Third-party (Recall.ai, Daily.co)** | Managed, scalable | Cost, vendor lock-in | **Recommended for production** |

**Selected Stack (Production):**
- **Recall.ai** or **Daily.co** for managed bot infrastructure
- **Fallback**: Self-hosted `meetbot` (Python + Playwright) for dev/staging

**Bot Capabilities:**
- Join via meeting code/link
- Record audio (stereo: bot + participants)
- Emit real-time transcript chunks via WebSocket
- Leave on meeting end or timeout

### 3. Transcription & Diarization
| Stage | Technology | Output |
|-------|------------|--------|
| ASR | **Whisper large-v3** (self-hosted) or **AssemblyAI** (API) | `transcript.json` with timestamps |
| Diarization | **pyannote.audio 3.1** or **AssemblyAI Speaker Diarization** | `speaker_turns.json` (speaker_id, start, end, text) |
| Merge | Custom pipeline | `diarized_transcript.json` — turn-by-turn with speaker labels |

**Output Schema:**
```json
{
  "meeting_id": "meet_abc123",
  "start_time": "2026-08-20T10:00:00Z",
  "end_time": "2026-08-20T11:00:00Z",
  "participants": [
    {"speaker_id": "SPEAKER_00", "name": "Alice", "email": "alice@co.com"},
    {"speaker_id": "SPEAKER_01", "name": "Bob", "email": "bob@co.com"}
  ],
  "turns": [
    {"speaker_id": "SPEAKER_00", "start": 12.3, "end": 18.7, "text": "Let's review the API design..."},
    {"speaker_id": "SPEAKER_01", "start": 19.1, "end": 25.4, "text": "I'll handle the auth module..."}
  ]
}
```

### 4. LLM Extraction Pipeline
**Framework**: LangChain / LlamaIndex + Structured Output (Instructor / Pydantic)

**Prompt Template** (condensed):
```
You are a meeting intelligence analyst. Given a diarized transcript with speaker attributions, extract:

1. MEETING OVERVIEW (2-3 sentences)
2. KEY DECISIONS (list: decision, rationale, decider)
3. DISCUSSION TOPICS (list: topic, summary, participants, open_questions)
4. ACTION ITEMS (array of objects matching ActionItem schema)

Speaker mapping: {speaker_map}

Transcript:
{transcript}
```

**Structured Output Schema (Pydantic):**
```python
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import date
from enum import Enum

class Priority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class ActionItem(BaseModel):
    title: str = Field(..., description="Concise action-oriented title")
    description: str = Field(..., description="Full context from discussion")
    assignee_email: str = Field(..., description="Email of owner from speaker attribution")
    assignee_name: str = Field(..., description="Display name")
    priority: Priority = Field(default=Priority.MEDIUM)
    due_date: Optional[date] = None
    source_quote: str = Field(..., description="Verbatim quote for traceability")
    topic: str = Field(..., description="Discussion topic this belongs to")

class MeetingReport(BaseModel):
    meeting_id: str
    title: str
    date: date
    duration_minutes: int
    participants: List[Participant]
    overview: str
    key_decisions: List[Decision]
    discussion_topics: List[DiscussionTopic]
    action_items: List[ActionItem]
```

**Model Selection:**
- **Primary**: GPT-4o / Claude 3.5 Sonnet (best reasoning + structured output)
- **Cost-optimized**: GPT-4o-mini / Llama-3.1-70B (self-hosted)
- **Validation**: Instructor retry + confidence scoring (logprob > 0.7)

### 5. Jira Ticket Creation
**API**: Jira REST API v3 (`POST /rest/api/3/issue/bulk`)

**Field Mapping:**
| ActionItem Field | Jira Field | Transform |
|------------------|------------|-----------|
| `title` | `summary` | Direct |
| `description` | `description` | ADF format with source quote |
| `assignee_email` | `assignee.accountId` | Resolve via `GET /rest/api/3/user/search?query={email}` |
| `priority` | `priority.name` | Map: high→Highest, medium→Medium, low→Low |
| `due_date` | `duedate` | ISO-8601 |
| `topic` | `labels` | Add `meeting-action`, `topic-{slug}` |
| `meeting_id` | `customfield_meeting_ref` | Custom field or label |

**Parent Linking:**
1. Create parent "Meeting" issue (type: Task/Epic) with meeting metadata
2. Link each action item via `POST /rest/api/3/issueLink` (type: "Is child of")

**Bulk Creation Code:**
```python
from atlassian import Jira

jira = Jira(url=JIRA_URL, username=JIRA_EMAIL, password=JIRA_TOKEN)

def create_meeting_tickets(report: MeetingReport) -> List[str]:
    # 1. Create parent meeting issue
    parent = jira.create_issue(fields={
        'project': {'key': PROJECT_KEY},
        'summary': f"Meeting: {report.title} ({report.date})",
        'description': report.overview,
        'issuetype': {'name': 'Task'},
        'labels': ['meeting-record', report.meeting_id]
    })
    parent_key = parent['key']
    
    # 2. Prepare bulk action items
    field_list = []
    for item in report.action_items:
        user = jira.user_search(item.assignee_email)
        account_id = user[0]['accountId'] if user else None
        
        field_list.append({
            'project': {'key': PROJECT_KEY},
            'summary': item.title,
            'description': {
                'type': 'doc', 'version': 1,
                'content': [{
                    'type': 'paragraph',
                    'content': [{'type': 'text', 'text': item.description}]
                }, {
                    'type': 'blockquote',
                    'content': [{'type': 'text', 'text': f"Source: \"{item.source_quote}\""}]
                }]
            },
            'issuetype': {'name': 'Task'},
            'assignee': {'accountId': account_id} if account_id else None,
            'priority': {'name': item.priority.value.capitalize()},
            'duedate': item.due_date.isoformat() if item.due_date else None,
            'labels': ['meeting-action', f'topic-{item.topic.lower().replace(" ", "-")}'],
        })
    
    # 3. Bulk create
    results = jira.create_issues(field_list)
    
    # 4. Link to parent
    for result in results:
        if 'key' in result:
            jira.issue_link(
                link_type='Is child of',
                inward_issue=result['key'],
                outward_issue=parent_key
            )
    
    return [r['key'] for r in results if 'key' in r]
```

### 6. Slack Posting to #technical-update
**API**: `chat.postMessage` with Block Kit

**Bot Setup:**
- App installed to workspace
- Scopes: `chat:write`, `channels:read`, `groups:read`
- Bot added to `#technical-update`

**Message Blocks:**
```python
def build_slack_blocks(report: MeetingReport, ticket_keys: List[str]) -> List[dict]:
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📋 Meeting Report: {report.title}"}},
        {"type": "divider"},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Date:*\n{report.date}"},
            {"type": "mrkdwn", "text": f"*Duration:*\n{report.duration_minutes} min"},
            {"type": "mrkdwn", "text": f"*Attendees:*\n{', '.join(p.name for p in report.participants)}"},
            {"type": "mrkdwn", "text": f"*Action Items:*\n{len(report.action_items)} created"}
        ]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Overview:*\n{report.overview}"}},
    ]
    
    if report.key_decisions:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Key Decisions:*"}})
        for d in report.key_decisions:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"• {d.decision} — *{d.decider}*"}})
    
    if report.action_items:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Action Items Created:*"}})
        for item, key in zip(report.action_items, ticket_keys):
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"• <{JIRA_URL}/browse/{key}|{key}>: {item.title} → *{item.assignee_name}*"}
            })
    
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"Meeting ID: {report.meeting_id} | Generated by Meet Intelligence Bot"}]
    })
    return blocks
```

---

## Tech Stack Summary

| Layer | Technology | Rationale |
|-------|------------|-----------|
| **Orchestration** | **LangGraph** | Cyclic workflows, checkpointing, HITL, state management |
| **Meet Bot** | **Recall.ai** (prod) / Playwright (dev) | Managed infrastructure vs. control |
| **Transcription** | **Whisper large-v3** (self-hosted GPU) | Privacy, cost at scale |
| **Diarization** | **pyannote.audio 3.1** | Open-source, speaker ID |
| **LLM** | **GPT-4o** / **Claude 3.5 Sonnet** | Best structured output + reasoning |
| **Structured Output** | **Instructor** (Pydantic) | Validation, retries, type safety |
| **Jira** | **atlassian-python-api** | Bulk create, link, search |
| **Slack** | **slack-sdk** (WebClient) | Block Kit, async support |
| **State/Queue** | **PostgreSQL + Redis** | Durability, idempotency, scheduling |
| **Deploy** | **Docker + Kubernetes** / **Cloud Run** | Scalability, GPU for Whisper |

---

## LangGraph Workflow Definition

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated, List
from operator import add

class PipelineState(TypedDict):
    meeting_id: str
    calendar_event: dict
    raw_audio_path: str
    diarized_transcript: dict
    meeting_report: MeetingReport
    jira_ticket_keys: List[str]
    slack_ts: str
    errors: Annotated[List[str], add]
    retry_count: int

# Nodes
def fetch_meeting(state): ...
def join_and_record(state): ...
def transcribe_and_diarize(state): ...
def extract_intelligence(state): ...
def create_jira_tickets(state): ...
def post_to_slack(state): ...
def handle_error(state): ...

# Graph
g = StateGraph(PipelineState)
g.add_node("fetch", fetch_meeting)
g.add_node("record", join_and_record)
g.add_node("transcribe", transcribe_and_diarize)
g.add_node("extract", extract_intelligence)
g.add_node("jira", create_jira_tickets)
g.add_node("slack", post_to_slack)
g.add_node("error", handle_error)

g.set_entry_point("fetch")
g.add_edge("fetch", "record")
g.add_edge("record", "transcribe")
g.add_edge("transcribe", "extract")
g.add_edge("extract", "jira")
g.add_edge("jira", "slack")
g.add_edge("slack", END)

# Error handling
g.add_conditional_edges("record", lambda s: "error" if s["errors"] else "transcribe")
g.add_conditional_edges("transcribe", lambda s: "error" if s["errors"] else "extract")
g.add_conditional_edges("extract", lambda s: "error" if s["errors"] else "jira")
g.add_conditional_edges("jira", lambda s: "error" if s["errors"] else "slack")
g.add_edge("error", END)

# Checkpointing for HITL / resume
app = g.compile(checkpointer=PostgresSaver(conn))
```

---

## Deployment & Operations

### Environment Variables
```bash
# Google
GOOGLE_CALENDAR_CREDENTIALS_JSON
GOOGLE_MEET_BOT_EMAIL

# Recall.ai (or self-hosted)
RECALL_API_KEY
RECALL_BOT_NAME

# Transcription
WHISPER_MODEL_PATH  # or ASSEMBLYAI_API_KEY
PYANNOTE_HF_TOKEN

# LLM
OPENAI_API_KEY  # or ANTHROPIC_API_KEY

# Jira
JIRA_URL
JIRA_EMAIL
JIRA_API_TOKEN
JIRA_PROJECT_KEY

# Slack
SLACK_BOT_TOKEN
SLACK_CHANNEL_ID  # resolved from #technical-update

# Infra
POSTGRES_DSN
REDIS_URL
```

### Monitoring & Observability
| Metric | Tool | Alert Threshold |
|--------|------|-----------------|
| Pipeline latency (end-to-end) | LangSmith + Prometheus | > 10 min |
| Transcription WER | Custom eval set | > 15% |
| Action item precision | Human eval sample | < 80% |
| Jira creation failure rate | Jira API errors | > 5% |
| Slack post failure | Slack API errors | > 1% |

### Idempotency & Retry
- **Meeting ID** as idempotency key across all stages
- **PostgreSQL** state store with `status` enum: `pending → recording → transcribing → extracting → jira_sync → slack_posted → completed`
- **Exponential backoff** on external API calls (max 3 retries)
- **Dead letter queue** for failed meetings → manual review

---

## Security & Compliance

| Concern | Mitigation |
|---------|------------|
| **Audio data privacy** | Self-hosted Whisper; no audio leaves VPC |
| **PII in transcripts** | Redaction pipeline (spaCy + custom patterns) before LLM |
| **Jira/Slack tokens** | Vault/Secret Manager; rotation policy |
| **Attendee consent** | Calendar invite includes recording notice; bot announces at join |
| **Data retention** | Configurable TTL (default 90 days); GDPR delete endpoint |

---

## Implementation Roadmap

| Phase | Deliverable | Timeline |
|-------|-------------|----------|
| **0** | PoC: Manual audio → transcript → summary → Jira → Slack | Week 1 |
| **1** | Automated Calendar trigger + Recall.ai bot join | Week 2 |
| **2** | Full LangGraph pipeline with checkpointing | Week 3 |
| **3** | Speaker identification (email mapping from Calendar) | Week 4 |
| **4** | Production hardening: monitoring, retries, DLQ | Week 5 |
| **5** | Self-hosted Whisper + pyannote (cost optimization) | Week 6 |

---

## Code Repository Structure

```
meet-intelligence/
├── .github/workflows/          # CI/CD
├── docker/
│   ├── Dockerfile.bot          # Meet bot (Playwright)
│   ├── Dockerfile.transcribe   # Whisper + pyannote (GPU)
│   ├── Dockerfile.api          # FastAPI orchestrator
│   └── docker-compose.yml
├── src/
│   ├── meet_intelligence/
│   │   ├── __init__.py
│   │   ├── config.py           # Pydantic Settings
│   │   ├── models.py           # Pydantic schemas (MeetingReport, ActionItem, etc.)
│   │   ├── calendar.py         # Google Calendar watch/poll
│   │   ├── bot/
│   │   │   ├── recall_client.py
│   │   │   └── playwright_bot.py
│   │   ├── transcription/
│   │   │   ├── whisper_asr.py
│   │   │   ├── diarization.py
│   │   │   └── merge.py
│   │   ├── extraction/
│   │   │   ├── prompts.py
│   │   │   ├── llm_chain.py
│   │   │   └── validator.py
│   │   ├── jira/
│   │   │   ├── client.py
│   │   │   ├── mapper.py
│   │   │   └── bulk.py
│   │   ├── slack/
│   │   │   ├── client.py
│   │   │   └── blocks.py
│   │   ├── pipeline/
│   │   │   ├── state.py
│   │   │   ├── nodes.py
│   │   │   └── graph.py
│   │   └── storage/
│   │       ├── postgres.py
│   │       └── redis_queue.py
│   └── main.py                 # Entry point
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── pyproject.toml
├── README.md
└── .env.example
```

---

## Next Steps

1. **Validate Calendar → Meet bot integration** with a test meeting
2. **Benchmark Whisper + pyannote** on sample meeting audio (accuracy, latency)
3. **Prototype LLM extraction** with 3-5 real transcripts; tune prompt + schema
4. **Set up Jira project** with custom fields for meeting linkage
5. **Create Slack app** and install to `#technical-update`
6. **Spin up LangGraph + Postgres** for stateful orchestration

---

*Generated from research synthesis — all claims traceable to verified sources in research artifacts.*