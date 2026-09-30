import { readFileSync } from "node:fs";
import pg from "pg";
import { embed } from "@ternlight/base";

const db = new pg.Client({ connectionString: process.env.DB });
await db.connect();

// Corpus: non-DM thread docs (the RAG unit the comms MCP searches).
const { rows: docs } = await db.query(`
  select td.id, td.text_plain from thread_docs td
  join channels c on c.origin = td.origin and c.native_id = td.channel_id
  where c.kind not in ('im','mpim') and coalesce(td.text_plain,'') <> ''`);
const docIds = new Set(docs.map((d) => d.id));

// Ground truth: canon decisions + incidents; relevant = thread docs containing their cited messages.
const canon = JSON.parse(readFileSync(process.env.CANON, "utf8"));
const items = [...canon.decisions, ...canon.incidents].filter((x) => x.sources?.length);
const cited = [...new Set(items.flatMap((x) => x.sources))];
const { rows: map } = await db.query(
  `select id, case when thread_id is not null then 'slack:' || channel_id || ':' || thread_id else id end as doc
   from messages where id = any($1)`, [cited]);
const toDoc = new Map(map.map((r) => [r.id, r.doc]));
const queries = items
  .map((x) => ({ q: x.title, rel: new Set(x.sources.map((s) => toDoc.get(s)).filter((d) => d && docIds.has(d))) }))
  .filter((x) => x.rel.size > 0);
console.log(`corpus ${docs.length} threads · ${queries.length} queries (canon decisions+incidents with resolvable citations)`);

const STOP = new Set("a an and are as at be by for from has have how i in into is it its of on or our so that the their this to was we were what when which who why will with".split(" "));
const kw = (q) => [...new Set(q.toLowerCase().split(/[^a-z0-9.-]+/).filter((t) => t.length > 1 && !STOP.has(t)))];

async function fts(q, or) {
  const query = or ? kw(q).join(" or ") : q;
  const { rows } = await db.query(
    `select td.id from thread_docs td join channels c on c.origin=td.origin and c.native_id=td.channel_id
     where c.kind not in ('im','mpim') and td.tsv @@ websearch_to_tsquery('english',$1)
     order by ts_rank_cd(td.tsv, websearch_to_tsquery('english',$1)) desc limit 10`, [query]);
  return rows.map((r) => r.id);
}

// Ternlight indexes
const dot = (a, b) => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i] * b[i]; return s; };
let t0 = Date.now();
const head = docs.map((d) => ({ id: d.id, v: embed(d.text_plain.slice(0, 900)) }));
const headMs = Date.now() - t0;
t0 = Date.now();
const chunks = [];
for (const d of docs) {
  const w = d.text_plain.split(/\s+/);
  for (let i = 0, n = 0; i < w.length && n < 40; i += 70, n++) chunks.push({ id: d.id, v: embed(w.slice(i, i + 90).join(" ")) });
}
const chunkMs = Date.now() - t0;
console.log(`ternlight: head index ${docs.length} embeds in ${headMs} ms (${(headMs / docs.length).toFixed(2)} ms/emb); chunked ${chunks.length} embeds in ${(chunkMs / 1000).toFixed(1)} s`);

function tern(index, q) {
  const qv = embed(q);
  const best = new Map();
  for (const e of index) { const s = dot(qv, e.v); if (s > (best.get(e.id) ?? -2)) best.set(e.id, s); }
  return [...best.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10).map(([id]) => id);
}

// Hybrid = RRF(k=10) of FTS-OR and ternlight-chunked (what the gateway would do with ternlight vectors).
function rrf(...lists) {
  const s = new Map();
  for (const l of lists) l.forEach((id, i) => s.set(id, (s.get(id) ?? 0) + 1 / (10 + i + 1)));
  return [...s.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10).map(([id]) => id);
}

const methods = { "FTS (AND)": [], "FTS (OR fallback)": [], "Ternlight head-128": [], "Ternlight chunked": [], "RRF(FTS-OR + Ternlight chunked)": [] };
const score = (ranked, rel) => {
  const i = ranked.findIndex((id) => rel.has(id));
  return { hit: i >= 0 ? 1 : 0, rr: i >= 0 ? 1 / (i + 1) : 0 };
};
t0 = Date.now();
for (const { q, rel } of queries) {
  const and = await fts(q, false), or = await fts(q, true);
  const h = tern(head, q), c = tern(chunks, q);
  methods["FTS (AND)"].push(score(and, rel));
  methods["FTS (OR fallback)"].push(score(or, rel));
  methods["Ternlight head-128"].push(score(h, rel));
  methods["Ternlight chunked"].push(score(c, rel));
  methods["RRF(FTS-OR + Ternlight chunked)"].push(score(rrf(or, c), rel));
}
console.log(`evaluated in ${((Date.now() - t0) / 1000).toFixed(1)} s\n`);
console.log("method".padEnd(34), "hit@10".padStart(8), "MRR@10".padStart(8));
for (const [m, r] of Object.entries(methods)) {
  const hit = r.reduce((a, x) => a + x.hit, 0) / r.length, mrr = r.reduce((a, x) => a + x.rr, 0) / r.length;
  console.log(m.padEnd(34), (hit * 100).toFixed(1).padStart(7) + "%", mrr.toFixed(3).padStart(8));
}
await db.end();
