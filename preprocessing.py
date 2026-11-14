from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import psycopg2
from dataclasses import dataclass, field
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# 0. Environment variables and defaults
# ---------------------------------------------------------------------------
import dotenv
dotenv.load_dotenv()  # load DB credentials and API key from a local .env file, if present

# model used when GEMINI_MODEL is not set in the environment
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"

# ---------------------------------------------------------------------------
# 1. PlanNode
# ---------------------------------------------------------------------------
@dataclass(eq=False)  # identity-based hashing so nodes can be used as dict keys
class PlanNode:
    """ represents one operator in a query execution plan. """
    node_type: str                      # e.g. "Seq Scan", "Hash Join", "Aggregate"
    relation: Optional[str] = None      # table name (scan nodes only)
    index_name: Optional[str] = None    # index used by an Index / Index Only Scan
    join_type: Optional[str] = None     # e.g. "Inner", "Left"
    conditions: Optional[str] = None    # all condition text, for display
    children: list = field(default_factory=list)  # child PlanNodes (inputs to this operator)

    selection: Optional[str] = None     # scan predicates: Filter / Index Cond / Recheck Cond
    join_cond: Optional[str] = None     # join predicates: Hash Cond / Merge Cond / Join Filter / Condition
    partial_mode: Optional[str] = None  # "Partial" or "Finalize" for parallel aggregates
    sort_key: Optional[str] = None      # Sort Key of Sort / Incremental Sort nodes, for display

    status: str = "unchanged"           # unchanged | changed | missing | extra | ignored

    def walk(self) -> Iterator["PlanNode"]:
        """ pre-order traverse this node and all of its descendants. """
        yield self  # visit the current node before its children
        for child in self.children:
            yield from child.walk()

    def tables(self) -> frozenset:
        """ return the base tables read anywhere in this subtrees. """
        return frozenset(n.relation for n in self.walk() if n.relation)

    def label(self) -> str:
        """ short text to be shown inside node's box in the tree. """
        text = self.node_type
        # add the table and index on separate lines
        if self.relation:
            text += f"\n[{self.relation}]"
        if self.index_name:
            text += f"\n({self.index_name})"
        return text

def plan_to_text(node: PlanNode, indent: int = 0) -> str:
    """ convert a plan tree into readable string. """
    line = "  " * indent + node.node_type
    if node.relation:
        line += f" [{node.relation}]"
    if node.index_name:
        line += f" using {node.index_name}"
    if node.conditions:
        line += f"  ({node.conditions})"
    lines = [line]

    # recursively render each child one level deeper
    for child in node.children:
        lines.append(plan_to_text(child, indent + 1))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# 2. Normalization: map LLM wording to PostgreSQL node names
# ---------------------------------------------------------------------------
# {key:value} == {lower-case variants from LLM: PostgreSQL's canonical names}
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
    """ return the PostgreSQL spelling of a node name. """
    # trim the name and collapse repeated whitespace before the lookup
    cleaned = re.sub(r"\s+", " ", (name or "").strip())

    # unknown names are left as is
    return NODE_TYPE_ALIASES.get(cleaned.lower(), cleaned)

# ---------------------------------------------------------------------------
# 3. Parsing EXPLAIN-style JSON >>> PlanNode
# ---------------------------------------------------------------------------
# for predicate conditions
_CONDITION_KEYS = (
    "Hash Cond", "Merge Cond", "Join Filter", "Index Cond",
    "Recheck Cond", "Filter", "Condition"
)
_SELECTION_KEYS = ("Filter", "Index Cond", "Recheck Cond")
_JOIN_KEYS = ("Hash Cond", "Merge Cond", "Join Filter", "Condition")

def plan_from_dict(plan_dict: dict) -> PlanNode:
    """ helper function to recursively convert one plan node dictionary into a PlanNode tree. """
    node_type = normalize_node_type(plan_dict.get("Node Type", "Unknown"))

    # PostgreSQL reports every aggregate as "Aggregate" with a separate Strategy
    if node_type == "Aggregate":
        strategy = str(plan_dict.get("Strategy", "")).lower()
        if strategy.startswith("hash"):
            node_type = "HashAggregate"
        elif strategy.startswith("sort"):
            node_type = "GroupAggregate"

    # gather predicate text
    conditions = [f"{k}: {plan_dict[k]}" for k in _CONDITION_KEYS if plan_dict.get(k)]
    selection = " AND ".join(str(plan_dict[k]) for k in _SELECTION_KEYS if plan_dict.get(k))
    join_cond = " AND ".join(str(plan_dict[k]) for k in _JOIN_KEYS if plan_dict.get(k))

    sort_key = plan_dict.get("Sort Key")                  # a list in EXPLAIN JSON or a string from the LLM
    if isinstance(sort_key, list):
        sort_key = ", ".join(str(k) for k in sort_key)

    return PlanNode(
        node_type = node_type,
        relation = plan_dict.get("Relation Name"),
        index_name = plan_dict.get("Index Name"),
        join_type = plan_dict.get("Join Type"),
        conditions = "; ".join(conditions) or None,
        children = [plan_from_dict(c) for c in plan_dict.get("Plans", [])],  # recurse into child plans
        selection = selection or None,
        join_cond = join_cond or None,
        partial_mode = plan_dict.get("Partial Mode"),
        sort_key = str(sort_key) if sort_key else None,
    )

