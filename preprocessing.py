from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# 0. Environment variables and defaults
# ---------------------------------------------------------------------------
import dotenv
dotenv.load_dotenv()

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"

# ---------------------------------------------------------------------------
# 1. PlanNode
# ---------------------------------------------------------------------------
@dataclass(eq=False)  # identity-based hashing so nodes can be used as dict keys
class PlanNode:
    """
    Represents one operator in a query execution plan.
    The fields up to partial_mode are filled in by plan_from_dict()
    """
    node_type: str                      # e.g. "Seq Scan", "Hash Join", "Aggregate"
    relation: Optional[str] = None      # table name (scan nodes only)
    index_name: Optional[str] = None    # index used by an Index / Index Only Scan
    join_type: Optional[str] = None     # e.g. "Inner", "Left"
    conditions: Optional[str] = None    # all condition text, for display
    children: list = field(default_factory=list)

    selection: Optional[str] = None     # scan predicates: Filter / Index Cond / Recheck Cond
    join_cond: Optional[str] = None     # join predicates: Hash Cond / Merge Cond / Join Filter / Condition
    partial_mode: Optional[str] = None  # "Partial" or "Finalize" for parallel aggregates

    status: str = "unchanged"           # unchanged | changed | missing | extra | ignored

    def walk(self) -> Iterator["PlanNode"]:
        """
        Traverse this node and all of its descendants in pre-order.

        Yields:
            PlanNode: each node of the subtree, parent before children.
        """
        yield self
        for child in self.children:
            yield from child.walk()

    def tables(self) -> frozenset:
        """
        Return the base tables read anywhere in this subtree.

        Returns:
            frozenset: names of the relations scanned below (and at) this node.
        """
        return frozenset(n.relation for n in self.walk() if n.relation)

    def label(self) -> str:
        """
        Short text shown inside this node's box in the GUI.
        """
        text = self.node_type
        if self.relation:
            text += f"\n[{self.relation}]"
        if self.index_name:
            text += f"\n({self.index_name})"
        return text

def plan_to_text(node: PlanNode, indent: int = 0) -> str:
    """
    Convert a plan tree into an indented, multi-line string.
    """
    line = "  " * indent + node.node_type
    if node.relation:
        line += f" [{node.relation}]"
    if node.index_name:
        line += f" using {node.index_name}"
    if node.conditions:
        line += f"  ({node.conditions})"
    lines = [line]
    for child in node.children:
        lines.append(plan_to_text(child, indent + 1))
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# 2. Normalization: map LLM wording to PostgreSQL node names
# ---------------------------------------------------------------------------
NODE_TYPE_ALIASES = {
    # scans
    "seq scan": "Seq Scan", "sequential scan": "Seq Scan",
    "full table scan": "Seq Scan", "table scan": "Seq Scan",
    "full scan": "Seq Scan",
    "index scan": "Index Scan", "index seek": "Index Scan",
    "index only scan": "Index Only Scan", "covering index scan": "Index Only Scan",
    "bitmap heap scan": "Bitmap Heap Scan", "bitmap index scan": "Bitmap Index Scan",
    # joins
    "hash join": "Hash Join", "hash": "Hash",
    "merge join": "Merge Join", "sort merge join": "Merge Join",
    "sort-merge join": "Merge Join",
    "nested loop": "Nested Loop", "nested loops": "Nested Loop",
    "nested loop join": "Nested Loop",
    # aggregation and others
    "aggregate": "Aggregate", "hashaggregate": "HashAggregate",
    "hash aggregate": "HashAggregate", "groupaggregate": "GroupAggregate",
    "group aggregate": "GroupAggregate",
    "sort": "Sort", "limit": "Limit", "gather": "Gather",
    "gather merge": "Gather Merge", "materialize": "Materialize",
    "unique": "Unique", "append": "Append", "result": "Result",
    "subquery scan": "Subquery Scan", "cte scan": "CTE Scan",
    "memoize": "Memoize",
}

def normalize_node_type(name: str) -> str:
    """
    Return the PostgreSQL spelling of a node name (unknown names unchanged).
    """
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    return NODE_TYPE_ALIASES.get(cleaned.lower(), cleaned)

# ---------------------------------------------------------------------------
# 3. Parsing EXPLAIN-style JSON >>> PlanNode
# ---------------------------------------------------------------------------
_CONDITION_KEYS = (
    "Hash Cond", "Merge Cond", "Join Filter", "Index Cond",
    "Recheck Cond", "Filter", "Condition"
)
_SELECTION_KEYS = ("Filter", "Index Cond", "Recheck Cond")
_JOIN_KEYS = ("Hash Cond", "Merge Cond", "Join Filter", "Condition")

