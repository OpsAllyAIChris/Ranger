# Graph Report - Ranger  (2026-09-08)

## Corpus Check
- Corpus is ~4,775 words - fits in a single context window. You may not need a graph.

## Summary
- 63 nodes · 97 edges · 8 communities
- Extraction: 87% EXTRACTED · 13% INFERRED · 0% AMBIGUOUS · INFERRED: 13 edges (avg confidence: 0.83)
- Token cost: 75,291 input · 0 output

## Community Hubs (Navigation)
- Proactive Heartbeat and Scheduling
- Voice I/O and Latency
- Tool Registry and Safety Rails
- Swappable Seams and Visibility
- Graphify Graph Commands
- Durable Long-Term Memory
- Text-First Build Discipline
- Interview and Spec Foundation

## God Nodes (most connected - your core abstractions)
1. `Tier 3 - The Ears and Mouth (voice in, voice out)` - 10 edges
2. `Tier 1 - The Brain (text conversation loop)` - 9 edges
3. `Tool Registry` - 9 edges
4. `Long-Term Memory Store` - 9 edges
5. `Tier 5 - The Heartbeat (proactivity)` - 9 edges
6. `Hard Confirmation Gate` - 8 edges
7. `Graphify Knowledge Graph` - 7 edges
8. `Tier 0 - Interview First` - 7 edges
9. `Tier 6 - The Rails (safety, confirmation, configuration)` - 6 edges
10. `Model Provider Seam` - 5 edges

## Surprising Connections (you probably didn't know these)
- `Long-Term Memory Store` --semantically_similar_to--> `Graphify Knowledge Graph`  [INFERRED] [semantically similar]
  start-here.md → CLAUDE.md

## Hyperedges (group relationships)
- **Five-Part Voice Assistant Architecture (brain, hands, ears/mouth, memory, heartbeat)** — start_here_tier_1_brain, start_here_tier_2_hands, start_here_tier_3_ears_and_mouth, start_here_tier_4_memory, start_here_tier_5_heartbeat, start_here_shared_agent_core [EXTRACTED 1.00]
- **Thin Provider Seam Pattern (model, STT, TTS)** — start_here_provider_seam, start_here_transcription_seam, start_here_tts_seam [INFERRED 0.95]
- **Tier 6 Safety Posture** — start_here_confirmation_gate, start_here_content_as_data, start_here_per_action_confirmation, start_here_config_file, start_here_audit_trail, start_here_kill_switch, start_here_never_without_asking_list [EXTRACTED 1.00]

## Communities (8 total, 0 thin omitted)

### Community 0 - "Proactive Heartbeat and Scheduling"
Cohesion: 0.18
Nodes (12): Always-On Host for the Heartbeat, Background Heartbeat Loop, Catch-Up-on-Return Delivery, Configuration File Over Hardcoded Values, Kill Switch, Never Block Forever Waiting on a Human, No Overlapping Check Runs, Quiet by Default (+4 more)

### Community 1 - "Voice I/O and Latency"
Cohesion: 0.27
Nodes (11): Interruptible Speech (barge-in), ElevenLabs (text-to-speech), Latency Is the Whole Experience, Push-to-Talk Capture, One Shared Agent Core, Many Ways In and Out, Streaming Model Replies, Tier 1 - The Brain (text conversation loop), Tier 2 - The Hands (tool calling) (+3 more)

### Community 2 - "Tool Registry and Safety Rails"
Cohesion: 0.25
Nodes (11): Hard Confirmation Gate, Treat Ingested Content as Data, Not Commands, Stored Memory Is Data, Not Instructions, Graceful Model Failure Handling, Confirmation Is Per-Action and Does Not Generalize, Per-Tool Safety Classification, Tier 6 - The Rails (safety, confirmation, configuration), Describe Tools for a Reader, Not a Compiler (+3 more)

### Community 3 - "Swappable Seams and Visibility"
Cohesion: 0.25
Nodes (8): Visible Audit Trail, Deepgram (speech-to-text), Dismissible Surfaced Items, Human-Readable, User-Editable Memory, Model Provider Seam, Secrets Out of Code, Transcription Seam, A Face (visual interface)

### Community 4 - "Graphify Graph Commands"
Cohesion: 0.38
Nodes (7): GRAPH_REPORT.md, graphify explain, Graphify Knowledge Graph, graphify path, graphify query, graphify update, Graphify Wiki Index

### Community 5 - "Durable Long-Term Memory"
Cohesion: 0.40
Nodes (5): Durable Schedule State, Long-Term Memory Store, One Fact Per Entry, Selective Memory Loading, Tier 4 - The Memory (durable across restarts)

### Community 6 - "Text-First Build Discipline"
Cohesion: 0.40
Nodes (5): Keep the Text Path Alive Forever, Get the Brain Working in Text Before Audio, Tier-by-Tier Incremental Build Discipline, Trillion (reference assistant name), Voice-First AI Assistant Harness

### Community 7 - "Interview and Spec Foundation"
Cohesion: 0.67
Nodes (4): AGENT.md Spec File, Never-Without-Asking List, System Prompt, Tier 0 - Interview First

## Knowledge Gaps
- **7 isolated node(s):** `graphify path`, `graphify explain`, `graphify update`, `Trillion (reference assistant name)`, `Typed, Validated Tool Inputs` (+2 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 12 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Long-Term Memory Store` connect `Durable Long-Term Memory` to `Tool Registry and Safety Rails`, `Swappable Seams and Visibility`, `Graphify Graph Commands`, `Interview and Spec Foundation`?**
  _High betweenness centrality (0.320) - this node is a cross-community bridge._
- **Why does `Tier 3 - The Ears and Mouth (voice in, voice out)` connect `Voice I/O and Latency` to `Swappable Seams and Visibility`, `Text-First Build Discipline`?**
  _High betweenness centrality (0.257) - this node is a cross-community bridge._
- **Why does `Tool Registry` connect `Tool Registry and Safety Rails` to `Proactive Heartbeat and Scheduling`, `Voice I/O and Latency`, `Durable Long-Term Memory`, `Interview and Spec Foundation`?**
  _High betweenness centrality (0.222) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `Long-Term Memory Store` (e.g. with `Durable Schedule State` and `Graphify Knowledge Graph`) actually correct?**
  _`Long-Term Memory Store` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `graphify path`, `graphify explain`, `graphify update` to the rest of the system?**
  _7 weakly-connected nodes found - possible documentation gaps or missing edges._