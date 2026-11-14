from __future__ import annotations

import re
from dataclasses import dataclass, field

from preprocessing import PlanNode, sample_plans

# helper operators that don't affect decision
IGNORED_NODE_TYPES = {
    "Hash", "Parallel Hash", "Bitmap Index Scan",
    "Gather", "Gather Merge"
}
# node types grouped by category >>> e.g. a Seq Scan and an Index Scan of one table still match
SCAN_TYPES = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"}
JOIN_TYPES = {"Hash Join", "Merge Join", "Nested Loop"}
AGGREGATE_TYPES = {"Aggregate", "HashAggregate", "GroupAggregate"}

@dataclass
class Difference:
    """ one difference found between the two plans. """
    kind: str                           # e.g. "scan_method", "join_order", "operator_missing"
    text: str                           # sentence shown to the user
    pg_node: PlanNode = None            # node in PostgreSQL's plan (None if extra)
    llm_node: PlanNode = None           # node in the LLM plan (None if missing)

@dataclass
class ComparisonResult:
    """ counts and list of differences produced by compare_plans(). """
    differences: list = field(default_factory=list)
    matched: int = 0                    # matched pairs, including changed ones
    changed: int = 0                    # matched pairs with at least one difference
    missing: int = 0                    # nodes only in PostgreSQL's plan
    extra: int = 0                      # nodes only in the LLM plan

    @property
    def rate(self) -> float:
        """ difference rate: (changed + missing + extra) / (matched + missing + extra). """

        total = self.matched + self.missing + self.extra
        # avoid division by zero if both plans contain only ignored nodes
        return (self.changed + self.missing + self.extra) / total if total else 0.0

# ---------------------------------------------------------------------------
# Step 1: node keys
# ---------------------------------------------------------------------------
def category(node_type: str) -> str:
    """ categorize a node type as scan | join | aggregate, else return it unchanged. """

    if node_type in SCAN_TYPES:
        return "scan"
    if node_type in JOIN_TYPES:
        return "join"
    if node_type in AGGREGATE_TYPES:
        return "aggregate"
    
    return node_type  # other operators (Sort, Limit, ...) form their own category

def is_ignored(node: PlanNode) -> bool:
    """ check if a node is left out of the comparison. """
    # partial aggregate compared through its Finalize partner instead
    return node.node_type in IGNORED_NODE_TYPES or node.partial_mode == "Partial"

def keyed_nodes(root: PlanNode) -> dict:
    """ assign a matching key to every compared node of a plan. """
    seen: dict = {}                     # how many times each base key has appeared so far
    keyed: dict = {}                    # (base key, occurrence) -> node
    for node in root.walk():
        if is_ignored(node):
            continue
        cat = category(node.node_type)
        # scans are keyed by their table; other nodes by category and the tables beneath them
        base = ("scan", node.relation) if cat == "scan" else (cat, node.tables())
        # the occurrence number keeps repeated keys apart (e.g. a table scanned twice)
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        keyed[(base, occurrence)] = node
    return keyed

# ---------------------------------------------------------------------------
# Steps 2-3: classifying and describing differences
# ---------------------------------------------------------------------------
def _names(tables) -> str:
    """ format a set of table names for a message. """
    return ", ".join(sorted(tables)) if tables else "?"

def _scan_desc(node: PlanNode) -> str:
    """ describe the access method of a scan node. """
    return node.node_type + (f" on index {node.index_name}" if node.index_name else "")

# --- condition analysis ----------------------------------------------------
_STRING = re.compile(r"'(?:[^']|'')*'")                 # quoted string literals
# type casts such as ::date, ::timestamp without time zone or ::text[]
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
    """ split a condition on its top-level ANDs into a list of predicates. """
    parts, depth, current = [], 0, ""

    # split on brackets and AND
    tokens = re.split(r"(\(|\)|\bAND\b)", text, flags=re.IGNORECASE)
    for tok in tokens:
        # track bracket depth so ANDs nested inside brackets are not split on
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        if tok.upper() == "AND" and depth <= 0:
            # a top-level AND ends the current predicate
            parts.append(current)
            current = ""
        else:
            current += tok
    parts.append(current)  # the last predicate has no AND after it

    # PostgreSQL writes conditions as ((a) AND (b)); remove one outer layer of brackets
    if len(parts) == 1:
        inner = parts[0].strip()
        if inner.startswith("(") and inner.endswith(")"):
            deeper = _split_and(inner[1:-1])
            if len(deeper) > 1:  # only keep the result if the brackets actually hid ANDs
                return deeper
    return [p.strip() for p in parts if p.strip()]

