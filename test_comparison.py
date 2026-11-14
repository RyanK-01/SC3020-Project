"""
test_comparison.py
==================
Offline checks for comparison.py (no database, no LLM key needed).
Run:   python -m unittest test_comparison -v
"""
import importlib.util
import unittest

import comparison as cmp
import preprocessing as pp
import batch


def scan(table, **extra):
    return {"Node Type": "Seq Scan", "Relation Name": table, **extra}


def hash_join(probe, build, cond="(a_key = b_key)"):
    return {"Node Type": "Hash Join", "Join Type": "Inner", "Hash Cond": cond,
            "Plans": [probe, {"Node Type": "Hash", "Plans": [build]}]}


def run(pg_dict, llm_dict):
    pg, llm = pp.plan_from_dict(pg_dict), pp.plan_from_dict(llm_dict)
    return cmp.compare_plans(pg, llm), pg, llm


def kinds(result):
    return sorted(d.kind for d in result.differences)


class ComparisonTests(unittest.TestCase):
    def test_identical_plans_have_rate_zero(self):
        plan = hash_join(scan("a"), scan("b"))
        result, _, _ = run(plan, plan)
        self.assertEqual(result.rate, 0.0)
        self.assertEqual(result.differences, [])

    def test_different_scan_method_is_reported(self):
        a = hash_join(scan("a"), scan("b"))
        b = hash_join({"Node Type": "Index Scan", "Relation Name": "a", "Index Name": "i"},
                      scan("b"))
        result, _, _ = run(a, b)
        self.assertEqual(kinds(result), ["scan_method"])
        self.assertEqual(result.changed, 1)

    def test_parallel_nodes_are_ignored(self):
        pg_plan, llm_plan = pp.sample_plans()
        result = cmp.compare_plans(pg_plan, llm_plan)
        self.assertEqual(result.missing, 0)           # Gather + Partial Aggregate skipped
        statuses = {n.node_type: n.status for n in pg_plan.walk() if n.node_type == "Gather"}
        self.assertEqual(statuses["Gather"], "ignored")
        self.assertTrue(any(n.status == "ignored" and n.node_type == "Hash"
                            for n in pg_plan.walk()))

    def test_missing_filter_is_reported(self):
        a = hash_join(scan("a", Filter="(a_date > '1995-01-01'::date)"), scan("b"))
        b = hash_join(scan("a"), scan("b"))
        result, _, _ = run(a, b)
        self.assertEqual(kinds(result), ["scan_condition"])

    def test_same_filter_written_differently_is_equal(self):
        a = hash_join(scan("a", Filter="((a_date >= '1995-01-01'::date) AND "
                                       "(a_date < '1995-02-01 00:00:00'::timestamp without time zone))"),
                      scan("b"))
        b = hash_join({"Node Type": "Index Scan", "Relation Name": "a", "Index Name": "x",
                       "Index Cond": "a.a_date >= DATE '1995-01-01' AND a.a_date < DATE '1995-02-01'"},
                      scan("b"))
        result, _, _ = run(a, b)
        self.assertEqual(kinds(result), ["scan_method"])   # only the scan type differs

    def test_join_predicate_in_index_cond_is_not_a_filter(self):
        a = {"Node Type": "Nested Loop", "Join Type": "Inner", "Plans": [
            scan("a"), {"Node Type": "Index Scan", "Relation Name": "b", "Index Name": "b_pkey",
                        "Index Cond": "(b.b_key = a.a_key)"}]}
        b = {"Node Type": "Nested Loop", "Join Type": "Inner", "Plans": [
            scan("a"), {"Node Type": "Index Scan", "Relation Name": "b", "Index Name": "b_pkey"}]}
        result, _, _ = run(a, b)
        self.assertEqual(result.differences, [])

    def test_swapped_hash_build_side_is_reported(self):
        a = hash_join(scan("a"), scan("b"))
        b = hash_join(scan("b"), scan("a"))
        result, _, _ = run(a, b)
        self.assertEqual(kinds(result), ["join_sides"])
        self.assertEqual(result.changed, 1)

    def test_swapped_nested_loop_outer_is_reported(self):
        a = {"Node Type": "Nested Loop", "Join Type": "Inner", "Plans": [scan("a"), scan("b")]}
        b = {"Node Type": "Nested Loop", "Join Type": "Inner", "Plans": [scan("b"), scan("a")]}
        result, _, _ = run(a, b)
        self.assertEqual(kinds(result), ["join_sides"])

    def test_different_join_condition_is_reported(self):
        a = hash_join(scan("a"), scan("b"), "(a_key = b_key)")
        b = hash_join(scan("a"), scan("b"), "(a_other = b_other)")
        result, _, _ = run(a, b)
        self.assertEqual(kinds(result), ["join_condition"])

    def test_different_join_order_is_reported(self):
        ab_c = hash_join(hash_join(scan("a"), scan("b")), scan("c"))
        a_bc = hash_join(scan("a"), hash_join(scan("b"), scan("c")))
        result, _, _ = run(ab_c, a_bc)
        self.assertIn("join_order", kinds(result))
        self.assertGreater(result.rate, 0)

    def test_one_node_with_two_differences_counts_once(self):
        a = hash_join(scan("a", Filter="(a_x > 1)"), scan("b"))
        b = hash_join({"Node Type": "Index Scan", "Relation Name": "a", "Index Name": "i"},
                      scan("b"))
        result, _, _ = run(a, b)
        self.assertEqual(sorted(kinds(result)), ["scan_condition", "scan_method"])
        self.assertEqual(result.changed, 1)

    def test_condition_helpers(self):
        self.assertEqual(cmp.selection_columns("((x > 1) AND (y = 'a'::bpchar))"),
                         frozenset({"x", "y"}))
        self.assertEqual(cmp.selection_columns("(a.k = b.k)"), frozenset())
        self.assertEqual(cmp.join_pairs("(a.k = b.k)"), frozenset({("k", "k")}))
        self.assertEqual(cmp.join_pairs("(l.l_partkey = part.p_partkey)"),
                         frozenset({("l_partkey", "p_partkey")}))
        self.assertEqual(cmp.selection_columns("((p_brand = 'x'::bpchar) OR (p_size > 3))"),
                         frozenset({"p_brand", "p_size"}))


