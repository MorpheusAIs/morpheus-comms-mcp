# Embeddings: bge-m3 (mordiem) vs Ternlight

`thread_docs.embedding` is `vector(1024)` for `text-embedding-bge-m3` via mordiem (`ingest/embed.py`). That costs mordiem credit and fails when the daily credit is spent. [Ternlight](https://ternlight.dev/) (MIT) is a free, local alternative: a 7.2 MB WASM sentence-embedding model (BitNet-style ternary weights, distilled from all-MiniLM-L6-v2).

## Ternlight facts

| | |
|---|---|
| Package | `@ternlight/base` 0.1.x (npm), `@ternlight/mini` (256-d) |
| Output | 384-d, L2-normalised Float32Array |
| Max input | 128 tokens (~95 words), silently truncated |
| Languages | English only |
| Runtime | JS/WASM only: Node ≥18, browsers, Workers, Deno, Bun. **No Python package** |
| Status | v0.1, "API may change before v1.0" |

## Benchmark (2026-09-30, `eval/ternlight_eval.mjs`)

- **Corpus:** 3814 non-DM `thread_docs`.
- **Queries:** 465 canon decision/incident titles from mor-archive-fe.
- **Relevant:** the thread docs that contain the messages each entry cites.

| Method | hit@10 | MRR@10 |
|---|---|---|
| FTS, `websearch_to_tsquery` AND (current keyword mode) | 20.9% | 0.201 |
| FTS, keywords OR-ed (gateway fallback) | 45.6% | 0.200 |
| Ternlight, first 128 tokens per thread | 50.5% | 0.330 |
| **Ternlight, 90-word chunks (70-word stride), max-sim per thread** | **69.5%** | **0.475** |
| RRF(k=10) of FTS-OR + Ternlight chunked | 70.5% | 0.394 |

- **Speed on this laptop:** 34 ms/embed on long inputs (not the advertised 5 ms, which assumes short strings). The full chunked index is 7087 vectors and took 5.5 min, single-threaded. Query embedding is about 35 ms.
- **Caveat:** the queries are LLM-written summaries of the same threads, which favours semantic methods. bge-m3 was not scored, because that needs the corpus embedded on mordiem credit. Run the same script with bge-m3 vectors before choosing between the two.

## Adopted: on-device search in mor-archive-fe

The archive is frozen, so mor-archive-fe embeds it **once** with Ternlight (`scripts/build-semantic-index.mts`, which reads `thread_docs` from this Postgres). It ships the int8 index as a static, auth-gated asset, and runs query embedding plus the vector scan **in the browser**. No schema change or sidecar is needed here, and search costs no mordiem credit. See the mor-archive-fe README, section "On-device semantic search".

The notes below still apply if semantic search is ever needed server-side (for example in the MCP tools).

## Verdict

Ternlight works for this archive. It more than triples keyword hit@10 with no API cost and no credit dependency. To adopt it:

1. **Chunks table.** Add `thread_chunks(thread_doc_id, ix, text, embedding vector(384))` with an HNSW cosine index. The 1024-d column stays for bge-m3.
2. **Ingest.** Write a small Node job (Ternlight is JS-only) that chunks each thread (90 words, stride 70, ≤40 chunks) and fills the table.
3. **Query path.** The Python MCP can't run Ternlight, so the Node side has to embed the query. Either:
   - the mor-archive-fe gateway embeds the query and calls a new comms tool, `vector_search(embedding)`; or
   - a Node sidecar in this compose file serves `/embed`.
4. **Ranking.** Weight the semantic list above FTS in hybrid ranking. Plain RRF with FTS-OR lowered MRR (0.475 → 0.394).
