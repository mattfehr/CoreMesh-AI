# Runtime source package

The <code>src</code> package separates the public HTTP composition root from
feature libraries:

| Module or package | Integration state |
| --- | --- |
| <code>main.py</code> | FastAPI app with health, extraction plus opt-in RAG indexing, chat, restricted unified execution, and read-only forensic trace routes. |
| <code>config.py</code> | Import-time typed settings, including provider and hermetic-mode switches, shared by all subsystems. |
| [chat](chat/README.md) | Minimal OpenAI-shaped chat completions helper for gateway/CI. |
| [ingestion](ingestion/README.md) | Mounted on FastAPI and reused by agents. |
| [rag](rag/README.md) | Python API used by RAG execution mode. |
| [sql_engine](sql_engine/README.md) | Python API used by text-to-SQL execution mode. |
| [agents](agents/README.md) | Supervisor library used by the unified execution route. |
| [arbitration](arbitration/README.md) | Python API used by agents; no route. |
| [tracing](tracing/README.md) | OpenTelemetry execution trees, local registry, and root-cause analysis. |

Keep HTTP validation, restricted context projection, and error normalization
in <code>main.py</code>, domain work in its package, and external clients lazy
so liveness and unrelated features do not require every provider. Browser
requests must reach these routes through the Go gateway rather than port 8000.

The package uses absolute imports beginning with <code>src</code>. Run commands
from <code>services-runtime</code> or otherwise put that directory on
<code>PYTHONPATH</code>.
