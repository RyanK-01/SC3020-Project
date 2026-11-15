"""
Generate report figures and a step-by-step walkthrough of the plan comparison algorithm.

Run from the repository root:
    ..\\.venv\\Scripts\\python.exe report_figures\\make_figures.py

Needs: the PostgreSQL database configured in .env, Pillow (for screenshots), and a
desktop session (Tk windows are drawn on screen and captured). It never calls the
Gemini API: the worked example reads a reply that is already in .llm_cache.

Outputs (in report_figures/):
    *.png                     figures drawn with interface.draw_tree (same look as the GUI)
    algorithm_walkthrough.md  code excerpts and real outputs for every algorithm step
"""
from __future__ import annotations

import ctypes
import hashlib
import inspect
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)   # true pixel coordinates for screenshots
except Exception:
    pass

import tkinter as tk
from PIL import ImageGrab

import comparison as cmp
import interface as ui
import preprocessing as pp

MD: list = []          # lines of algorithm_walkthrough.md

# cached Gemini reply for example query 5 (TPC-H Q5, prompt without statistics)
KNOWN_Q5_REPLY = "2fe49832eece8acadfd893c50862e745c6fe596ef037101705ab9b17b0b46c57.txt"


def md(*lines):
    MD.extend(lines)


def code(text, lang=""):
    md(f"```{lang}", text.rstrip("\n"), "```", "")


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
def load_queries() -> dict:
    sql = open("example_queries.sql", encoding="utf-8").read()
    out = {}
    for block in re.split(r"\n-- \d+\. ", sql)[1:]:
        title, _, body = block.partition("\n")
        out[title.strip()] = body.strip()
    return out


def raw_pg(conn, query, schema):
    """ real EXPLAIN (FORMAT JSON) output: (root plan dict, pretty JSON text) """
    _, text = pp.get_postgres_plan(conn, query, schema)
    return json.loads(text)[0]["Plan"], text


def tree(d) -> pp.PlanNode:
    return pp.plan_from_dict(json.loads(json.dumps(d)))   # fresh copy every time


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------
TK_ROOT = tk.Tk()
TK_ROOT.withdraw()
TITLE_FONT = ("Segoe UI", 12, "bold")
SUB_FONT = ("Segoe UI", 10, "bold")
CAPTION_FONT = ("Consolas", 9)


def grab(widget, path):
    for _ in range(3):
        widget.update_idletasks()
        widget.update()
        time.sleep(0.15)
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(path)
    print("wrote", os.path.relpath(path, ROOT))


def tree_canvas(parent, root_node):
    c = tk.Canvas(parent, bg="white", highlightthickness=1, highlightbackground="#bbbbbb")
    ui.draw_tree(c, root_node)
    x0, y0, x1, y1 = c.bbox("all")
    c.configure(width=x1 - x0 + 2 * ui.MARGIN, height=y1 - y0 + 2 * ui.MARGIN)
    c.xview_moveto(0)
    c.yview_moveto(0)
    return c


def legend(parent, statuses=None):
    row = tk.Frame(parent, bg="white")
    for status, text in ui.LEGEND:
        if statuses is None or status in statuses:
            tk.Label(row, text=f" {text} ", bg=ui.STATUS_COLORS[status], relief="solid",
                     borderwidth=1, font=("Segoe UI", 9)).pack(side="left", padx=4)
    return row


def pair_figure(path, title, pg_root, llm_root, caption="", legend_statuses=None):
    win = tk.Toplevel(TK_ROOT)
    win.configure(bg="white")
    win.attributes("-topmost", True)
    win.geometry("+0+0")
    frame = tk.Frame(win, bg="white", padx=12, pady=10)
    frame.pack()
    tk.Label(frame, text=title, font=TITLE_FONT, bg="white").grid(row=0, column=0, columnspan=2, sticky="w")
    for col, (name, node) in enumerate((("PostgreSQL plan", pg_root), ("AI plan", llm_root))):
        tk.Label(frame, text=name, font=SUB_FONT, bg="white").grid(row=1, column=col, pady=(6, 2))
        tree_canvas(frame, node).grid(row=2, column=col, padx=6, sticky="n")
    legend(frame, legend_statuses).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
    if caption:
        tk.Label(frame, text=caption, font=CAPTION_FONT, bg="white", justify="left",
                 anchor="w").grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
    grab(frame, path)
    win.destroy()


