# AI Agents

## Architect — Iterative Software Design Agent

**Spec:** [`.opencode/agents/architect.md`](.opencode/agents/architect.md)

### Purpose

Agent for iterative co-design of software architecture with the user.
Designed for **local LLMs with limited context** (64K tokens): Qwen 2.5, DeepSeek-R1,
Mistral, Llama 3, Phi-3, and similar models running on Ollama / LM Studio / vLLM.

### How It Works

```
┌─────────────────────────────────────────────────────────┐
│                    64K Context Window                    │
├──────────┬──────────┬────────────┬──────────┬───────────┤
│  System  │ ONTOLOGY │   PLAN.md  │  Source  │ Response  │
│  Prompt  │   .md    │  (current  │  files   │           │
│  ~4K     │  ≤12K    │  section)  │ (on-     │  ≤8K      │
│          │          │  ≤12K      │ demand)  │           │
│          │          │            │ ≤16K     │           │
└──────────┴──────────┴────────────┴──────────┴───────────┘
         ↑                    ↑
    "Long-term         "Working memory"
     memory"         (loaded per-phase)
```

The agent compensates for the small context window by:

1. **External memory in files** — all decisions, domain knowledge, and progress
   are stored in `docs/ONTOLOGY.md` and `docs/PLAN.md`, not in conversation
   history.

2. **Phase-based workflow** — work is split into 6 phases (0–5), each small
   enough to fit in context. Only data relevant to the current phase is loaded.

3. **Read-before-speak protocol** — every turn starts by re-reading the doc
   files, restoring full project context regardless of conversation history.

4. **Context overflow recovery** — when context approaches capacity, the agent
   saves a session summary to `PLAN.md` and asks to start a fresh conversation.

### Workflow Phases

| Phase | Name | Input | Output |
|-------|------|-------|--------|
| 0 | Bootstrap | User's project idea | `ONTOLOGY.md` + `PLAN.md` created |
| 1 | Domain Decomposition | User answers | Entities, relations, invariants, ER diagram |
| 2 | Architecture Decisions | Trade-off discussions | ADRs in `PLAN.md` |
| 3 | Component Design | Accepted ADRs | Module interfaces, data structures, diagrams |
| 4 | Implementation Plan | Component specs | Ordered increments with file-level roadmap |
| 5 | Guided Implementation | Roadmap | Code, tests, progress updates |

### Usage with OpenCode

```bash
# 1. Start opencode with the architect agent
opencode --agent .opencode/agents/architect.md

# 2. Or set in config (~/.config/opencode/config.toml)
# [agents.architect]
# path = ".opencode/agents/architect.md"

# 3. Then in opencode:
# /agent architect
```

### Usage with Other Tools

The agent spec is a standard Markdown system prompt. It works with any tool
that accepts a system prompt:

```bash
# Ollama
ollama run qwen2.5:32b --system "$(cat .opencode/agents/architect.md)"

# LM Studio — paste into System Prompt field

# aider
aider --system-prompt .opencode/agents/architect.md

# Continue.dev — add to config.json as system message
```

### Files Managed by This Agent

| File | Purpose | Token Budget |
|------|---------|--------------|
| `docs/ONTOLOGY.md` | Domain knowledge, glossary, entity model | ≤12 000 |
| `docs/PLAN.md` | ADRs, components, roadmap, session log | ≤12 000 |
| `docs/ONTOLOGY_detail_*.md` | Overflow sections (created if needed) | ≤8 000 each |

### Tips for Small Models

- **Keep questions focused.** Don't ask "design the whole system" — ask
  "what entities exist in the domain?"
- **One phase per session.** If your model struggles, complete one phase,
  then start a new conversation. The agent restores state from files.
- **Review file edits.** Weaker models may produce invalid YAML/Mermaid.
  Check the doc files after each phase.
- **Use 32B+ models for Phase 2–3.** Architecture decisions and interface
  design benefit from larger models. Phases 0–1 and 4–5 work fine on 7B–14B.