def plan_from_dict(d: dict) -> PlanNode:
    """
    Recursively convert one plan node dictionary into a PlanNode tree.
    """
    node_type = normalize_node_type(d.get("Node Type", "Unknown"))

    # PostgreSQL reports every aggregate as "Aggregate" with a separate Strategy
    if node_type == "Aggregate":
        strategy = str(d.get("Strategy", "")).lower()
        if strategy.startswith("hash"):
            node_type = "HashAggregate"
        elif strategy.startswith("sort"):
            node_type = "GroupAggregate"

    conditions = [f"{k}: {d[k]}" for k in _CONDITION_KEYS if d.get(k)]
    selection = " AND ".join(str(d[k]) for k in _SELECTION_KEYS if d.get(k))
    join_cond = " AND ".join(str(d[k]) for k in _JOIN_KEYS if d.get(k))
    return PlanNode(
        node_type=node_type,
        relation=d.get("Relation Name"),
        index_name=d.get("Index Name"),
        join_type=d.get("Join Type"),
        conditions="; ".join(conditions) or None,
        children=[plan_from_dict(c) for c in d.get("Plans", [])],
        selection=selection or None,
        join_cond=join_cond or None,
        partial_mode=d.get("Partial Mode"),
    )

def plan_from_explain_json(obj) -> PlanNode:
    """
    Convert EXPLAIN (FORMAT JSON) output into a PlanNode tree.

    Args:
        obj: a JSON string or parsed object in any of the forms
            [{"Plan": {...}}], {"Plan": {...}} or a bare node dict.

    Returns:
        PlanNode: the root of the plan.
    """
    if isinstance(obj, str):
        obj = json.loads(obj)
    if isinstance(obj, list):
        obj = obj[0]
    if "Plan" in obj:
        obj = obj["Plan"]
    return plan_from_dict(obj)

# ---------------------------------------------------------------------------
# 4. PostgreSQL access
# ---------------------------------------------------------------------------
@dataclass
class DBConfig:
    """Connection settings for the PostgreSQL database."""
    host: str = "localhost"
    port: str = "5432"
    dbname: str = "TPC-H"
    user: str = "postgres"
    password: str = ""
    schema: str = "public"

    @classmethod
    def from_env(cls) -> "DBConfig":
        return cls(
            host=os.environ.get("PGHOST", "localhost"),
            port=os.environ.get("PGPORT", "5432"),
            dbname=os.environ.get("PGDATABASE", "TPC-H"),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", ""),
            schema=os.environ.get("PGSCHEMA", "public"),
        )

def connect_db(cfg: DBConfig):
    try:
        import psycopg2  # imported here so the other modules load without psycopg2
    except ImportError as exc:
        raise RuntimeError("psycopg2 is not installed. Run: pip install psycopg2-binary") from exc
    try:
        return psycopg2.connect(host=cfg.host, port=cfg.port, dbname=cfg.dbname,
                                user=cfg.user, password=cfg.password,
                                connect_timeout=10)
    except psycopg2.Error as exc:
        raise RuntimeError(f"Could not connect to PostgreSQL: {exc}") from exc

def get_schema(conn, schema: str = "public") -> dict:
    """
    Read the tables, columns and indexes of a schema.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT table_name, column_name, data_type
                 FROM information_schema.columns
                WHERE table_schema = %s
                ORDER BY table_name, ordinal_position""", (schema,))
        tables: dict = {}
        for table, column, dtype in cur.fetchall():
            tables.setdefault(table, []).append((column, dtype))

        cur.execute("SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = %s "
                    "ORDER BY tablename, indexname", (schema,))
        indexes = [(t, d) for t, d in cur.fetchall()]
    conn.rollback()
    return {"tables": tables, "indexes": indexes}

def schema_to_text(schema: dict) -> str:
    """
    Describe a schema as text for the LLM prompt.
    """
    lines = ["TABLES:"]
    for table, cols in schema["tables"].items():
        lines.append(f"  {table}(" + ", ".join(f"{c} {t}" for c, t in cols) + ")")
    lines.append("INDEXES:")
    if schema["indexes"]:
        lines.extend(f"  {d}" for _, d in schema["indexes"])
    else:
        lines.append("  (none)")
    return "\n".join(lines)

def get_postgres_plan(conn, query: str, schema: str = "public") -> PlanNode:
    """
    Get the execution plan PostgreSQL chooses for a query by running EXPLAIN.
    """
    import psycopg2
    from psycopg2.extensions import quote_ident
    query = query.strip().rstrip(";")
    if not query:
        raise ValueError("The query is empty.")
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO " + quote_ident(schema, cur))
            cur.execute("EXPLAIN (FORMAT JSON) " + query)
            raw = cur.fetchone()[0]
        return plan_from_explain_json(raw)
    except psycopg2.Error as exc:
        raise RuntimeError(f"PostgreSQL rejected the query: {exc}") from exc
    finally:
        conn.rollback()  # end the transaction so the connection is usable after an error


