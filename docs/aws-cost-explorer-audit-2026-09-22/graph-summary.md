# Cost Explorer graph audit

Built with the actual local graphify runtime (0.9.65), following the installed graphify skill (0.9.47). This focused, sanitized graph maps live AWS controls to source code, tests and documented capability boundaries. It does not establish production reconciliation or live AWS API acceptance.

## Scope and control coverage

- 31 supported snapshot files: 19 code files and 12 documents/templates; approximately 33,361 words. One additional CSS file is fingerprinted but unsupported by graphify extraction.
- All **66 live control IDs** are distinct searchable graph nodes: W14, T8, G4, F9, A6, V9, C9 and L7. Option labels and inspected conditional behavior remain attached to the evidence. Dynamic customer account, service, tag, category and resource values were excluded.
- Final sources include the report library and chooser, scoped workspace history and AWS handoff, keyed filters, cost/usage measures, comparison, tests, and implementation coverage. The snapshot matches current scoped repository files byte-for-byte except the two intentionally annotated historical documents.
- Live observations, the pre-change source audit, final implementation coverage, official API notes and historical references retain distinct provenance. The corpus copy redacts the historical customer cost figure.
- Resource grouping, usage-dependent states, billing-view breadth, chart options and separate SP/RI products retain the limitations in the [implementation coverage](implementation-coverage.md). A graph link indicates an evidence or code relationship; it does not assert full AWS parity. Library archive/restore is the intentional recoverable equivalent of deletion; SP/RI types hand off to AWS with separate permissions.

## Graph results

- **490 exported nodes, 1,037 exported edges and 17 named communities.**
- Raw extraction: 445 declared nodes and 1,084 relationships: 1,021 EXTRACTED, 61 INFERRED and 2 AMBIGUOUS. Graphify adds placeholders for unresolved references; the undirected export can collapse relationships.
- Main hubs: `build_report()` (degree 41), `normalize()` (28), `ExplorerControlTests` (24), and `run_query()` (23).
- Cross-community paths connect parameter normalization, report assembly, authorization, durable AWS query cache, workspace controls and library coverage. API-to-UI correspondences lacking direct evidence remain AMBIGUOUS.

| Community | Nodes | Cohesion |
| --- | ---: | ---: |
| Report Contract and Navigation | 76 | 0.05052631578947368 |
| Report Assembly and Query Cache | 70 | 0.06915113871635611 |
| AWS Capabilities and Report Evidence | 64 | 0.050595238095238096 |
| Report Library and Web Endpoints | 60 | 0.07627118644067797 |
| Interactive Filters and Charts | 47 | 0.06845513413506013 |
| Workspace and Library Coverage | 34 | 0.09090909090909091 |
| Explorer Contract Tests | 24 | 0.12681159420289856 |
| Library and Permission Tests | 22 | 0.09090909090909091 |
| Date and Keyed Filter Tests | 16 | 0.125 |
| Time Controls and Source Audit | 15 | 0.20952380952380953 |
| Chart and Breakdown Coverage | 12 | 0.2878787878787879 |
| Customer Authorization | 10 | 0.2 |
| Workspace Navigation Tests | 10 | 0.24444444444444444 |
| Workspace Routes and Modules | 9 | 0.2222222222222222 |
| Report Scope Resolution | 8 | 0.25 |
| Authorized AWS Handoff | 7 | 0.2857142857142857 |
| Account Ownership and Sources | 6 | 0.3333333333333333 |

## Integrity, validation and usage

- Graph health warning: **150 dangling-endpoint relationships, 0 missing endpoints, 6 self-loops, and 39 repeated endpoint relationships collapsed** in the undirected diagnostic. Most unresolved endpoints arise from imports beyond the focused corpus. These are graph limitations, not verified application dependency failures; the complete raw evidence is retained locally.
- Validated all 66 inventory IDs against distinct exported nodes, every semantic endpoint against declared nodes, every exported edge endpoint, community labels, source fingerprints, report generation and HTML export. A graph-vocabulary query completed during the initial build. Application checks are recorded separately in the implementation audit; graph validation does not replace them.
- Initial semantic extraction used one existing host-session sibling agent; the final changed-document refresh used host-agent review. No external semantic-provider API call was made. Actual input/output token usage and monetary cost are **unknown** because the tools expose no counters. Compatibility zero fields are explicitly placeholders.
- Graphify benchmark estimates: 24,500 words / approximately 32,666 naive tokens, approximately 7,424 tokens per query, 4.4× reduction. Its corpus estimator differs from detection. These are rough estimates, not measured session usage or cost savings.

## Local outputs

- `graphify-out/graph.html`: interactive graph.
- `graphify-out/graph.json`: graph data with provenance and control metadata.
- `graphify-out/GRAPH_REPORT.md`: audit, hubs, bridges and suggested queries.
- `graphify-out/corpus-manifest.json`: frozen source paths, SHA-256 fingerprints and scope.
- `graphify-out/extraction-audit.json`, `diagnostics.json` and `diagnostics.txt`: raw relationships and health warnings.
- `graphify-out/cost.json`: usage ledger distinguishing unknown actual usage from estimates.

The local output directory is excluded from version control and production artifacts. This tracked summary accompanies the [live inventory](live-ui-inventory.md), [pre-change audit](current-implementation.md), [API capabilities](aws-api-capabilities.md) and [implementation coverage](implementation-coverage.md). Source locations resolve against the frozen corpus and may shift after subsequent edits.
