# SC3020: Project 2 (PostgreSQL vs LLM)

Retrieves an execution plan from PostgreSQL (`EXPLAIN (FORMAT JSON)`) given an SQL query, and asks an LLM (Google Gemini) to predict the plan for the same query, and compares the two. A Tkinter GUI is used to visually render the plans for comparison purposes.

## Prerequisites

- **Python 3.9+** with Tkinter
- **PostgreSQL** (tested on 16) with the **TPC-H database already created and loaded**, containing the tables `region`, `nation`, `part`, `supplier`, `partsupp`, `customer`, `orders` and `lineitem`. Setting up TPC-H is not covered here. Run `ANALYZE` after loading.
- **A Google Gemini API key** from [Google AI Studio](https://aistudio.google.com/). Not needed to view the demo plans. Our group provides a key with our submission for convenience purposes that will be deleted after the project grading is completed.
  - As this is a free plan, usage limits apply and the project might not be able to call the API if limits are hit. You are free to provide your own key for ease of tracking of limits.
  - **Gemini 3.1 Flash Lite** is currently the default model called as it has the most generous limits available at the time of submission.

## Installation

Install the two required packages:

```bash
pip install -r requirements.txt
```

## (optional) Setting up a .env file

You can type the database connection details and API key straight into the GUI's connection bar each time you run it, so no configuration file is needed.

If you would rather not retype them everytime the GUI is closed, create a file named `.env` in the same folder as `project.py`. It is loaded automatically at startup and its values pre-fill the connection bar:

```ini
PGHOST=localhost
PGPORT=5432
PGDATABASE=TPC-H
PGUSER=postgres
PGPASSWORD=your_password
PGSCHEMA=public
GEMINI_API_KEY=your_gemini_api_key
```

Every line is optional. Any value left out defaults to the value below. The same names can also be set as ordinary environment variables instead of using a `.env` file.

| Variable | Default | Purpose |
|---|---|---|
| `PGHOST` | `localhost` | Database host |
| `PGPORT` | `5432` | Database port |
| `PGDATABASE` | `TPC-H` | Database name |
| `PGUSER` | `postgres` | Database user |
| `PGPASSWORD` | *(empty)* | Database password |
| `PGSCHEMA` | `public` | Schema that holds the TPC-H tables |
| `GEMINI_API_KEY` | *(empty)* | Gemini API key |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Gemini model to use (can only be changed here, not in the GUI) |

## Usage

From the folder containing the project files:

```bash
python project.py
```

1. Check the connection fields at the top and fill in anything missing, such as the password or API key.
2. Paste one SQL query into the query box.
3. Click **Compare**. The PostgreSQL plan appears on the left, the AI plan on the right, and the differences below.

Example query to try:

```sql
SELECT l_orderkey, sum(l_extendedprice * (1 - l_discount)) AS revenue, o_orderdate
FROM customer, orders, lineitem
WHERE c_mktsegment = 'BUILDING' AND c_custkey = o_custkey AND l_orderkey = o_orderkey
  AND o_orderdate < date '1995-03-15' AND l_shipdate > date '1995-03-15'
GROUP BY l_orderkey, o_orderdate
ORDER BY revenue DESC
LIMIT 10;
```

Other controls:

- **Load demo plans (no DB / LLM)**: shows two built-in sample plans, with no database or API key needed. Useful for checking the GUI works.
- **Send statistics to AI**: when ticked, the prompt includes the row counts of the tables used in the query as well as the schema and indexes.
- **View EXPLAIN output / View LLM prompt / View LLM response**: show the raw data behind the last comparison.

Node colours:

| Colour legend | Colour | Meaning |
|---|---|---|
| 🟩 Same in both | Green | Operator matched with no differences |
| 🟧 Different operator | Orange | Matched category, but differs (e.g. Seq Scan vs Index Scan) |
| 🟥 Only in PostgreSQL | Red | Missing from the AI plan |
| 🟦 Only in AI plan | Blue | Not in PostgreSQL's plan |
| ⬜ Not compared | Grey | Helper nodes (Hash, Gather, Bitmap Index Scan) left out of the comparison |

## How the comparison works

1. Both plans are parsed into the same tree structure (`PlanNode`). The LLM is asked to answer in the same JSON shape as `EXPLAIN (FORMAT JSON)`.
2. Helper nodes that do not reflect a planning decision (`Hash`, `Parallel Hash`, `Bitmap Index Scan`, `Gather`, `Gather Merge`, partial aggregates) are ignored.
3. Each remaining node is given a key from its category and the tables under it. For example, a Seq Scan and an Index Scan of the same table are matched as the same scan, and joins are matched by the set of tables they join.
4. Matched pairs are checked for differences. Unmatched nodes are reported as missing, extra, or as a different join order.

Kinds of difference reported:

| Kind | Meaning |
|---|---|
| `scan_method` | Different scan operator, or same operator on a different index |
| `scan_condition` | Different columns filtered on (literal values are ignored) |
| `join_algorithm` | Hash Join vs Merge Join vs Nested Loop |
| `join_type` | e.g. Inner vs Left |
| `join_sides` | Build/inner side of a Hash Join or Nested Loop swapped |
| `join_condition` | Different join predicate |
| `join_order` | Tables joined in a different order or grouping |
| `aggregate_strategy` | Plain vs Hash vs Group aggregate |
| `operator_missing` / `operator_extra` | Operator only in PostgreSQL's plan / only in the AI plan |

**Difference rate** = (changed + missing + extra) / (matched + missing + extra). 0% means the plans are identical after the steps above.

LLM replies are cached in a `.llm_cache/` folder created next to the project files, keyed by model and prompt, so rerunning the same query does not call the API again. Delete the folder to force fresh replies.

## Project files

| File | Description |
|---|---|
| `project.py` | Entry point: starts the GUI |
| `interface.py` | Tkinter GUI: connection bar, query box, plan tree drawing, differences panel |
| `preprocessing.py` | Database access, schema and statistics, PostgreSQL plans, Gemini prompt and API call, plan parsing |
| `comparison.py` | Matches the two plans and classifies the differences |