# ---------------------------------------------------------------------------
# 5. LLM access (Google Gemini REST API)
# ---------------------------------------------------------------------------
class LLMError(RuntimeError):
    """Raised when the LLM cannot be called or its reply cannot be used."""
    pass


PROMPT_TEMPLATE = """You are a PostgreSQL query optimizer. Predict the query execution plan
PostgreSQL would choose for the query below. The output is to be in the same format as 
how "EXPLAIN (FORMAT JSON)" would show it.

{schema}

QUERY:
{query}

Rules:
- Answer with ONE JSON object and nothing else (no markdown, no commentary).
- Shape: {{"Plan": {{"Node Type": ..., "Relation Name": ..., "Index Name": ...,
  "Join Type": ..., "Condition": ..., "Plans": [child nodes]}}}}
- Use PostgreSQL node names only: Seq Scan, Index Scan, Index Only Scan,
  Bitmap Heap Scan, Hash Join, Merge Join, Nested Loop, Hash, Sort,
  Aggregate, HashAggregate, GroupAggregate, Limit, Gather, Materialize, ...
- "Relation Name" is the real table name and appears on scan nodes only.
- On scan nodes put the WHERE predicates applied to that table in "Filter"
  (or "Index Cond" for an index lookup). On join nodes put the join
  predicate in "Hash Cond", "Merge Cond" or "Condition".
- Omit keys that do not apply. Do not include costs or row counts.
- A Hash Join has two children: the probe side first, then a Hash node
  whose single child is the build side.
"""

def build_prompt(query: str, schema_text: str) -> str:
    return PROMPT_TEMPLATE.format(schema=schema_text, query=query.strip())

def ask_llm(prompt: str, api_key: Optional[str] = None) -> str:
    """
    Prompt LLM and cache replies in .llm_cache, keyed by the model name and the prompt.
    """
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise LLMError("No API key. Set the GEMINI_API_KEY environment variable "
                       "or paste a key into the GUI.")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)

    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".llm_cache")
    cache_file = os.path.join(
        cache_dir, hashlib.sha256((model + prompt).encode()).hexdigest() + ".txt")
    if os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            return f.read()

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
        parts = payload["candidates"][0]["content"]["parts"]
        # some models return "thought" parts before the answer; skip them
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise KeyError("no text in the model reply")
    except urllib.error.HTTPError as exc:
        raise LLMError(f"LLM API returned HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
    except (urllib.error.URLError, KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"LLM call failed: {exc}") from exc

    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        f.write(text)
    return text

def extract_json(text: str):
    """
    Extract the JSON object from an LLM reply.
    """
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("The LLM reply did not contain a JSON object.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"The LLM reply was not valid JSON: {exc}") from exc


def get_llm_plan(query: str, schema: dict, api_key: Optional[str] = None) -> PlanNode:
    """
    Ask the LLM to predict the execution plan of a query.
    """
    reply = ask_llm(build_prompt(query, schema_to_text(schema)), api_key)
    try:
        return plan_from_explain_json(extract_json(reply))
    except (AttributeError, KeyError, TypeError, IndexError) as exc:
        raise LLMError(f"Could not interpret the LLM plan structure: {exc}") from exc


# ---------------------------------------------------------------------------
# 6. Sample plans (no database or API key needed)
# ---------------------------------------------------------------------------
def sample_plans():
    """
    Build a fixed pair of example plans for a join of lineitem and part.

    Returns:
        tuple: (postgres_plan, llm_plan) as PlanNode trees.
    """
    pg = {"Node Type": "Aggregate", "Strategy": "Plain", "Partial Mode": "Finalize",
          "Plans": [
        {"Node Type": "Gather", "Plans": [
            {"Node Type": "Aggregate", "Strategy": "Plain", "Partial Mode": "Partial",
             "Plans": [
                {"Node Type": "Hash Join", "Join Type": "Inner",
                 "Hash Cond": "(l_partkey = p_partkey)", "Plans": [
                    {"Node Type": "Seq Scan", "Relation Name": "lineitem",
                     "Filter": "(l_shipdate >= '1993-11-01'::date)"},
                    {"Node Type": "Hash", "Plans": [
                        {"Node Type": "Seq Scan", "Relation Name": "part"}]}]}]}]}]}
    llm = {"Node Type": "Aggregate", "Strategy": "Plain", "Plans": [
        {"Node Type": "Hash Join", "Join Type": "Inner",
         "Condition": "l_partkey = p_partkey", "Plans": [
            {"Node Type": "Index Scan", "Relation Name": "lineitem",
             "Index Name": "idx_lineitem_shipdate",
             "Index Cond": "l_shipdate >= '1993-11-01'"},
            {"Node Type": "Hash", "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "part"}]}]}]}
    return plan_from_dict(pg), plan_from_dict(llm)