class BatchHelperTests(unittest.TestCase):
    def test_split_queries(self):
        text = """-- first
SELECT 1;
-- second, with a ; inside a string and a comment
SELECT 'a;b' FROM t -- trailing comment
WHERE x = 1;

SELECT 3"""
        queries = batch.split_queries(text)
        self.assertEqual(len(queries), 3)
        self.assertIn("'a;b'", queries[1])
        self.assertNotIn("comment", queries[1])
        self.assertEqual(queries[2], "SELECT 3")

    def test_summarize(self):
        rows = [
            {"rate": 0.0, "difference_kinds": "", "error": ""},
            {"rate": 0.5, "difference_kinds": "scan_method; scan_method; join_order", "error": ""},
            {"rate": 0.0, "difference_kinds": "", "error": "boom"},
        ]
        s = batch.summarize(rows)
        self.assertEqual((s["queries"], s["succeeded"], s["failed"], s["identical"]), (3, 2, 1, 1))
        self.assertAlmostEqual(s["mean_rate"], 0.25)
        self.assertEqual(s["kind_queries"], {"scan_method": 1, "join_order": 1})
        self.assertEqual(s["kind_total"]["scan_method"], 2)
        self.assertIn("mean 25.0%", batch.format_summary(s))

    def test_summarize_with_no_successes(self):
        s = batch.summarize([{"rate": 0.0, "difference_kinds": "", "error": "x"}])
        self.assertIsNone(s["mean_rate"])
        batch.format_summary(s)        # must not raise


class _FakeCursor:
    """Stands in for a psycopg2 cursor: returns canned rows, one list per execute()."""
    def __init__(self, results):
        self.results = list(results)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.current = self.results.pop(0)

    def fetchall(self):
        return self.current


class _FakeConn:
    def __init__(self, results):
        self.results = results

    def cursor(self):
        return _FakeCursor(self.results)

    def rollback(self):
        pass


# canned catalog rows from pg_class
_PG_CLASS = [("lineitem", 600000.0, 11000), ("orders", 150000.0, 2500), ("part", -1.0, 0)]
_QUERY = ("SELECT l_returnflag, count(*) FROM lineitem JOIN orders "
          "ON l_orderkey = o_orderkey GROUP BY l_returnflag")


@unittest.skipUnless(importlib.util.find_spec("psycopg2"), "psycopg2 is not installed")
class StatsTests(unittest.TestCase):
    def stats(self, query=_QUERY):
        return pp.get_stats(_FakeConn([_PG_CLASS]), "public", query)

    def test_only_tables_in_the_query_are_kept(self):
        stats = self.stats()
        self.assertEqual(list(stats), ["tables"])        # table sizes only
        self.assertEqual(list(stats["tables"]), ["lineitem", "orders"])

    def test_all_tables_are_kept_when_none_is_named(self):
        self.assertEqual(len(self.stats("SELECT 1")["tables"]), 3)
        self.assertEqual(len(self.stats(None)["tables"]), 3)

    def test_unanalyzed_table_has_no_row_count(self):
        stats = self.stats(None)
        self.assertIsNone(stats["tables"]["part"]["rows"])
        self.assertIn("part: statistics unavailable", pp.stats_to_text(stats))

    def test_stats_text(self):
        text = pp.stats_to_text(self.stats())
        self.assertIn("lineitem: 600000 rows, 11000 pages", text)
        self.assertIn("orders: 150000 rows, 2500 pages", text)
        self.assertNotIn("COLUMN STATISTICS", text)
        self.assertNotIn("KEYS", text)

    def test_empty_stats_text(self):
        text = pp.stats_to_text({"tables": {}})
        self.assertEqual(text.count("(none)"), 1)


class PromptTests(unittest.TestCase):
    def test_prompt_without_stats_is_the_schema_only_prompt(self):
        prompt = pp.build_prompt("SELECT 1", "TABLES:\n  t(a integer)")
        self.assertNotIn("STATISTICS", prompt)
        self.assertTrue(prompt.endswith("whose single child is the build side.\n"))

    def test_prompt_with_stats(self):
        prompt = pp.build_prompt("SELECT 1", "TABLES:\n  t(a integer)",
                                 "TABLE STATISTICS:\n  t: 5 rows, 1 pages")
        self.assertIn("t: 5 rows, 1 pages\n\nQUERY:", prompt)
        self.assertIn("Use the estimated table sizes in TABLE STATISTICS", prompt)


if __name__ == "__main__":
    unittest.main()