def _columns(predicate: str) -> list:
    """ return the column names mentioned in a predicate, duplicates included. """

    # remove string literals and casts so their words are not mistaken for columns
    text = _CAST.sub(" ", _STRING.sub(" ", predicate))

    # drop any table/alias qualifier so both plans compare equally
    names = [m.split(".")[-1].lower() for m in _IDENT.findall(text)]
    return [n for n in names if n not in _NOT_COLUMNS]

def _has_or(predicate: str) -> bool:
    """ check if a predicate contains an OR. """
    return re.search(r"\bOR\b", predicate, re.IGNORECASE) is not None

def selection_columns(text) -> frozenset:
    """ find columns in selection predicates (one column, or an OR); column-to-column join predicates are skipped. """
    cols = set()
    for pred in _split_and(text or ""):
        found = _columns(pred)
        # one column (e.g. "x > 5") or an OR over columns (e.g. "x = 1 OR y = 2")
        if len(found) == 1 or (found and _has_or(pred)):
            cols |= set(found)
    return frozenset(cols)

def join_pairs(text) -> frozenset:
    """ find the pairs of columns compared in join predicates. """
    pairs = set()
    for pred in _split_and(text or ""):
        found = _columns(pred)
        if len(found) == 2 and not _has_or(pred):
            # sort so "a = b" and "b = a" give the same pair
            pairs.add(tuple(sorted(found)))
    return frozenset(pairs)

def _cols_text(cols) -> str:
    """ format a set of filter columns for a message. """
    return ", ".join(sorted(cols)) if cols else "no filter"

def _pairs_text(pairs) -> str:
    """ format a set of join column pairs for a message. """
    return ", ".join(" = ".join(p) for p in sorted(pairs))

def _first_child_tables(node: PlanNode) -> frozenset:
    """ return the tables under a node's first child (Hash Join probe side / Nested Loop outer side). """
    return node.children[0].tables() if node.children else frozenset()

def classify_matched(pg: PlanNode, llm: PlanNode) -> list:
    """ compare a matched pair of nodes and return their differences (empty list if none). """
    found = []

    # both nodes share a key, so they are in the same category
    cat = category(pg.node_type)
    if cat == "scan":
        # different scan operator or same operator on a different index
        if pg.node_type != llm.node_type or pg.index_name != llm.index_name:
            found.append(Difference(
                "scan_method",
                f"Table '{pg.relation}': PostgreSQL uses {_scan_desc(pg)}, "
                f"the AI plan uses {_scan_desc(llm)}.", pg, llm
            ))
        # compare which columns are filtered on, ignoring the literal values used
        a, b = selection_columns(pg.selection), selection_columns(llm.selection)

        if a != b:
            found.append(Difference(
                "scan_condition",
                f"Table '{pg.relation}': PostgreSQL filters on [{_cols_text(a)}], "
                f"the AI plan on [{_cols_text(b)}].", pg, llm
            ))
            
    elif cat == "join":
        if pg.node_type != llm.node_type:
            found.append(Difference(
                "join_algorithm",
                f"Join of {_names(pg.tables())}: PostgreSQL uses {pg.node_type}, "
                f"the AI plan uses {llm.node_type}.", pg, llm
            ))
        else:
            # e.g. Inner vs Left; only checked when both plans state a join type
            if pg.join_type and llm.join_type and pg.join_type != llm.join_type:
                found.append(Difference(
                    "join_type",
                    f"Join of {_names(pg.tables())}: PostgreSQL does a {pg.join_type} join, "
                    f"the AI plan a {llm.join_type} join.", pg, llm
                ))

            # Same join algorithm: check whether the two inputs are swapped
            if pg.node_type in ("Hash Join", "Nested Loop") and len(pg.children) == 2 and len(llm.children) == 2:
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
                        f"(build/inner side swapped).", pg, llm
                    ))

        a, b = join_pairs(pg.join_cond), join_pairs(llm.join_cond)

        if a and b and a != b:           # compared only when both plans give a join predicate
            found.append(Difference(
                "join_condition",
                f"Join of {_names(pg.tables())}: PostgreSQL joins on "
                f"[{_pairs_text(a)}], the AI plan on [{_pairs_text(b)}].", pg, llm
            ))
            
    elif cat == "aggregate":
        # plain vs hashed vs sorted (group) aggregation
        if pg.node_type != llm.node_type:
            found.append(Difference(
                "aggregate_strategy",
                f"Aggregation over {_names(pg.tables())}: PostgreSQL uses {pg.node_type}, "
                f"the AI plan uses {llm.node_type}.", pg, llm
            ))
            
    elif pg.node_type != llm.node_type:   # not reachable: other keys include the node type
        found.append(Difference("operator_changed", f"{pg.node_type} vs {llm.node_type}.", pg, llm))

    return found