def plan_from_explain_json(obj) -> PlanNode:
    """ convert EXPLAIN (FORMAT JSON) output into a PlanNode tree and returning the root node. """
    if isinstance(obj, str):  # raw JSON text
        obj = json.loads(obj)
    if isinstance(obj, list):  # EXPLAIN wraps the result in a one-element list
        obj = obj[0]
    if "Plan" in obj:  # unwrap {"Plan": {...}} to reach the root node
        obj = obj["Plan"]
    return plan_from_dict(obj)

# ---------------------------------------------------------------------------
# 4. PostgreSQL access
# ---------------------------------------------------------------------------
@dataclass
class DBConfig:
    """ connection settings for the PostgreSQL database. """
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
        return psycopg2.connect(
            host=cfg.host, 
            port=cfg.port, 
            dbname=cfg.dbname,
            user=cfg.user, 
            password=cfg.password,
            connect_timeout=10  # fail after 10 seconds instead of hanging
        )
    except psycopg2.Error as exc:
        raise RuntimeError(f"Could not connect to PostgreSQL: {exc}") from exc

def get_schema(conn, schema: str = "public") -> dict:
    """ read the tables, columns and indexes of a schema. """
    with conn.cursor() as cur:
        # list every column of every table in the schema
        _query = """SELECT table_name, column_name, data_type
                        FROM information_schema.columns
                        WHERE table_schema = %s
                        ORDER BY table_name, ordinal_position"""
        cur.execute(_query, (schema,))
        tables: dict = {}

        # group the (column, type) pairs by table name
        for table, column, dtype in cur.fetchall():
            tables.setdefault(table, []).append((column, dtype))

        # indexdef is the full CREATE INDEX statement
        cur.execute("SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = %s ORDER BY tablename, indexname", (schema,))
        indexes = [(t, d) for t, d in cur.fetchall()]
    conn.rollback()  # end the read-only transaction opened by the queries
    return {"tables": tables, "indexes": indexes}

def schema_to_text(schema: dict) -> str:
    """ describe a schema as text for the LLM prompt. """

    lines = ["TABLES:"]
    # "  part(p_partkey integer, p_name character varying)"
    for table, cols in schema["tables"].items():
        lines.append(f"  {table}(" + ", ".join(f"{c} {t}" for c, t in cols) + ")")
    lines.append("INDEXES:")

    if schema["indexes"]:
        lines.extend(f"  {d}" for _, d in schema["indexes"])
    else:
        lines.append("  (none)")
    return "\n".join(lines)

def get_stats(conn, schema: str = "public", query: Optional[str] = None) -> dict:
    """ obtain the table sizes from pg_class of the tables named in the query """

    # retrieves identifier words from the user's SQL query, such as table names
    words = set(re.findall(r"[a-z_][a-z0-9_$]*", query.lower())) if query else None
    try:
        with conn.cursor() as cur:
            # --- table sizes ---
            # relkind r/p/m = ordinary tables, partitioned tables and materialized views
            table_size_query = """SELECT c.relname, c.reltuples, c.relpages
                                FROM pg_class c
                                JOIN pg_namespace n ON n.oid = c.relnamespace
                                WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'm')
                                ORDER BY c.relname"""
            cur.execute(table_size_query, (schema,))
            
            sizes = {}
            for table, rows, pages in cur.fetchall():
                # checks if table has had ANALYSE run on it before
                known = rows is not None and rows >= 0 and not (rows == 0 and not pages)
                sizes[table] = {"rows": int(rows) if known else None, "pages": int(pages or 0)}
            
            # keep only the tables mentioned in the query
            if words is not None:
                used = {t for t in sizes if t.lower() in words}
                if used:  # list all tables if no table name matched 
                    sizes = {t: s for t, s in sizes.items() if t in used}
    except psycopg2.Error as exc:
        raise RuntimeError(f"Could not read the statistics: {exc}") from exc
    finally:
        conn.rollback()

    return {"tables": sizes}

def stats_to_text(stats: dict) -> str:
    """ describe the statistics returned by get_stats() as text for the LLM prompt. """

    lines = ["TABLE STATISTICS (PostgreSQL's estimates from the last ANALYZE):"]
    for table, size in stats["tables"].items():
        # rows is None if table has never been analyzed
        if size["rows"] is None:
            lines.append(f"  {table}: statistics unavailable (table not analyzed)")
        else:
            lines.append(f"  {table}: {size['rows']} rows, {size['pages']} pages")
    if not stats["tables"]:
        lines.append("  (none)")

    return "\n".join(lines)

