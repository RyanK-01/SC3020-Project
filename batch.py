from __future__ import annotations

import argparse
import csv
import os
import statistics
import time


# ---------------------------------------------------------------------------
# Batch mode helpers
# ---------------------------------------------------------------------------
def split_queries(text: str) -> list:
    """
    Split the contents of a .sql file into separate queries.

    Queries are separated by ';'. Comments starting with '--' are removed,
    and a ';' inside a quoted string does not end a query.

    Args:
        text: the contents of the .sql file.

    Returns:
        list: the non-empty queries, without the trailing ';'.
    """
    queries, current = [], []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            current.append(ch)
            if ch == "'":
                in_string = False
        elif ch == "'":
            in_string = True
            current.append(ch)
        elif text.startswith("--", i):               # comment: skip to the end of the line
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif ch == ";":
            queries.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    queries.append("".join(current))                  # last query may have no ';'
    return [q.strip() for q in queries if q.strip()]


def summarize(rows: list) -> dict:
    """
    Compute summary statistics over the results of a batch run.

    Args:
        rows: one dict per query, with the same keys as CSV_COLUMNS and a
            numeric "rate".

    Returns:
        dict: number of queries, successes and failures, number of identical
        plans, mean / median / min / max difference rate (None if no query
        succeeded), and for each difference kind the number of queries that
        have it ("kind_queries") and its total occurrences ("kind_total").
    """
    ok = [r for r in rows if not r["error"]]
    rates = [r["rate"] for r in ok]
    kind_queries: dict = {}     # kind -> number of queries that have it
    kind_total: dict = {}       # kind -> total occurrences
    for r in ok:
        kinds = [k for k in r["difference_kinds"].split("; ") if k]
        for k in kinds:
            kind_total[k] = kind_total.get(k, 0) + 1
        for k in set(kinds):
            kind_queries[k] = kind_queries.get(k, 0) + 1
    return {
        "queries": len(rows),
        "succeeded": len(ok),
        "failed": len(rows) - len(ok),
        "identical": sum(1 for r in ok if r["rate"] == 0),
        "mean_rate": statistics.mean(rates) if rates else None,
        "median_rate": statistics.median(rates) if rates else None,
        "min_rate": min(rates) if rates else None,
        "max_rate": max(rates) if rates else None,
        "kind_queries": kind_queries,
        "kind_total": kind_total,
    }


def format_summary(summary: dict) -> str:
    """
    Format a batch summary as readable text.

    Args:
        summary: the dict returned by summarize().

    Returns:
        str: a multi-line report, with difference kinds listed from the most
        to the least common.
    """
    lines = [f"Queries: {summary['queries']}  (succeeded {summary['succeeded']}, "
             f"failed {summary['failed']})"]
    if summary["succeeded"]:
        lines += [
            f"Identical plans (rate 0): {summary['identical']} of {summary['succeeded']}",
            f"Difference rate: mean {summary['mean_rate']:.1%}, "
            f"median {summary['median_rate']:.1%}, "
            f"min {summary['min_rate']:.1%}, max {summary['max_rate']:.1%}",
            "Kinds of difference (queries that have it / total occurrences):",
        ]
        for kind in sorted(summary["kind_queries"], key=lambda k: -summary["kind_queries"][k]):
            lines.append(f"  {kind:<20} {summary['kind_queries'][kind]:>3} / "
                         f"{summary['kind_total'][kind]}")
        if not summary["kind_queries"]:
            lines.append("  (none)")
    return "\n".join(lines)


CSV_COLUMNS = ["id", "query", "pg_nodes", "llm_nodes", "matched", "changed",
               "missing", "extra", "rate", "difference_kinds", "differences", "error"]