def fmt_key(key) -> str:
    (kind, what), occurrence = key
    what = what if isinstance(what, str) or what is None else "{" + ", ".join(sorted(what)) + "}"
    return f"({kind}, {what})" + (f" #{occurrence}" if occurrence else "")


def annotate_keys(root_node):
    """ replace each box's label with operator + matching key; colour only ignored nodes """
    inverse = {id(n): k for k, n in cmp.keyed_nodes(root_node).items()}
    for n in root_node.walk():
        base = pp.PlanNode.label(n)
        if cmp.is_ignored(n):
            extra, n.status = "\n(ignored)", "ignored"
        else:
            extra, n.status = "\nkey: " + fmt_key(inverse[id(n)]), "_pending"   # white box
        n.label = (lambda s=base + extra: s)
        n.conditions = n.sort_key = None
    return root_node


def caption_for(result, width=150) -> str:
    lines = [f"{i}. [{d.kind}] {d.text}" for i, d in enumerate(result.differences, 1)] or ["No differences."]
    lines.append("")
    lines.append(rate_line(result))
    return "\n".join(wrap(l, width) if width else l for l in lines)


def rate_line(r) -> str:
    return (f"Difference rate = (changed {r.changed} + missing {r.missing} + extra {r.extra}) / "
            f"(matched {r.matched} + missing {r.missing} + extra {r.extra}) = {r.rate:.0%}")


def wrap(line, width):
    out, cur = [], ""
    for word in line.split(" "):
        if len(cur) + len(word) + 1 > width and cur:
            out.append(cur)
            cur = "      " + word
        else:
            cur = f"{cur} {word}" if cur else word
    out.append(cur)
    return "\n".join(out)


def node_fields(n: pp.PlanNode) -> dict:
    keys = ("node_type", "relation", "index_name", "join_type", "selection", "join_cond", "partial_mode", "sort_key")
    return {k: getattr(n, k) for k in keys if getattr(n, k) is not None} | {"children": len(n.children)}


def source_lines(obj, start_pat=None, end_pat=None) -> str:
    src = inspect.getsource(obj)
    if start_pat:
        lines = src.splitlines()
        s = next(i for i, l in enumerate(lines) if re.search(start_pat, l))
        e = next(i for i, l in enumerate(lines[s + 1:], s + 1) if re.search(end_pat, l)) if end_pat else len(lines)
        src = "\n".join(lines[s:e])
    return inspect.cleandoc(src) if start_pat else src


def short(d: dict) -> dict:
    """ an EXPLAIN node without its children, for display """
    return {k: v for k, v in d.items() if k != "Plans"}


# ---------------------------------------------------------------------------
# Hand-written AI plans (illustrative) for the classification gallery
# ---------------------------------------------------------------------------
def seq(rel, flt=None):
    d = {"Node Type": "Seq Scan", "Relation Name": rel}
    if flt:
        d["Filter"] = flt
    return d


def hashed(child):
    return {"Node Type": "Hash", "Plans": [child]}


def hj(cond, probe, build, jt="Inner"):
    return {"Node Type": "Hash Join", "Join Type": jt, "Hash Cond": cond, "Plans": [probe, hashed(build)]}


