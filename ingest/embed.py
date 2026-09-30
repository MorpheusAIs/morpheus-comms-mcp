"""Fill thread_docs.embedding (vector 1024) where null. Resumable: commits per batch.

    EMBED_API_KEY=... python -m ingest.embed [--limit N] [--origin slack]

Env: EMBED_BASE_URL (default https://api.mordiem.com/api/v1), EMBED_API_KEY,
EMBED_MODEL (default text-embedding-bge-m3, 1024-d). OpenAI-compatible /embeddings.
Exit 2 on no-credit (402, or 429 mentioning credit/quota/balance), 1 on other failures.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from ingest.common import _dotenv, database_url

DIM = 1024
NO_CREDIT_WORDS = ("credit", "quota", "balance", "insufficient", "billing", "payment")


class NoCredit(Exception):
    pass


def config() -> dict:
    env = {**_dotenv(), **os.environ}
    return {
        "base": env.get("EMBED_BASE_URL", "https://api.mordiem.com/api/v1").rstrip("/"),
        "key": env.get("EMBED_API_KEY", ""),
        "model": env.get("EMBED_MODEL", "text-embedding-bge-m3"),
    }


def embed(texts: list[str], cfg: dict, client: httpx.Client, retries: int = 5) -> list[list[float]]:
    headers = {"Authorization": f"Bearer {cfg['key']}"} if cfg["key"] else {}
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            r = client.post(f"{cfg['base']}/embeddings", headers=headers,
                            json={"model": cfg["model"], "input": texts})
        except httpx.HTTPError as e:
            err = f"{type(e).__name__}: {e}"
        else:
            if r.status_code == 200:
                data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                vecs = [d["embedding"] for d in data]
                if len(vecs) != len(texts) or any(len(v) != DIM for v in vecs):
                    raise RuntimeError(
                        f"bad embedding response: {len(vecs)} vectors, "
                        f"dims {sorted({len(v) for v in vecs})}, want {DIM}")
                return vecs
            body = r.text[:300]
            if r.status_code == 402 or (
                r.status_code == 429 and any(w in body.lower() for w in NO_CREDIT_WORDS)
            ):
                raise NoCredit(f"HTTP {r.status_code}: {body}")
            if r.status_code in (401, 403):
                raise RuntimeError(f"HTTP {r.status_code} (check EMBED_API_KEY): {body}")
            if r.status_code not in (408, 409, 429) and r.status_code < 500:
                raise RuntimeError(f"HTTP {r.status_code}: {body}")
            err = f"HTTP {r.status_code}: {body}"
            ra = r.headers.get("retry-after")
            if ra and ra.isdigit():
                delay = max(delay, float(ra))
        if attempt == retries:
            raise RuntimeError(f"giving up after {retries + 1} tries: {err}")
        print(f"  retry in {delay:.0f}s ({err[:120]})", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise AssertionError("unreachable")


def to_vector(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url")
    ap.add_argument("--origin", choices=("slack", "discord"))
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--limit", type=int, help="stop after N docs")
    a = ap.parse_args(argv)

    cfg = config()
    if not cfg["key"]:
        print("EMBED_API_KEY is not set", file=sys.stderr)
        return 1

    import psycopg
    done = 0
    with psycopg.connect(database_url(a.database_url)) as conn, \
            httpx.Client(timeout=120) as client:
        where = "embedding is null and coalesce(text_plain, '') <> ''"
        args: list = []
        if a.origin:
            where += " and origin = %s"
            args.append(a.origin)
        total = conn.execute(f"select count(*) from thread_docs where {where}", args).fetchone()[0]
        todo = min(total, a.limit) if a.limit else total
        print(f"{total} docs need embeddings; doing {todo} with {cfg['model']}")
        while done < todo:
            n = min(a.batch_size, todo - done)
            rows = conn.execute(
                f"select id, text_plain from thread_docs where {where} "
                "order by sent_at desc nulls last, id limit %s",
                args + [n],
            ).fetchall()
            if not rows:
                break
            try:
                vecs = embed([t[: a.max_chars] for _, t in rows], cfg, client)
            except NoCredit as e:
                print(f"no credit on embeddings endpoint, stopping ({done} done this run; "
                      f"re-run later to resume): {e}", file=sys.stderr)
                return 2
            except RuntimeError as e:
                print(f"embedding failed ({done} done this run): {e}", file=sys.stderr)
                return 1
            conn.cursor().executemany(
                "update thread_docs set embedding = %s::vector where id = %s",
                [(to_vector(v), i) for (i, _), v in zip(rows, vecs)],
            )
            conn.commit()
            done += len(rows)
            print(f"  {done}/{todo}")
    print(f"embedded {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
