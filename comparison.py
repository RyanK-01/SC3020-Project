from __future__ import annotations

import re
from dataclasses import dataclass, field

from preprocessing import PlanNode, sample_plans

IGNORED_NODE_TYPES = {
    "Hash", "Parallel Hash", "Bitmap Index Scan",
    "Gather", "Gather Merge"
}
SCAN_TYPES = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"}
JOIN_TYPES = {"Hash Join", "Merge Join", "Nested Loop"}
AGGREGATE_TYPES = {"Aggregate", "HashAggregate", "GroupAggregate"}

@dataclass
class Difference:
    """
    class representing a difference found between the two plans.
    """
    kind: str
    text: str                           # sentence shown to the user
    pg_node: PlanNode = None
    llm_node: PlanNode = None

@dataclass
class ComparisonResult:
    """Counts and list of differences produced by compare_plans()."""
    differences: list = field(default_factory=list)
    matched: int = 0                    # matched pairs, including changed ones
    changed: int = 0
    missing: int = 0
    extra: int = 0

    @property
    def rate(self) -> float:
        """
        Difference rate between the two plans.
        float: (changed + missing + extra) / (matched + missing + extra)
        """
        total = self.matched + self.missing + self.extra
        return (self.changed + self.missing + self.extra) / total if total else 0.0

# ---------------------------------------------------------------------------
# Step 1: node keys
# ---------------------------------------------------------------------------
def category(node_type: str) -> str:
    """
    categorizes node type into scan | join | aggregate
    """
    if node_type in SCAN_TYPES:
        return "scan"
    if node_type in JOIN_TYPES:
        return "join"
    if node_type in AGGREGATE_TYPES:
        return "aggregate"
    return node_type

def is_ignored(node: PlanNode) -> bool:
    """
    checks if a node is left out of the comparison.
    """
    return node.node_type in IGNORED_NODE_TYPES or node.partial_mode == "Partial"

def keyed_nodes(root: PlanNode) -> dict:
    """
    Assign a matching key to every compared node of a plan.
    """
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
# Steps 2-3: classifying and describing differences
# ---------------------------------------------------------------------------
def _names(tables) -> str:
    """
    Format a set of table names for a message.
    """
    return ", ".join(sorted(tables)) if tables else "?"

def _scan_desc(node: PlanNode) -> str:
    """
    Describe the access method of a scan node.
    """
    return node.node_type + (f" on index {node.index_name}" if node.index_name else "")

# --- condition analysis ----------------------------------------------------
_STRING = re.compile(r"'(?:[^']|'')*'")                 # quoted string literals
_CAST = re.compile(r"::\s*[A-Za-z_]+(?:\s+(?:without|with)\s+time\s+zone)?(?:\[\])?")
_IDENT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")  # identifiers, optionally qualified
# SQL keywords, type names and functions that are not column names
_NOT_COLUMNS = {
    "and", "or", "not", "like", "ilike", "in", "any", "all", "is", "null", "between",
    "case", "when", "then", "else", "end", "true", "false", "date", "interval",
    "timestamp", "text", "numeric", "integer", "bigint", "bpchar", "varchar", "char",
    "array", "cast", "as", "extract", "year", "month", "day", "substring", "from",
    "for", "exists", "select",
}

def _split_and(text: str) -> list:
    """
    splits a condition joined by top-level AND into a list of predicates.
    """
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

    # PostgreSQL writes conditions as ((a) AND (b)); remove one outer layer of brackets
    if len(parts) == 1:
        inner = parts[0].strip()
        if inner.startswith("(") and inner.endswith(")"):
            deeper = _split_and(inner[1:-1])
            if len(deeper) > 1:
                return deeper
    return [p.strip() for p in parts if p.strip()]

