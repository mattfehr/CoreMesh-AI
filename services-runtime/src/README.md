# Runtime source package

The <code>src</code> package separates the public HTTP composition root from
feature libraries:

| Module or package | Integration state |
| --- | --- |
| <code>main.py</code> | FastAPI app with <code>/health</code>, <code>/v1/ingest</code>, and <code>/v1/chat/completions</code>. |
| <code>config.py</code> | Import-time typed settings shared by all subsystems. |
| [chat](chat/README.md) | Minimal OpenAI-shaped chat completions helper for gateway/CI. |
| [ingestion](ingestion/README.md) | Mounted on FastAPI and reused by agents. |
| [rag](rag/README.md) | Python API only. |
| [sql_engine](sql_engine/README.md) | Python API only. |
| [agents](agents/README.md) | Python API only. |
| [arbitration](arbitration/README.md) | Python API used by agents; no route. |
| [tracing](tracing/README.md) | OpenTelemetry execution trees, local registry, and root-cause analysis. |

Keep HTTP validation and error normalization in <code>main.py</code>, domain
work in its package, and external clients lazy so liveness and unrelated
features do not require every provider.

The package uses absolute imports beginning with <code>src</code>. Run commands
from <code>services-runtime</code> or otherwise put that directory on
<code>PYTHONPATH</code>.
