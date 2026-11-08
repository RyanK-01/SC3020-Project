-- Example queries for the TPC-H database. Paste ONE query at a time into the GUI.
-- Each was checked to run through EXPLAIN on PostgreSQL 16 with TPC-H data.

-- 1. Single table, filter + aggregate (TPC-H Q6 style)
SELECT sum(l_extendedprice * l_discount) AS revenue
FROM lineitem
WHERE l_shipdate >= date '1994-01-01' AND l_shipdate < date '1995-01-01'
  AND l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24;

-- 2. Single table, GROUP BY + ORDER BY (TPC-H Q1 style)
SELECT l_returnflag, l_linestatus, sum(l_quantity), avg(l_extendedprice), count(*)
FROM lineitem
WHERE l_shipdate <= date '1998-09-02'
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus;

-- 3. Two-table join (TPC-H Q14 style, the example in the project brief)
SELECT 100.00 * sum(case when p_type like 'PROMO%'
                         then l_extendedprice * (1 - l_discount) else 0 end)
       / sum(l_extendedprice * (1 - l_discount)) AS promo_revenue
FROM lineitem, part
WHERE l_partkey = p_partkey
  AND l_shipdate >= date '1993-11-01'
  AND l_shipdate < date '1993-11-01' + interval '1' month;

-- 4. Three-table join with GROUP BY, ORDER BY, LIMIT (TPC-H Q3 style)
SELECT l_orderkey, sum(l_extendedprice * (1 - l_discount)) AS revenue, o_orderdate
FROM customer, orders, lineitem
WHERE c_mktsegment = 'BUILDING' AND c_custkey = o_custkey AND l_orderkey = o_orderkey
  AND o_orderdate < date '1995-03-15' AND l_shipdate > date '1995-03-15'
GROUP BY l_orderkey, o_orderdate
ORDER BY revenue DESC, o_orderdate
LIMIT 10;

-- 5. Five-table join (TPC-H Q5 style)
SELECT n_name, sum(l_extendedprice * (1 - l_discount)) AS revenue
FROM customer, orders, lineitem, supplier, nation
WHERE c_custkey = o_custkey AND l_orderkey = o_orderkey AND l_suppkey = s_suppkey
  AND c_nationkey = s_nationkey AND s_nationkey = n_nationkey
  AND o_orderdate >= date '1994-01-01' AND o_orderdate < date '1995-01-01'
GROUP BY n_name
ORDER BY revenue DESC;

-- 6. Primary-key lookup (tests index choice)
SELECT * FROM orders WHERE o_orderkey = 1;

-- 7. Subquery
SELECT s_name, s_acctbal
FROM supplier
WHERE s_suppkey IN (SELECT ps_suppkey FROM partsupp WHERE ps_availqty < 100)
ORDER BY s_acctbal DESC
LIMIT 20;

-- 8. Outer join
SELECT c_custkey, count(o_orderkey)
FROM customer LEFT JOIN orders ON c_custkey = o_custkey AND o_comment NOT LIKE '%special%requests%'
GROUP BY c_custkey;