def _columns(predicate: str) -> list:
    """
    returns the column names mentioned in a predicate as a list
    duplicate entries allowed
    """
    text = _CAST.sub(" ", _STRING.sub(" ", predicate))
    names = [m.split(".")[-1].lower() for m in _IDENT.findall(text)]
    return [n for n in names if n not in _NOT_COLUMNS]

def _has_or(predicate: str) -> bool:
    """
    check for OR condition given a predicate
    """
    return re.search(r"\bOR\b", predicate, re.IGNORECASE) is not None

def selection_columns(text) -> frozenset:
    """
    find the columns used in selection (WHERE-style) predicates.

    A predicate counts as a selection if it mentions one column, or if it is
    an OR over several columns. Predicates comparing two columns are join
    predicates and left out, so that join predicates placed in an index
    condition are not treated as a filter.
    """
    cols = set()
    for pred in _split_and(text or ""):
        found = _columns(pred)
        if len(found) == 1 or (found and _has_or(pred)):
            cols |= set(found)
    return frozenset(cols)

def join_pairs(text) -> frozenset:
    """
    find the pairs of columns compared in join predicates.
    """
    pairs = set()
    for pred in _split_and(text or ""):
        found = _columns(pred)
        if len(found) == 2 and not _has_or(pred):
            pairs.add(tuple(sorted(found)))
    return frozenset(pairs)

def _cols_text(cols) -> str:
    """
    helper method to format a set of filter columns for a message.
    """
    return ", ".join(sorted(cols)) if cols else "no filter"

def _pairs_text(pairs) -> str:
    """
    helper method to format a set of join column pairs for a message.
    """
    return ", ".join(" = ".join(p) for p in sorted(pairs))

def _first_child_tables(node: PlanNode) -> frozenset:
    """
    helper method to return the tables under the first child of a node.

    For a Hash Join this is the probe side and for a Nested Loop the outer
    side, so comparing between plans shows whether the sides are swapped.
    """
    return node.children[0].tables() if node.children else frozenset()

def classify_matched(pg: PlanNode, llm: PlanNode) -> list:
    """
    compare a matched pair of nodes, returning a list of differences found.
    list is empty if no differences found
    """
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
            # Same join algorithm: check whether the two inputs are swapped
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
        if a and b and a != b:           # compared only when both plans give a join predicate
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
            
    elif pg.node_type != llm.node_type:   # not reachable: other keys include the node type
        found.append(Difference("operator_changed", f"{pg.node_type} vs {llm.node_type}.", pg, llm))

    return found

def describe_unmatched(node: PlanNode, only_in: str) -> Difference:
    """
    Describe a node that has no partner in the other plan.

    Returns:
        Difference: a join_order difference for joins, otherwise
        operator_missing or operator_extra.
    """
    plan_name, other = ("PostgreSQL's plan", "the AI plan") if only_in == "pg" else ("the AI plan", "PostgreSQL's plan")
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
# Main comparison
# ---------------------------------------------------------------------------
def compare_plans(pg_plan: PlanNode, llm_plan: PlanNode) -> ComparisonResult:
    """
    compare PostgreSQL's plan with the LLM plan.
    """
    for node in list(pg_plan.walk()) + list(llm_plan.walk()):
        node.status, node.note = ("ignored" if is_ignored(node) else "unchanged"), ""

    pg_keys, llm_keys = keyed_nodes(pg_plan), keyed_nodes(llm_plan)
    result = ComparisonResult()

    # Nodes of PostgreSQL's plan: matched (changed / unchanged) or missing
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
        if diffs:                       # a node is counted once, however many differences it has
            pg_node.status = llm_node.status = "changed"
            pg_node.note = llm_node.note = " ".join(d.text for d in diffs)
            result.changed += 1
            result.differences.extend(diffs)

    # Nodes found only in the LLM plan
    for key, llm_node in llm_keys.items():
        if key not in pg_keys:
            diff = describe_unmatched(llm_node, "llm")
            llm_node.status, llm_node.note = "extra", diff.text
            result.extra += 1
            result.differences.append(diff)

    return result