def get_postgres_plan(conn, query: str, schema: str = "public") -> PlanNode:
    """ get the execution plan PostgreSQL chooses for a query by running EXPLAIN. """
    from psycopg2.extensions import quote_ident

    query = query.strip().rstrip(";")  # remove trailing ";"
    if not query:
        raise ValueError("The query is empty.")
    try:
        with conn.cursor() as cur:
            # resolve unqualified table names against the chosen schema
            cur.execute("SET search_path TO " + quote_ident(schema, cur))
            cur.execute("EXPLAIN (FORMAT JSON) " + query)
            raw = cur.fetchone()[0]  
    except psycopg2.Error as exc:
        raise RuntimeError(f"PostgreSQL rejected the query: {exc}") from exc
    finally:
        conn.rollback() 
    if isinstance(raw, str):  # psycopg2 usually decodes the JSON already
        raw = json.loads(raw)

    # return parsed tree and formatted raw JSON for display
    return plan_from_explain_json(raw), json.dumps(raw, indent=2)

# ---------------------------------------------------------------------------
# 5. LLM access (Google Gemini REST API)
# ---------------------------------------------------------------------------
class LLMError(RuntimeError):
    """ raised when the LLM cannot be called or its reply cannot be used. """
    prompt: Optional[str] = None
    reply: Optional[str] = None


# doubled braces {{ }} are literal braces; single braces are str.format() placeholders
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
{stats_rule}"""

# extra rule inserted into the prompt only when table statistics are supplied
STATS_RULE = """- Use the estimated table sizes in TABLE STATISTICS above to aid in the plan formulation,
    using the number of rows produced from each scan and each join produces to choose the appropriate 
    scan method, join algorithm, join order and hash build side.
"""

def build_prompt(query: str, schema_text: str, stats_text: str = "") -> str:
    """ builds the LLM prompt """

    # append the table statistics if provided as an argument
    if stats_text:
        schema_text = schema_text + "\n" + stats_text 

    # builds the final prompt template
    return PROMPT_TEMPLATE.format(schema=schema_text, query=query.strip(), stats_rule=STATS_RULE if stats_text else "")

def ask_llm(prompt: str, api_key: Optional[str] = None) -> str:
    """ prompt LLM and cache replies in .llm_cache, keyed by the model name and the prompt. """

    # GUI key priority over the environment variable
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise LLMError("No API key. Set the GEMINI_API_KEY environment variable or paste a key into the GUI.")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)

    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".llm_cache")
    # hash model + prompt so each distinct request maps to its own file
    cache_file = os.path.join(
        cache_dir, hashlib.sha256((model + prompt).encode()).hexdigest() + ".txt")
    # return the cached reply to avoid a repeat API call
    if os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            return f.read()

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    # temperature 0 for deterministic output; the MIME type asks Gemini for pure JSON
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
        # the answer text is inside the first candidate's content parts
        parts = payload["candidates"][0]["content"]["parts"]
        # some models return "thought" parts before the answer; skip them
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise KeyError("no text in the model reply")
    except urllib.error.HTTPError as exc:
        # include the first 300 characters of the error body
        raise LLMError(f"LLM API returned HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
    except (urllib.error.URLError, KeyError, IndexError, ValueError) as exc:
        # network failures or an unexpected response shape
        raise LLMError(f"LLM call failed: {exc}") from exc

    # cache successful responses
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        f.write(text)
    return text

def extract_json(text: str):
    """ extract the JSON object from an LLM reply. """

    text = re.sub(r"```(?:json)?", "", text)  # strip markdown code fences if the model added them

    # take everything from the first "{" to the last "}" to skip any surrounding prose
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("The LLM reply did not contain a JSON object.")
    
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"The LLM reply was not valid JSON: {exc}") from exc


def get_llm_plan(query: str, schema: dict, api_key: Optional[str] = None, stats: Optional[dict] = None) -> tuple:
    """ ask the LLM to predict the execution plan of a query. """
    prompt = build_prompt(query, schema_to_text(schema), stats_to_text(stats) if stats else "")
    reply = None  # stays None if the API call itself fails

    try:
        reply = ask_llm(prompt, api_key)
        try:
            plan = plan_from_explain_json(extract_json(reply))
        except (AttributeError, KeyError, TypeError, IndexError) as exc:
            # JSON not shaped like an EXPLAIN plan
            raise LLMError(f"Could not interpret the LLM plan structure: {exc}") from exc
    except LLMError as exc:
        exc.prompt, exc.reply = prompt, reply
        raise
    return plan, prompt, reply


# ---------------------------------------------------------------------------
# 6. Sample plans (no database or API key needed)
# ---------------------------------------------------------------------------
def sample_plans():
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
         "Hash Cond": "l_partkey = p_partkey", "Plans": [
            {"Node Type": "Index Scan", "Relation Name": "lineitem",
             "Index Name": "idx_lineitem_shipdate",
             "Index Cond": "l_shipdate >= '1993-11-01'"},
            {"Node Type": "Hash", "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "part"}]}]}]}
    return plan_from_dict(pg), plan_from_dict(llm)