# ---------------------------------------------------------------------------
# Batch run
# ---------------------------------------------------------------------------
def run_batch(args) -> int:
    """
    Compare the PostgreSQL and LLM plans of every query in a .sql file.

    A query that fails is recorded with its error and the run continues.
    The per-query results are written to a CSV file and a summary is printed.

    Args:
        args: parsed command-line arguments (see parse_args()).

    Returns:
        int: exit code, 0 on success or 1 if the file has no queries.
    """
    import comparison
    import preprocessing as pp

    with open(args.batch, encoding="utf-8") as f:
        queries = split_queries(f.read())
    if not queries:
        print("No queries found in", args.batch)
        return 1

    # Command-line options override the environment / default settings
    cfg = pp.DBConfig.from_env()
    for name in ("host", "port", "dbname", "user", "password", "schema"):
        value = getattr(args, name)
        if value:
            setattr(cfg, name, value)
    api_key = args.api_key or os.environ.get("GEMINI_API_KEY", "")

    conn = pp.connect_db(cfg)
    schema = pp.get_schema(conn, cfg.schema)
    print(f"Running {len(queries)} queries from {args.batch} against "
          f"{cfg.dbname} (schema {cfg.schema}).")

    rows = []
    for number, query in enumerate(queries, 1):
        row = {"id": number, "query": " ".join(query.split())[:120], "pg_nodes": "",
               "llm_nodes": "", "matched": "", "changed": "", "missing": "", "extra": "",
               "rate": "", "difference_kinds": "", "differences": "", "error": ""}
        try:
            pg_plan = pp.get_postgres_plan(conn, query, cfg.schema)
            llm_plan = None
            for attempt in range(args.retries + 1):
                started = time.time()
                try:
                    llm_plan = pp.get_llm_plan(query, schema, api_key or None)
                    break
                except pp.LLMError as exc:
                    # HTTP 429 = API rate limit: wait longer after each attempt and retry
                    if "429" in str(exc) and attempt < args.retries:
                        wait = 20 * (attempt + 1)
                        print(f"  [{number}] rate limit, waiting {wait}s ...")
                        time.sleep(wait)
                        continue
                    raise
            result = comparison.compare_plans(pg_plan, llm_plan)
            row.update(
                pg_nodes=sum(1 for _ in pg_plan.walk()),
                llm_nodes=sum(1 for _ in llm_plan.walk()),
                matched=result.matched, changed=result.changed,
                missing=result.missing, extra=result.extra, rate=round(result.rate, 4),
                difference_kinds="; ".join(d.kind for d in result.differences),
                differences=" | ".join(d.text for d in result.differences))
            print(f"  [{number}/{len(queries)}] rate {result.rate:.0%}  "
                  f"({len(result.differences)} differences)")
            # only pause after a real API call since cached replies return almost instantly
            if time.time() - started > 0.2 and number < len(queries):
                time.sleep(args.delay)
        except Exception as exc:                # record the error and continue with the next query
            row["error"] = str(exc).replace("\n", " ")[:300]
            print(f"  [{number}/{len(queries)}] ERROR: {row['error']}")
        rows.append(row)
    conn.close()

    # failed queries have empty rate, use 0.0 so summarize() can process
    numeric = [dict(r, rate=(r["rate"] if r["rate"] != "" else 0.0)) for r in rows]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel reads UTF-8
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print()
    print(format_summary(summarize(numeric)))
    print(f"\nPer-query results written to {args.out}")
    return 0


def parse_args(argv=None):
    """
    Parse the command-line arguments.

    Args:
        argv: list of arguments, or None to use sys.argv.

    Returns:
        argparse.Namespace: the parsed options. Database options that are not
        given are None, and the environment / default values are used instead.
    """
    parser = argparse.ArgumentParser(
        description="Compare PostgreSQL and AI query plans (GUI by default).")
    parser.add_argument("--batch", metavar="FILE.sql",
                        help="run all queries in this file without the GUI")
    parser.add_argument("--out", default="results.csv", help="CSV file for --batch (default results.csv)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="seconds to wait after each AI call (default 2)")
    parser.add_argument("--retries", type=int, default=3,
                        help="retries when the AI API reports a rate limit (default 3)")
    for name in ("host", "port", "dbname", "user", "password", "schema"):
        parser.add_argument(f"--{name}", help=f"database {name} (default: environment / built-in)")
    parser.add_argument("--api-key", help="LLM API key (default: GEMINI_API_KEY)")
    return parser.parse_args(argv)