def describe_unmatched(node: PlanNode, only_in: str) -> Difference:
    """ describe an unmatched node: join_order for joins, else operator_missing / operator_extra. """
    plan_name, other = ("PostgreSQL's plan", "the AI plan") if only_in == "pg" else ("the AI plan", "PostgreSQL's plan")
    kind_prefix = "operator_missing" if only_in == "pg" else "operator_extra"

    cat = category(node.node_type)
    # a join keyed by its tables is unmatched when the other plan joins them in a different order
    if cat == "join":
        return Difference(
            "join_order",
            f"{node.node_type} of {_names(node.tables())} appears only in {plan_name}; "
            f"{other} joins these tables in a different order or grouping.",
            node if only_in == "pg" else None, node if only_in == "llm" else None
        )
    
    # store whether operator sits on its table for scans or the tables beneath it
    where = f" on '{node.relation}'" if node.relation else (
        f" over {_names(node.tables())}" if node.tables() else ""
    )
    
    return Difference(
        kind_prefix, 
        f"{node.node_type}{where} appears only in {plan_name}.",
        node if only_in == "pg" else None, 
        node if only_in == "llm" else None
    )

# ---------------------------------------------------------------------------
# Main comparison
# ---------------------------------------------------------------------------
def compare_plans(pg_plan: PlanNode, llm_plan: PlanNode) -> ComparisonResult:
    """ compare PostgreSQL's plan with the LLM plan. """
    for node in list(pg_plan.walk()) + list(llm_plan.walk()):
        node.status, node.note = ("ignored" if is_ignored(node) else "unchanged"), ""

    # nodes with the same key in both plans are treated as the same operator
    pg_keys, llm_keys = keyed_nodes(pg_plan), keyed_nodes(llm_plan)
    result = ComparisonResult()

    # Nodes of PostgreSQL's plan: matched (changed / unchanged) or missing
    for key, pg_node in pg_keys.items():
        llm_node = llm_keys.get(key)
        if llm_node is None:
            # no partner in the LLM plan
            diff = describe_unmatched(pg_node, "pg")
            pg_node.status, pg_node.note = "missing", diff.text
            result.missing += 1
            result.differences.append(diff)
            continue

        result.matched += 1
        diffs = classify_matched(pg_node, llm_node)
        
        # mark both nodes so the GUI can highlight them in each tree
        if diffs:       # a node is counted once, however many differences it has    
            pg_node.status = llm_node.status = "changed"
            pg_node.note = llm_node.note = " ".join(d.text for d in diffs)
            result.changed += 1
            result.differences.extend(diffs)

    # Nodes found only in the LLM plan
    for key, llm_node in llm_keys.items():
        if key not in pg_keys:  # matched keys were already handled in the loop above
            diff = describe_unmatched(llm_node, "llm")
            llm_node.status, llm_node.note = "extra", diff.text
            result.extra += 1
            result.differences.append(diff)

    return result