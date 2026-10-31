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
4. Helper nodes that carry no decision (Hash, Bitmap Index Scan) are skipped.

Cost: one traversal per tree plus dictionary lookups, so O(n + m) for plans of
n and m nodes (tables() is recomputed per node, which is O(n * depth) - cache
it if plans get large).

Difference rate
---------------
    rate = (changed + missing + extra) / (matched + missing + extra)
0 = identical, 1 = nothing in common. Justify or replace this in the report.

Status values written onto PlanNode.status:
    unchanged | changed | missing (only in PostgreSQL) | extra (only in LLM)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from preprocessing import PlanNode, sample_plans

IGNORED_NODE_TYPES = {"Hash", "Bitmap Index Scan"}
SCAN_TYPES = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"}
JOIN_TYPES = {"Hash Join", "Merge Join", "Nested Loop"}
AGGREGATE_TYPES = {"Aggregate", "HashAggregate", "GroupAggregate"}


@dataclass
class Difference:
    kind: str            # scan_method | join_algorithm | join_type | aggregate_strategy
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


def keyed_nodes(root: PlanNode) -> dict:
    """Map (base_key, occurrence) -> node for every decision-carrying node."""
    seen: dict = {}
    keyed: dict = {}
    for node in root.walk():
        if node.node_type in IGNORED_NODE_TYPES:
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


def classify_matched(pg: PlanNode, llm: PlanNode):
    """Return a Difference for a matched pair, or None if they agree."""
    cat = category(pg.node_type)
    if cat == "scan":
        if pg.node_type != llm.node_type or pg.index_name != llm.index_name:
            return Difference(
                "scan_method",
                f"Table '{pg.relation}': PostgreSQL uses {_scan_desc(pg)}, "
                f"the AI plan uses {_scan_desc(llm)}.", pg, llm)
    elif cat == "join":
        if pg.node_type != llm.node_type:
            return Difference(
                "join_algorithm",
                f"Join of {_names(pg.tables())}: PostgreSQL uses {pg.node_type}, "
                f"the AI plan uses {llm.node_type}.", pg, llm)
        if pg.join_type and llm.join_type and pg.join_type != llm.join_type:
            return Difference(
                "join_type",
                f"Join of {_names(pg.tables())}: PostgreSQL does a {pg.join_type} join, "
                f"the AI plan a {llm.join_type} join.", pg, llm)
    elif cat == "aggregate":
        if pg.node_type != llm.node_type:
            return Difference(
                "aggregate_strategy",
                f"Aggregation over {_names(pg.tables())}: PostgreSQL uses {pg.node_type}, "
                f"the AI plan uses {llm.node_type}.", pg, llm)
    elif pg.node_type != llm.node_type:   # cannot happen with the current keys
        return Difference("operator_changed",
                          f"{pg.node_type} vs {llm.node_type}.", pg, llm)
    return None


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
        node.status, node.note = "unchanged", ""

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
        diff = classify_matched(pg_node, llm_node)
        if diff:
            pg_node.status = llm_node.status = "changed"
            pg_node.note = llm_node.note = diff.text
            result.changed += 1
            result.differences.append(diff)

    for key, llm_node in llm_keys.items():
        if key not in pg_keys:
            diff = describe_unmatched(llm_node, "llm")
            llm_node.status, llm_node.note = "extra", diff.text
            result.extra += 1
            result.differences.append(diff)

    return result


# TODO ideas for the team:
#  - compare join conditions and filters (node.conditions)
#  - detect swapped build/probe sides of a Hash Join
#  - compare ordering of children for Nested Loop (outer vs inner table)
#  - tree edit distance as a second difference-rate metric to compare against
#  - decide whether Gather/Parallel nodes should count (they depend on settings
#    and table size, not only on the query)

if __name__ == "__main__":
    pg, llm = sample_plans()
    outcome = compare_plans(pg, llm)
    for d in outcome.differences:
        print(f"[{d.kind}] {d.text}")
    print(f"matched={outcome.matched} changed={outcome.changed} "
          f"missing={outcome.missing} extra={outcome.extra} rate={outcome.rate:.2f}")
