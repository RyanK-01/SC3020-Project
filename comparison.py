"""
comparison.py
=============
Compares the PostgreSQL plan (the baseline) with the LLM plan.

Algorithm (basic version - improve it, this is the part that is graded)
-----------------------------------------------------------------------
1. Give every node a KEY that identifies "the same piece of work" in both plans:
     scan       -> ("scan", table)
     join       -> ("join", set of base tables below it)
     aggregate  -> ("aggregate", set of base tables below it)
     anything else (Sort, Gather, Limit...) -> (node type, set of tables below it)
   Repeated keys are told apart by an occurrence counter (pre-order).
2. Nodes with the same key in both plans are MATCHED. If their operator or index
   differs the node is "changed" (e.g. Seq Scan vs Index Scan, Hash Join vs
   Merge Join). Otherwise it is "unchanged".
3. A node with no partner is "missing" (only in PostgreSQL's plan) or
   "extra" (only in the LLM plan). A join that has no partner means the two
   plans join the tables in a different order.
4. Nodes that carry no decision of their own are skipped ("ignored", drawn grey):
   Hash / Parallel Hash / Bitmap Index Scan helpers, and the parallel-execution
   nodes Gather, Gather Merge and the Partial half of a parallel aggregate.
   Whether PostgreSQL goes parallel depends on table size and server settings,
   not on the query, so counting them would punish the AI for something it
   cannot know. (The Finalize aggregate is kept and stands for the whole aggregation.)
5. For matched nodes we also compare, besides the operator:
     scans -> the columns used in single-column predicates (WHERE filters)
     joins -> the column pairs in the join predicate (when both plans give one)
     Hash Join / Nested Loop -> which side is the build (hash) / outer table

Cost: one traversal per tree plus dictionary lookups, so O(n + m) for plans of
n and m nodes (tables() is recomputed per node, which is O(n * depth) - cache
it if plans get large).

Difference rate
---------------
    rate = (changed + missing + extra) / (matched + missing + extra)
0 = identical, 1 = nothing in common. Justify or replace this in the report.

Status values written onto PlanNode.status:
    unchanged | changed | missing (only in PostgreSQL) | extra (only in LLM) | ignored
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from preprocessing import PlanNode, sample_plans

IGNORED_NODE_TYPES = {"Hash", "Parallel Hash", "Bitmap Index Scan",
                      "Gather", "Gather Merge"}
SCAN_TYPES = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"}
JOIN_TYPES = {"Hash Join", "Merge Join", "Nested Loop"}
AGGREGATE_TYPES = {"Aggregate", "HashAggregate", "GroupAggregate"}


@dataclass
class Difference:
    kind: str            # scan_method | scan_condition | join_algorithm | join_type
                         # join_condition | join_sides | aggregate_strategy
                         # join_order | operator_missing | operator_extra
    text: str            # sentence shown in the GUI
    pg_node: PlanNode = None
    llm_node: PlanNode = None


@dataclass
class ComparisonResult:
    differences: list = field(default_factory=list)
    matched: int = 0     # matched pairs (changed ones included)
    changed: int = 0
    missing: int = 0
    extra: int = 0

    @property
    def rate(self) -> float:
        total = self.matched + self.missing + self.extra
        return (self.changed + self.missing + self.extra) / total if total else 0.0


# ---------------------------------------------------------------------------
# Step 1: keys
# ---------------------------------------------------------------------------
def category(node_type: str) -> str:
    if node_type in SCAN_TYPES:
        return "scan"
    if node_type in JOIN_TYPES:
        return "join"
    if node_type in AGGREGATE_TYPES:
        return "aggregate"
    return node_type


def is_ignored(node: PlanNode) -> bool:
    """True for helper / parallel-execution nodes that are not compared."""
    return node.node_type in IGNORED_NODE_TYPES or node.partial_mode == "Partial"


def keyed_nodes(root: PlanNode) -> dict:
    """Map (base_key, occurrence) -> node for every decision-carrying node."""
    seen: dict = {}
    keyed: dict = {}
    for node in root.walk():
        if is_ignored(node):
            continue
        cat = category(node.node_type)
        base = ("scan", node.relation) if cat == "scan" else (cat, node.tables())
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        keyed[(base, occurrence)] = node
    return keyed


# ---------------------------------------------------------------------------
# Step 2-3: classify and explain
# ---------------------------------------------------------------------------
def _names(tables) -> str:
    return ", ".join(sorted(tables)) if tables else "?"


def _scan_desc(node: PlanNode) -> str:
    return node.node_type + (f" on index {node.index_name}" if node.index_name else "")


# --- condition analysis ----------------------------------------------------
_STRING = re.compile(r"'(?:[^']|'')*'")
_CAST = re.compile(r"::\s*[A-Za-z_]+(?:\s+(?:without|with)\s+time\s+zone)?(?:\[\])?")
_IDENT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_NOT_COLUMNS = {
    "and", "or", "not", "like", "ilike", "in", "any", "all", "is", "null", "between",
    "case", "when", "then", "else", "end", "true", "false", "date", "interval",
    "timestamp", "text", "numeric", "integer", "bigint", "bpchar", "varchar", "char",
    "array", "cast", "as", "extract", "year", "month", "day", "substring", "from",
    "for", "exists", "select",
}


def _split_and(text: str) -> list:
    """Split a condition at top-level AND (ignores AND inside parentheses)."""
    parts, depth, current = [], 0, ""
    tokens = re.split(r"(\(|\)|\bAND\b)", text, flags=re.IGNORECASE)
    for tok in tokens:
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        if tok.upper() == "AND" and depth <= 0:
            parts.append(current)
            current = ""
        else:
            current += tok
    parts.append(current)
    # PostgreSQL wraps the whole thing as ((a) AND (b)); peel one outer layer
    if len(parts) == 1:
        inner = parts[0].strip()
        if inner.startswith("(") and inner.endswith(")"):
            deeper = _split_and(inner[1:-1])
            if len(deeper) > 1:
                return deeper
    return [p.strip() for p in parts if p.strip()]


def _columns(predicate: str) -> list:
    """Column names mentioned in a predicate, one entry per mention
    (qualifiers, literals and casts removed). a.k = b.k gives ['k', 'k']."""
    text = _CAST.sub(" ", _STRING.sub(" ", predicate))
    names = [m.split(".")[-1].lower() for m in _IDENT.findall(text)]
    return [n for n in names if n not in _NOT_COLUMNS]


def _has_or(predicate: str) -> bool:
    """Checks whether a predicate contains OR"""
    return re.search(r"\bOR\b", predicate, re.IGNORECASE) is not None


def selection_columns(text) -> frozenset:
    """Columns used in WHERE-style predicates: those that mention one column,
    or an OR of several. Predicates comparing two columns are join predicates
    and are left out, so a join predicate pushed into an index scan does not
    look like a filter."""
    cols = set()
    for pred in _split_and(text or ""):
        found = _columns(pred)
        if len(found) == 1 or (found and _has_or(pred)):
            cols |= set(found)
    return frozenset(cols)


def join_pairs(text) -> frozenset:
    """Pairs of columns compared against each other, e.g. ('l_partkey', 'p_partkey')."""
    pairs = set()
    for pred in _split_and(text or ""):
        found = _columns(pred)
        if len(found) == 2 and not _has_or(pred):
            pairs.add(tuple(sorted(found)))
    return frozenset(pairs)


def _cols_text(cols) -> str:
    return ", ".join(sorted(cols)) if cols else "no filter"


def _pairs_text(pairs) -> str:
    return ", ".join(" = ".join(p) for p in sorted(pairs))


def _first_child_tables(node: PlanNode) -> frozenset:
    """Gets the tables under the first child. For a Hash Join that’s the probe side, 
    and for a Nested Loop the outer side, which is how swapped sides are detected"""
    return node.children[0].tables() if node.children else frozenset()


def classify_matched(pg: PlanNode, llm: PlanNode) -> list:
    """Return the list of Differences for a matched pair (empty if they agree)."""
    found = []
    cat = category(pg.node_type)
    if cat == "scan":
        if pg.node_type != llm.node_type or pg.index_name != llm.index_name:
            found.append(Difference(
                "scan_method",
                f"Table '{pg.relation}': PostgreSQL uses {_scan_desc(pg)}, "
                f"the AI plan uses {_scan_desc(llm)}.", pg, llm))
        a, b = selection_columns(pg.selection), selection_columns(llm.selection)
        if a != b:
            found.append(Difference(
                "scan_condition",
                f"Table '{pg.relation}': PostgreSQL filters on [{_cols_text(a)}], "
                f"the AI plan on [{_cols_text(b)}].", pg, llm))
    elif cat == "join":
        if pg.node_type != llm.node_type:
            found.append(Difference(
                "join_algorithm",
                f"Join of {_names(pg.tables())}: PostgreSQL uses {pg.node_type}, "
                f"the AI plan uses {llm.node_type}.", pg, llm))
        else:
            if pg.join_type and llm.join_type and pg.join_type != llm.join_type:
                found.append(Difference(
                    "join_type",
                    f"Join of {_names(pg.tables())}: PostgreSQL does a {pg.join_type} join, "
                    f"the AI plan a {llm.join_type} join.", pg, llm))
            if pg.node_type in ("Hash Join", "Nested Loop") \
                    and len(pg.children) == 2 and len(llm.children) == 2:
                p_first, l_first = _first_child_tables(pg), _first_child_tables(llm)
                if p_first != l_first:
                    if pg.node_type == "Hash Join":
                        role = "probes with"       # first child = probe, second = hash/build
                    else:
                        role = "has as outer table"
                    found.append(Difference(
                        "join_sides",
                        f"{pg.node_type} of {_names(pg.tables())}: PostgreSQL {role} "
                        f"{_names(p_first)}, the AI plan {role} {_names(l_first)} "
                        f"(build/inner side swapped).", pg, llm))
        a, b = join_pairs(pg.join_cond), join_pairs(llm.join_cond)
        if a and b and a != b:           # only when both plans state a join predicate
            found.append(Difference(
                "join_condition",
                f"Join of {_names(pg.tables())}: PostgreSQL joins on "
                f"[{_pairs_text(a)}], the AI plan on [{_pairs_text(b)}].", pg, llm))
    elif cat == "aggregate":
        if pg.node_type != llm.node_type:
            found.append(Difference(
                "aggregate_strategy",
                f"Aggregation over {_names(pg.tables())}: PostgreSQL uses {pg.node_type}, "
                f"the AI plan uses {llm.node_type}.", pg, llm))
    elif pg.node_type != llm.node_type:   # cannot happen with the current keys
        found.append(Difference("operator_changed",
                                f"{pg.node_type} vs {llm.node_type}.", pg, llm))
    return found


def describe_unmatched(node: PlanNode, only_in: str) -> Difference:
    """only_in is 'pg' (node missing from the LLM plan) or 'llm' (extra)."""
    plan_name, other = ("PostgreSQL's plan", "the AI plan") if only_in == "pg" \
        else ("the AI plan", "PostgreSQL's plan")
    kind_prefix = "operator_missing" if only_in == "pg" else "operator_extra"
    cat = category(node.node_type)
    if cat == "join":
        return Difference(
            "join_order",
            f"{node.node_type} of {_names(node.tables())} appears only in {plan_name}; "
            f"{other} joins these tables in a different order or grouping.",
            node if only_in == "pg" else None, node if only_in == "llm" else None)
    where = f" on '{node.relation}'" if node.relation else (
        f" over {_names(node.tables())}" if node.tables() else "")
    return Difference(
        kind_prefix, f"{node.node_type}{where} appears only in {plan_name}.",
        node if only_in == "pg" else None, node if only_in == "llm" else None)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def compare_plans(pg_plan: PlanNode, llm_plan: PlanNode) -> ComparisonResult:
    """
    Mark every node of both trees (status + note) and return the summary.
    The trees are modified in place so the GUI can draw them directly.
    """
    for node in list(pg_plan.walk()) + list(llm_plan.walk()):
        node.status, node.note = ("ignored" if is_ignored(node) else "unchanged"), ""

    pg_keys, llm_keys = keyed_nodes(pg_plan), keyed_nodes(llm_plan)
    result = ComparisonResult()

    for key, pg_node in pg_keys.items():
        llm_node = llm_keys.get(key)
        if llm_node is None:
            diff = describe_unmatched(pg_node, "pg")
            pg_node.status, pg_node.note = "missing", diff.text
            result.missing += 1
            result.differences.append(diff)
            continue
        result.matched += 1
        diffs = classify_matched(pg_node, llm_node)
        if diffs:                       # one node counts once, however many things differ
            pg_node.status = llm_node.status = "changed"
            pg_node.note = llm_node.note = " ".join(d.text for d in diffs)
            result.changed += 1
            result.differences.extend(diffs)

    for key, llm_node in llm_keys.items():
        if key not in pg_keys:
            diff = describe_unmatched(llm_node, "llm")
            llm_node.status, llm_node.note = "extra", diff.text
            result.extra += 1
            result.differences.append(diff)

    return result


# TODO ideas for the team:
#  - compare predicate operators and constants (only the columns are compared now)
#  - tree edit distance as a second difference-rate metric to compare against
#  - the Sort below a parallel (Partial) aggregate is still compared

if __name__ == "__main__":
    pg, llm = sample_plans()
    outcome = compare_plans(pg, llm)
    for d in outcome.differences:
        print(f"[{d.kind}] {d.text}")
    print(f"matched={outcome.matched} changed={outcome.changed} "
          f"missing={outcome.missing} extra={outcome.extra} rate={outcome.rate:.2f}")