GALLERY = [
    dict(
        file="gallery_1_scan_method.png", kind="scan_method",
        title="scan_method: same table, different access path (query 6: primary-key lookup)",
        pg={"Node Type": "Index Scan", "Relation Name": "orders", "Index Name": "orders_pkey",
            "Index Cond": "(o_orderkey = 1)"},
        llm=seq("orders", "o_orderkey = 1"),
        note="Both scans are keyed (scan, orders), so they are matched. The filter column is the same "
             "(o_orderkey) even though PostgreSQL puts it in Index Cond and the AI in Filter, so only the "
             "access path is reported.",
    ),
    dict(
        file="gallery_2_scan_condition.png", kind="scan_condition",
        title="scan_condition: the AI plan drops a filter column (query 1: TPC-H Q6)",
        pg={"Node Type": "Aggregate", "Strategy": "Plain", "Partial Mode": "Finalize", "Plans": [
            {"Node Type": "Gather", "Plans": [
                {"Node Type": "Aggregate", "Strategy": "Plain", "Partial Mode": "Partial", "Plans": [
                    seq("lineitem", "((l_shipdate >= '1994-01-01'::date) AND (l_shipdate < '1995-01-01'::date) "
                                    "AND (l_discount >= 0.05) AND (l_discount <= 0.07) "
                                    "AND (l_quantity < '24'::numeric))")]}]}]},
        llm={"Node Type": "Aggregate", "Strategy": "Plain", "Plans": [
            seq("lineitem", "l_shipdate >= '1994-01-01' AND l_shipdate < '1995-01-01' "
                            "AND l_discount >= 0.05 AND l_discount <= 0.07")]},
        note="Literal values, ::casts and brackets are ignored; only the set of filtered columns is compared. "
             "Gather and the Partial aggregate are helper nodes (grey), so the Finalize aggregate matches "
             "the AI's single Aggregate.",
    ),
    dict(
        file="gallery_3_join_algorithm.png", kind="join_algorithm",
        title="join_algorithm: Nested Loop vs Hash Join (query 4: TPC-H Q3)",
        pg={"Node Type": "Limit", "Plans": [{"Node Type": "Sort", "Sort Key": ["(sum(...)) DESC", "orders.o_orderdate"], "Plans": [
            {"Node Type": "Aggregate", "Strategy": "Hashed", "Plans": [{"Node Type": "Gather", "Plans": [
                {"Node Type": "Nested Loop", "Join Type": "Inner", "Plans": [
                    hj("(orders.o_custkey = customer.c_custkey)",
                       seq("orders", "(o_orderdate < '1995-03-15'::date)"),
                       seq("customer", "(c_mktsegment = 'BUILDING'::bpchar)")),
                    {"Node Type": "Index Scan", "Relation Name": "lineitem", "Index Name": "lineitem_pkey",
                     "Index Cond": "(l_orderkey = orders.o_orderkey)",
                     "Filter": "(l_shipdate > '1995-03-15'::date)"}]}]}]}]}]},
        llm={"Node Type": "Limit", "Plans": [{"Node Type": "Sort", "Sort Key": ["revenue DESC", "o_orderdate"], "Plans": [
            {"Node Type": "HashAggregate", "Plans": [
                hj("l_orderkey = o_orderkey",
                   seq("lineitem", "l_shipdate > '1995-03-15'"),
                   {"Node Type": "Hash Join", "Join Type": "Inner", "Hash Cond": "o_custkey = c_custkey", "Plans": [
                       seq("orders", "o_orderdate < '1995-03-15'"),
                       hashed(seq("customer", "c_mktsegment = 'BUILDING'"))]})]}]}]},
        note="The top joins are both keyed (join, {customer, lineitem, orders}) and so are matched even "
             "though the algorithm differs. The AI also reads lineitem with a Seq Scan instead of the "
             "Index Scan PostgreSQL uses on the inner side of its Nested Loop, so that scan is changed too.",
    ),
    dict(
        file="gallery_4_join_type.png", kind="join_type",
        title="join_type: same algorithm, different join semantics (query 8: outer join)",
        pg={"Node Type": "Aggregate", "Strategy": "Hashed", "Plans": [
            hj("(orders.o_custkey = customer.c_custkey)",
               seq("orders", "((o_comment)::text !~~ '%special%requests%'::text)"),
               {"Node Type": "Index Only Scan", "Relation Name": "customer", "Index Name": "customer_pkey"},
               jt="Right")]},
        llm={"Node Type": "HashAggregate", "Plans": [
            hj("o_custkey = c_custkey",
               seq("orders", "o_comment NOT LIKE '%special%requests%'"),
               {"Node Type": "Index Only Scan", "Relation Name": "customer", "Index Name": "customer_pkey"},
               jt="Inner")]},
        note="The join type is only compared when both plans use the same algorithm and both state a join "
             "type. Here the AI turned the LEFT JOIN into an inner join.",
    ),
    dict(
        file="gallery_5_join_sides.png", kind="join_sides",
        title="join_sides: build and probe sides swapped (query 3: TPC-H Q14)",
        pg={"Node Type": "Aggregate", "Strategy": "Plain", "Plans": [
            hj("(lineitem.l_partkey = part.p_partkey)",
               seq("lineitem", "((l_shipdate >= '1993-11-01'::date) AND (l_shipdate < '1993-12-01 00:00:00'::timestamp without time zone))"),
               seq("part"))]},
        llm={"Node Type": "Aggregate", "Strategy": "Plain", "Plans": [
            hj("p_partkey = l_partkey",
               seq("part"),
               seq("lineitem", "l_shipdate >= '1993-11-01' AND l_shipdate < '1993-12-01'"))]},
        note="For a Hash Join the first child is the probe side and the Hash child is the build side. "
             "PostgreSQL hashes the small part table; the AI hashes lineitem. The join condition is written "
             "the other way round (p = l instead of l = p) but is still recognised as the same pair.",
    ),
    dict(
        file="gallery_6_join_condition.png", kind="join_condition",
        title="join_condition: the AI joins on fewer columns (illustrative partsupp-lineitem join)",
        pg=hj("((lineitem.l_partkey = partsupp.ps_partkey) AND (lineitem.l_suppkey = partsupp.ps_suppkey))",
              seq("lineitem"), seq("partsupp")),
        llm=hj("l_partkey = ps_partkey", seq("lineitem"), seq("partsupp")),
        note="Join predicates are reduced to sorted column pairs, so table qualifiers and the order of the "
             "two sides do not matter. The AI plan is missing the (l_suppkey, ps_suppkey) pair.",
    ),
    dict(
        file="gallery_7_aggregate_strategy.png", kind="aggregate_strategy",
        title="aggregate_strategy: GroupAggregate vs HashAggregate (query 2: TPC-H Q1)",
        pg={"Node Type": "Aggregate", "Strategy": "Sorted", "Partial Mode": "Finalize",
            "Group Key": ["l_returnflag", "l_linestatus"], "Plans": [
            {"Node Type": "Gather Merge", "Plans": [
                {"Node Type": "Sort", "Sort Key": ["l_returnflag", "l_linestatus"], "Plans": [
                    {"Node Type": "Aggregate", "Strategy": "Hashed", "Partial Mode": "Partial", "Plans": [
                        seq("lineitem", "(l_shipdate <= '1998-09-02'::date)")]}]}]}]},
        llm={"Node Type": "Sort", "Sort Key": "l_returnflag, l_linestatus", "Plans": [
            {"Node Type": "HashAggregate", "Plans": [seq("lineitem", "l_shipdate <= '1998-09-02'")]}]},
        note="PostgreSQL's 'Aggregate' with Strategy 'Sorted' becomes GroupAggregate when parsed. The Sort "
             "nodes sit at different depths but both are keyed (Sort, {lineitem}), so they still match: "
             "matching does not depend on position in the tree.",
    ),
    dict(
        file="gallery_8_join_order.png", kind="join_order",
        title="join_order: the plans join the tables in a different order (3-table join)",
        pg=hj("(lineitem.l_orderkey = orders.o_orderkey)",
              seq("lineitem"),
              {"Node Type": "Hash Join", "Join Type": "Inner", "Hash Cond": "(orders.o_custkey = customer.c_custkey)",
               "Plans": [seq("orders"), hashed(seq("customer"))]}),
        llm={"Node Type": "Hash Join", "Join Type": "Inner", "Hash Cond": "o_custkey = c_custkey", "Plans": [
            hj("l_orderkey = o_orderkey", seq("lineitem"), seq("orders")),
            hashed(seq("customer"))]},
        note="The inner joins are keyed by the tables beneath them: {customer, orders} in PostgreSQL's plan and "
             "{lineitem, orders} in the AI plan. Neither has a partner, so each is reported as join_order. "
             "The top join still matches; its three differences are counted as ONE changed node.",
    ),
    dict(
        file="gallery_9_missing_extra.png", kind="operator_missing / operator_extra",
        title="operator_missing / operator_extra (query 7: IN-subquery)",
        pg={"Node Type": "Limit", "Plans": [{"Node Type": "Sort", "Sort Key": ["supplier.s_acctbal DESC"], "Plans": [
            hj("(partsupp.ps_suppkey = supplier.s_suppkey)", seq("partsupp", "(ps_availqty < 100)"), seq("supplier"))]}]},
        llm={"Node Type": "Sort", "Sort Key": "s_acctbal DESC", "Plans": [
            hj("ps_suppkey = s_suppkey",
               {"Node Type": "HashAggregate", "Plans": [seq("partsupp", "ps_availqty < 100")]},
               seq("supplier"))]},
        note="The AI forgot the LIMIT (red: only in PostgreSQL) and de-duplicates the subquery with an extra "
             "HashAggregate (blue: only in the AI plan).",
    ),
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    queries = load_queries()
    q14 = queries["Two-table join (TPC-H Q14 style, the example in the project brief)"]
    q5 = queries["Five-table join (TPC-H Q5 style)"]
    selfjoin = ("SELECT n1.n_name, n2.n_name FROM nation n1 JOIN nation n2 "
                "ON n1.n_regionkey = n2.n_regionkey WHERE n1.n_nationkey < n2.n_nationkey")

    cfg = pp.DBConfig.from_env()
    conn = pp.connect_db(cfg)
    schema = pp.get_schema(conn, cfg.schema)
    q14_pg, _ = raw_pg(conn, q14, cfg.schema)
    q5_pg, q5_pg_text = raw_pg(conn, q5, cfg.schema)
    self_pg, _ = raw_pg(conn, selfjoin, cfg.schema)
    conn.close()

    # illustrative AI plan for Q14 (same as the GUI's demo AI plan)
    q14_llm = {"Node Type": "Aggregate", "Strategy": "Plain", "Plans": [
        {"Node Type": "Hash Join", "Join Type": "Inner", "Hash Cond": "l_partkey = p_partkey", "Plans": [
            {"Node Type": "Index Scan", "Relation Name": "lineitem", "Index Name": "idx_lineitem_shipdate",
             "Index Cond": "l_shipdate >= '1993-11-01' AND l_shipdate < '1993-12-01'"},
            {"Node Type": "Hash", "Plans": [{"Node Type": "Sequential Scan", "Relation Name": "part"}]}]}]}

    md("# Plan comparison algorithm: step-by-step illustrations", "",
       "Generated by `report_figures/make_figures.py`. Every output below was produced by running the "
       "project code. PostgreSQL plans come from `EXPLAIN (FORMAT JSON)` on the local TPC-H database. "
       "AI plans in steps 1 to 6 and in the gallery are **hand-written for illustration**; the worked example "
       "uses a **real Gemini reply** read from `.llm_cache`.", "",
       "Running example for steps 1 to 6: query 3 from `example_queries.sql` (TPC-H Q14):", "")
    code(q14, "sql")

    # ---------------- Step 1 ----------------
    md("## Step 1: Parse both plans into PlanNode trees (`preprocessing.plan_from_dict`)", "",
       "PostgreSQL's EXPLAIN output has many fields (costs, row estimates, widths, parallel flags). Only the "
       "fields that describe *what* the plan does are kept. Here is the root node exactly as PostgreSQL "
       "returns it (children removed):", "")
    code(json.dumps(short(q14_pg), indent=2), "json")
    md("After `plan_from_dict`:", "")
    code(json.dumps(node_fields(tree(q14_pg)), indent=2), "json")
    md("PostgreSQL reports every aggregate as `Aggregate` with a separate `Strategy`. It is renamed so "
       "that it can be compared with the node names an LLM uses:", "")
    code(source_lines(pp.plan_from_dict, r"PostgreSQL reports every aggregate", r"conditions = |# gather predicate"), "python")
    md("The AI reply goes through the same function. `normalize_node_type` maps the LLM's own wording to "
       "PostgreSQL names, and predicates from any condition key are merged into `selection` or `join_cond`:", "")
    llm_frag = {"Node Type": "Sequential Scan", "Relation Name": "part"}
    llm_join = {"Node Type": "hash join", "Join Type": "Inner", "Condition": "l.l_partkey = p.p_partkey"}
    rows = [("LLM node", "Parsed PlanNode fields")]
    for frag in (llm_frag, llm_join, {"Node Type": "Hash Aggregate"}, {"Node Type": "Full Table Scan", "Relation Name": "orders"}):
        rows.append((json.dumps(frag), json.dumps(node_fields(pp.plan_from_dict(frag)))))
    md("| LLM node (JSON) | Parsed fields |", "|---|---|")
    for a, b in rows[1:]:
        md(f"| `{a}` | `{b}` |")
    md("")
    md("Both trees as text (`preprocessing.plan_to_text`):", "")
    code("PostgreSQL plan:\n" + pp.plan_to_text(tree(q14_pg)) + "\n\nAI plan:\n" + pp.plan_to_text(tree(q14_llm)))

    # ---------------- Step 2 ----------------
    md("## Step 2: Leave out helper nodes (`comparison.is_ignored`)", "")
    code(inspect.getsource(cmp.is_ignored), "python")
    code("IGNORED_NODE_TYPES = " + repr(sorted(cmp.IGNORED_NODE_TYPES)), "python")
    md("| Node (PostgreSQL plan) | Partial Mode | Compared? | Why |", "|---|---|---|---|")
    reasons = {"Gather": "parallelism helper, not a relational operator",
               "Gather Merge": "parallelism helper",
               "Hash": "build phase of its parent Hash Join; compared through the join",
               "Bitmap Index Scan": "compared through its parent Bitmap Heap Scan"}
    for n in tree(q14_pg).walk():
        if cmp.is_ignored(n):
            why = reasons.get(n.node_type, "partial aggregate; compared through its Finalize partner")
            md(f"| {pp.PlanNode.label(n).replace(chr(10), ' ')} | {n.partial_mode or ''} | no | {why} |")
        else:
            md(f"| {pp.PlanNode.label(n).replace(chr(10), ' ')} | {n.partial_mode or ''} | yes | |")
    md("")

    # ---------------- Step 3 ----------------
    md("## Step 3: Give every compared node a matching key (`comparison.keyed_nodes`)", "")
    code(inspect.getsource(cmp.category) + "\n\n" + inspect.getsource(cmp.keyed_nodes), "python")
    for name, d in (("PostgreSQL plan", q14_pg), ("AI plan", q14_llm)):
        md(f"**{name}**", "")
        lines = [f"{fmt_key(k):<45} -> {pp.PlanNode.label(n).replace(chr(10), ' ')}"
                 for k, n in cmp.keyed_nodes(tree(d)).items()]
        code("\n".join(lines))
    md("![Matching keys](fig_step3_keys.png)", "",
       "*Figure: matching key of every compared node. Grey nodes are left out (step 2).*", "")
    pair_figure(os.path.join(HERE, "fig_step3_keys.png"),
                "Step 3: matching keys (TPC-H Q14; grey = ignored helper node)",
                annotate_keys(tree(q14_pg)), annotate_keys(tree(q14_llm)), legend_statuses={"ignored"})

    md("**Repeated keys.** When the same table is scanned twice (a self-join), the occurrence number keeps "
       "the two scans apart. Real PostgreSQL plan for:", "")
    code(selfjoin, "sql")
    code("\n".join(f"{fmt_key(k):<45} -> {pp.PlanNode.label(n).replace(chr(10), ' ')}"
                   for k, n in cmp.keyed_nodes(tree(self_pg)).items()))

    # ---------------- Step 4 ----------------
    md("## Step 4: Match nodes by key", "",
       "Both plans are now dictionaries `{key: node}`. A key in both is a **matched** pair, a key only in "
       "PostgreSQL's plan is **missing**, and a key only in the AI plan is **extra**. Each lookup is one "
       "dictionary access.", "")
    pg_keys, llm_keys = cmp.keyed_nodes(tree(q14_pg)), cmp.keyed_nodes(tree(q14_llm))
    md("| Key | PostgreSQL node | AI node | Outcome |", "|---|---|---|---|")
    for k in list(pg_keys) + [k for k in llm_keys if k not in pg_keys]:
        a, b = pg_keys.get(k), llm_keys.get(k)
        outcome = "matched" if a and b else ("missing" if a else "extra")
        md(f"| `{fmt_key(k)}` | {a.node_type if a else '-'} | {b.node_type if b else '-'} | {outcome} |")
    md("")

    # ---------------- Step 5 ----------------
    md("## Step 5: Normalise predicates before comparing them", "",
       "The two plans write the same predicate differently: PostgreSQL adds brackets, `::type` casts and "
       "table qualifiers, while the LLM usually does not. Predicates are split on top-level `AND`s and reduced "
       "to **column names**; literal values are ignored.", "")
    examples = [
        ("selection_columns", "((l_shipdate >= '1993-11-01'::date) AND (l_shipdate < '1993-12-01 00:00:00'::timestamp without time zone))"),
        ("selection_columns", "l_shipdate >= '1993-11-01' AND l_shipdate < '1993-12-01'"),
        ("selection_columns", "((c_mktsegment = 'BUILDING'::bpchar) OR (c_acctbal > '0'::numeric))"),
        ("selection_columns", "(l_orderkey = orders.o_orderkey)"),
        ("join_pairs", "(lineitem.l_partkey = part.p_partkey)"),
        ("join_pairs", "p.p_partkey = l.l_partkey"),
        ("join_pairs", "((lineitem.l_suppkey = supplier.s_suppkey) AND (customer.c_nationkey = supplier.s_nationkey))"),
    ]
    out = []
    for fn, text in examples:
        val = getattr(cmp, fn)(text)
        shown = sorted(val)
        out.append(f"_split_and({text!r})\n  -> {cmp._split_and(text)}\n{fn}(...)\n  -> {shown}\n")
    code("\n".join(out))
    md("A column-to-column predicate inside a scan (e.g. the `Index Cond` of the inner side of a Nested Loop) "
       "is not a filter, so `selection_columns` skips it; it is a join predicate.", "")

    # ---------------- Step 6 ----------------
    md("## Step 6: Classify each matched pair (`comparison.classify_matched`)", "",
       "Each matched pair is checked according to its category. Unmatched nodes are described by "
       "`describe_unmatched` (a join with no partner is reported as `join_order`). One example per "
       "difference kind follows. All figures use the GUI's colours.", "")
    q14_res = cmp.compare_plans(a := tree(q14_pg), b := tree(q14_llm))
    md("**Running example (Q14):**", "")
    code(caption_for(q14_res, None))
    pair_figure(os.path.join(HERE, "fig_step6_q14_result.png"),
                "Step 6: comparison result for TPC-H Q14 (illustrative AI plan)", a, b, caption_for(q14_res))
    md("![Q14 result](fig_step6_q14_result.png)", "")

    md("### Gallery: one example per difference kind", "",
       "| Kind | Category | Reported when |", "|---|---|---|",
       "| `scan_method` | scan | operator or index differs |",
       "| `scan_condition` | scan | the set of filtered columns differs |",
       "| `join_algorithm` | join | Hash Join / Merge Join / Nested Loop differs |",
       "| `join_type` | join | same algorithm, both state a join type, and it differs |",
       "| `join_sides` | join | same Hash Join / Nested Loop, but the first (probe / outer) child covers different tables |",
       "| `join_condition` | join | both give a join predicate and the column pairs differ |",
       "| `aggregate_strategy` | aggregate | Aggregate / HashAggregate / GroupAggregate differs |",
       "| `join_order` | unmatched join | a join's set of tables exists only in one plan |",
       "| `operator_missing` / `operator_extra` | unmatched other | any other node only in one plan |", "")
    for case in GALLERY:
        pg_t, llm_t = tree(case["pg"]), tree(case["llm"])
        res = cmp.compare_plans(pg_t, llm_t)
        pair_figure(os.path.join(HERE, case["file"]), case["title"], pg_t, llm_t, caption_for(res))
        md(f"#### {case['kind']}", "", case["note"], "", f"![{case['kind']}]({case['file']})", "")
        code(caption_for(res, None))

    # ---------------- Step 7 ----------------
    md("## Step 7: Difference rate (`ComparisonResult.rate`)", "")
    code(inspect.getsource(cmp.ComparisonResult.rate.fget), "python")
    md("For the Q14 running example:", "")
    code(rate_line(q14_res))
    md("A node with several differences counts once (see the join_order gallery example: three differences "
       "on one join, one changed node). Ignored helper nodes are in neither the numerator nor the "
       "denominator.", "")

    # ---------------- Worked example ----------------
    md("## Worked example: TPC-H Q5 with a real Gemini reply", "")
    code(q5, "sql")
    prompt = pp.build_prompt(q5, pp.schema_to_text(schema), "")
    model = os.environ.get("GEMINI_MODEL", pp.DEFAULT_GEMINI_MODEL)
    cache = os.path.join(ROOT, ".llm_cache", hashlib.sha256((model + prompt).encode()).hexdigest() + ".txt")
    fallback = None
    if not os.path.exists(cache):
        # reply to the same Q5 prompt from before trailing spaces were stripped from PROMPT_TEMPLATE
        fallback = os.path.join(ROOT, ".llm_cache", KNOWN_Q5_REPLY)
        if os.path.exists(fallback):
            cache = fallback
    if not os.path.exists(cache):
        md("_Skipped: no cached Gemini reply for this prompt (the script does not call the API)._", "")
        print("worked example skipped: no cached reply")
    else:
        reply = open(cache, encoding="utf-8").read()
        md(f"Gemini model `{model}`, prompt without table statistics. The reply was read from the cache, "
           "so no API call was made.", "")
        if cache == fallback:
            md("_(The cached reply was produced with an earlier version of the prompt template that differed "
               "only in trailing whitespace.)_", "")
        pg_t = tree(q5_pg)
        llm_t = pp.plan_from_explain_json(pp.extract_json(reply))
        res = cmp.compare_plans(pg_t, llm_t)
        code("PostgreSQL plan:\n" + pp.plan_to_text(pg_t) + "\n\nGemini plan:\n" + pp.plan_to_text(llm_t))
        md("Matching keys:", "")
        pk, lk = cmp.keyed_nodes(pg_t), cmp.keyed_nodes(llm_t)
        md("| Key | PostgreSQL | Gemini | Outcome |", "|---|---|---|---|")
        for k in list(pk) + [k for k in lk if k not in pk]:
            a, b = pk.get(k), lk.get(k)
            outcome = (b.status if a and b else ("missing" if a else "extra"))
            md(f"| `{fmt_key(k)}` | {a.node_type if a else '-'} | {b.node_type if b else '-'} | {outcome} |")
        md("")
        code(caption_for(res, None))
        pair_figure(os.path.join(HERE, "fig_worked_example_q5.png"),
                    "Worked example: TPC-H Q5, PostgreSQL vs Gemini (real reply)", pg_t, llm_t, caption_for(res))
        md("![Q5 worked example](fig_worked_example_q5.png)", "")

        # full GUI screenshot with the same result
        win = tk.Toplevel(TK_ROOT)
        app = ui.App(win)
        win.geometry("1300x900+0+0")
        win.attributes("-topmost", True)
        app.query_text.insert("1.0", q5)
        pg_t, llm_t = tree(q5_pg), pp.plan_from_explain_json(pp.extract_json(reply))
        app._set_raw({"explain": q5_pg_text, "prompt": prompt, "reply": reply})
        app.show(pg_t, llm_t, cmp.compare_plans(pg_t, llm_t))
        app.status_var.set("Done.")
        app.stats_var.set(False)          # the cached reply came from the prompt without statistics
        win.update()
        for canvas, root_node in ((app.pg_canvas, pg_t), (app.llm_canvas, llm_t)):
            # scroll so the root of each tree is at the top centre of its pane
            column = ui.layout_tree(root_node)[id(root_node)][0]
            root_mid = ui.MARGIN + column * ui.X_GAP + ui.BOX_W / 2
            sx0, sy0, sx1, sy1 = (float(v) for v in str(canvas.cget("scrollregion")).split())
            visible = canvas.winfo_width()
            canvas.xview_moveto(max(0.0, (root_mid - visible / 2 - sx0) / (sx1 - sx0)))
            canvas.yview_moveto(0)
        grab(win, os.path.join(HERE, "fig_gui_q5.png"))
        win.destroy()
        md("![GUI](fig_gui_q5.png)", "", "*Figure: the GUI after comparing TPC-H Q5.*", "")

    with open(os.path.join(HERE, "algorithm_walkthrough.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(MD) + "\n")
    print("wrote report_figures/algorithm_walkthrough.md")


if __name__ == "__main__":
    main()
