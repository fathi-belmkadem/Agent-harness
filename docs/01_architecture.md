# Architecture

The system as it stands at the end of week 3. Two diagrams: the components
and how they connect, then one turn as it actually executes. Both render on
GitHub; the source is Mermaid, so a change to the code is a change to a few
lines here.

## Components

```mermaid
flowchart TB
    User([user / terminal])

    subgraph Harness["agent-harness (one container)"]
        direction TB
        CLI[cli.py · startup.py<br/>REPL · one-shot · startup wiring]
        Skills["skills.py<br/>SKILL.md files → prompt index · skill__read"]

        subgraph Agent["agent/"]
            Loop["AgentLoop<br/>the facade: one turn"]
            Phases["phases/<br/>fit_context · recall · response_check · act · finalize"]
            Ctx["Context<br/>the conversation"]
            Sub["SubagentProvider<br/>delegate__researcher · delegate__writer"]
        end

        subgraph Tools["tools/"]
            Reg["ToolRegistry<br/>one flat namespace"]
            Pol["Policy<br/>allow · deny · ask"]
            FS["FilesystemProvider<br/>sandboxed, no delete"]
            Cl["clarify<br/>one question, ends the turn"]
        end

        subgraph MCP["mcp/"]
            MCPP["MCPProvider<br/>server__tool"]
            MCPC["MCPClient<br/>JSON-RPC handshake"]
        end

        subgraph Memory["memory/"]
            Facts[("SQLiteMemory<br/>facts · FTS5 · vectors")]
            Epi[("EpisodicArchive<br/>every turn · FTS5 · vectors")]
            Comp["compaction<br/>token budget"]
            Cons["consolidation<br/>archive unused"]
            Emb["EmbeddingClient"]
        end

        subgraph Obs["observability/"]
            Emit["TraceEmitter<br/>run_id · parent_run_id · redaction"]
            Tr[("Transcript<br/>.transcripts/*.jsonl")]
            Met["MetricsSink<br/>/metrics :9100"]
        end
    end

    LLM[("innkube proxy<br/>qwen3-next-80b · octen-embedding-8b")]
    Redis[("Redis<br/>live conversation")]
    Srv["MCP servers<br/>everything · brave-search"]
    Prom[("Prometheus")]
    Graf["Grafana<br/>:3000"]

    User --> CLI --> Loop
    Loop --> Phases
    Phases --> Ctx
    Phases -- "chat(messages, tools)" --> LLM
    Phases -- "dispatch" --> Pol --> Reg
    Reg --> FS
    Reg --> MCPP --> MCPC --> Srv
    Reg --> Sub -- "child AgentLoop<br/>own prompt · tool subset · own policy" --> Loop
    Phases -- "recall / store" --> Facts
    Phases -- "archive turn" --> Epi
    Phases --> Comp
    Phases --> Cons
    Facts --> Emb --> LLM
    Epi --> Emb
    Phases -- "save / load" --> Redis
    Phases -- "emit" --> Emit
    Emit --> Tr
    Emit --> Met
    Prom -- "docker service discovery, 5s" --> Met
    Graf --> Prom
```

What changed this week, in the diagram: the `Policy` box in front of the
registry, the `SubagentProvider` that builds a second `AgentLoop`, and the
`MetricsSink` beside the transcript with Prometheus and Grafana behind it.
None of the three required a change to `AgentLoop` or to any phase; each
plugs into a seam that already existed — the one dispatch function, the
provider protocol, the emitter.

## One turn

```mermaid
sequenceDiagram
    participant U as user
    participant L as AgentLoop
    participant P as Policy
    participant R as ToolRegistry
    participant C as child AgentLoop
    participant M as LLM
    participant E as TraceEmitter

    U->>L: message
    L->>L: fit_context (compact if over budget)
    L->>L: recall (search facts, prefix onto the message)
    loop until answer, cap, or budget
        L->>M: chat(system + conversation + recall, tool schemas)
        M-->>L: reply
        alt tool calls
            L->>L: response_check (repair or drop malformed calls)
            L->>P: decide(tool)
            alt allow, or ask answered yes
                P->>R: dispatch
                opt tool is delegate__role
                    R->>C: run(fresh Context with the task only)
                    C->>M: its own chat calls
                    C->>P: its own policy per call
                    C-->>R: final message
                end
                R-->>L: ToolResult (ok or error text)
            else deny, or ask with no terminal
                P-->>L: ToolResult(ok=False, "denied")
            end
            L->>E: act, permission_decision, delegated
        else text only
            L->>L: blank? unbacked claim? → fail or re-ask once with tool_choice
            L->>E: done
        end
    end
    L->>L: finalize (save to Redis, archive turn, consolidate)
    L-->>U: answer
```

## Where each component is documented

| component | doc |
|---|---|
| the loop and its phases | [02_react-loop.md](02_react-loop.md) |
| context and compaction | [03_context.md](03_context.md) |
| tools, registry, sandbox | [04_tools.md](04_tools.md) |
| MCP | [05_mcp.md](05_mcp.md) |
| memory: facts, episodic, consolidation | [06_memory.md](06_memory.md) |
| permissions | [07_permissions.md](07_permissions.md) |
| sub-agents | [08_subagents.md](08_subagents.md) |
| observability | [10_observability.md](10_observability.md) |

## Deployment

```mermaid
flowchart LR
    subgraph Compose["docker compose"]
        H["agent-harness<br/>python:3.12-slim-bookworm<br/>/metrics :9100"]
        Rd[("redis:7")]
        Pr[("prometheus<br/>:9090")]
        Gr["grafana<br/>:3000"]
    end
    Sec["/.secrets/agent_access_token.txt<br/>mounted as a Docker secret"] --> H
    Env["/.env<br/>BRAVE_API_KEY"] -.-> H
    WS["./workspace<br/>./.memory<br/>./.transcripts"] <-->|bind mounts| H
    H --> Rd
    Pr -- "docker.sock: discover by compose label" --> H
    Gr --> Pr
```

`docker compose run --rm --name agent-harness agent-harness` starts all four. The token never
appears in a shell command or an environment listing; the three bind mounts
are what make output, memory and traces visible on the host.
