# SC3020 Project 2: PostgreSQL vs AI Query Plans

This tool takes an SQL query, gets the query execution plan chosen by PostgreSQL
and the plan predicted by an AI agent (Google Gemini by default), and compares them.
It shows both plans as colour-coded trees, lists the differences in plain text, and
reports a "difference rate".

It does not depend on a particular schema: table, column and index information is
read from the database at run time, so it works with TPC-H, IMDB or any other
PostgreSQL database. The query itself is never executed (only `EXPLAIN` is run).

## 1. Requirements

* Windows 10/11 (the course platform). The code only uses cross-platform libraries.
* Python 3.9 or newer, installed with the **tcl/tk** option (the default on Windows).
  Check with: `python --version` and `python -c "import tkinter"` (no error = OK).
* A PostgreSQL server (version 12 or newer; developed against 16) with the database
  you want to query (see section 3).
* A Google Gemini API key (free keys are available from Google AI Studio).

## 2. Install

Open a command prompt in this folder and run:

```
pip install -r requirements.txt
```

This installs `psycopg2-binary`. Everything else (Tkinter, the HTTP calls to the
LLM) comes with Python.

## 3. Prepare the database

### 3.1 Generate the TPC-H data

Use TPC-H `dbgen` as described in the Appendix of the project brief. It produces
`customer.tbl`, `lineitem.tbl`, `nation.tbl`, `orders.tbl`, `part.tbl`, `partsupp.tbl`,
`region.tbl` and `supplier.tbl`. A small scale factor (0.05 to 1) is enough for this
project. As an alternative to `dbgen`, `pip install tpchgen-cli` followed by
`tpchgen-cli -s 0.1 --output-dir=tpch` produces the same eight `.tbl` files (it was
used to test this tool).

Every line of a `.tbl` file ends with a `|` that PostgreSQL cannot import. Remove it
(for example, in PowerShell, from inside the folder with the files):

```
Get-ChildItem *.tbl | ForEach-Object {
    (Get-Content $_) -replace '\|$','' | Set-Content $_ -Encoding ascii
}
```

### 3.2 Create the database and tables

Either use pgAdmin4 (create a database called `TPC-H`, open the Query Tool on it and
run `tpch_schema.sql`), or use `psql`:

```
psql -U postgres -c "CREATE DATABASE \"TPC-H\""
psql -U postgres -d TPC-H -f tpch_schema.sql
```

`tpch_schema.sql` creates the eight TPC-H tables with their primary keys. It follows the
standard TPC-H column names (`l_orderkey`, `p_partkey`, ...), which is what the queries
in `example_queries.sql` use.

### 3.3 Load the data and collect statistics

With pgAdmin4, right-click each table, choose Import/Export Data, select the file,
format `csv`, delimiter `|`. Or with `psql` (run from the folder holding the `.tbl` files):

```
psql -U postgres -d TPC-H -c "\copy region   FROM 'region.tbl'   WITH (FORMAT csv, DELIMITER '|')"
psql -U postgres -d TPC-H -c "\copy nation   FROM 'nation.tbl'   WITH (FORMAT csv, DELIMITER '|')"
psql -U postgres -d TPC-H -c "\copy part     FROM 'part.tbl'     WITH (FORMAT csv, DELIMITER '|')"
psql -U postgres -d TPC-H -c "\copy supplier FROM 'supplier.tbl' WITH (FORMAT csv, DELIMITER '|')"
psql -U postgres -d TPC-H -c "\copy partsupp FROM 'partsupp.tbl' WITH (FORMAT csv, DELIMITER '|')"
psql -U postgres -d TPC-H -c "\copy customer FROM 'customer.tbl' WITH (FORMAT csv, DELIMITER '|')"
psql -U postgres -d TPC-H -c "\copy orders   FROM 'orders.tbl'   WITH (FORMAT csv, DELIMITER '|')"
psql -U postgres -d TPC-H -c "\copy lineitem FROM 'lineitem.tbl' WITH (FORMAT csv, DELIMITER '|')"
psql -U postgres -d TPC-H -c "ANALYZE"
```

Always run `ANALYZE` after loading: PostgreSQL chooses plans from these statistics, and
without them the plans can be very different.

Any other PostgreSQL database (for example IMDB) can be used instead: enter its name
and schema in the GUI (section 5).

## 4. Set the API key (and optionally the connection defaults)

The GUI has fields for all of these, but you can set them once as environment
variables so the fields are pre-filled.

Command Prompt:

```
set GEMINI_API_KEY=your-key-here
set PGPASSWORD=your-postgres-password
```

PowerShell:

```
$env:GEMINI_API_KEY = "your-key-here"
$env:PGPASSWORD = "your-postgres-password"
```

| Variable | Default | Meaning |
|---|---|---|
| `PGHOST` | `localhost` | PostgreSQL host |
| `PGPORT` | `5432` | PostgreSQL port |
| `PGDATABASE` | `TPC-H` | database name |
| `PGUSER` | `postgres` | database user |
| `PGPASSWORD` | (empty) | database password |
| `PGSCHEMA` | `public` | schema that holds the tables |
| `GEMINI_API_KEY` | (empty) | Gemini API key |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Gemini model name (see section 8) |

## 5. Run

```
python project.py
```

In the window:

1. Check the **Connection** fields (host, port, database, schema, user, password)
   and enter the **LLM API key** if it is not pre-filled.
2. Type or paste an SQL query into the **SQL query** box. `example_queries.sql` has
   eight ready-made TPC-H queries (one table, joins of 2, 3 and 5 tables, a lookup,
   a subquery and an outer join). Paste one query at a time.
3. Click **Compare**. The left tree is PostgreSQL's plan, the right tree is the AI plan.
   Use the scroll bars or the mouse wheel for large plans.
4. Read the **Differences** panel under the trees.

To try the interface without a database or an API key, click
**Load demo plans (no DB / LLM)**.

### Colours

| Colour | Meaning |
|---|---|
| Green | the same operation appears in both plans |
| Orange | the same piece of work, but something differs (operator, filter columns, join predicate or join sides) |
| Red | only in PostgreSQL's plan |
| Blue | only in the AI plan |
| Grey | not compared: helper nodes (Hash) and parallel-execution nodes (Gather, partial aggregate) |

### What is compared

Nodes of the two plans are paired by the work they do: a scan by its table, any other
node by its type and the set of tables below it. For each pair the tool reports:

* **scan_method**: Seq Scan vs Index Scan vs Index Only Scan vs Bitmap Heap Scan, or a different index.
* **scan_condition**: the columns used in the WHERE filters of that table differ.
* **join_algorithm**: Hash Join vs Merge Join vs Nested Loop.
* **join_sides**: the build (hash) side or the outer table of a Hash Join / Nested Loop is swapped.
* **join_condition**: the columns joined on differ.
* **join_type**: Inner vs Left, and so on.
* **aggregate_strategy**: HashAggregate vs GroupAggregate vs plain Aggregate.
* **join_order**: a join of a different group of tables appears in only one plan.
* **operator_missing / operator_extra**: a Sort, Limit, scan, ... that only one plan has.

Gather, Gather Merge and the "Partial" half of a parallel aggregate are not compared:
whether PostgreSQL goes parallel depends on table size and server settings, not on the
query. Constants and comparison operators inside filters are not compared, only which
columns are filtered.

### Difference rate

```
rate = (changed + missing + extra) / (matched + missing + extra)
```

Every compared node counts once. `matched` is a node found in both plans (it counts as
`changed` if anything about it differs), `missing` is only in PostgreSQL's plan, `extra`
is only in the AI plan. 0% means the plans are identical and 100% means they have
nothing in common. It measures nodes, not tree edit distance.

### Batch mode (many queries, no GUI)

To evaluate many queries at once, put them in a `.sql` file (queries end with `;`,
`--` comments are allowed) and run:

```
python project.py --batch example_queries.sql --out results.csv
```

For every query it gets PostgreSQL's plan, asks the AI for its plan and compares them
exactly as the GUI does. It writes one row per query to `results.csv` (open it in Excel):
the node counts, `matched / changed / missing / extra`, the difference rate, the kinds of
difference found and their descriptions, and an error message if that query failed (a
failed query does not stop the run). At the end it prints a summary: how many queries
succeeded, the mean / median / min / max difference rate, and how often each kind of
difference occurred.

| Option | Default | Meaning |
|---|---|---|
| `--batch FILE.sql` | (GUI) | the queries to run |
| `--out FILE.csv` | `results.csv` | where to write the per-query results |
| `--delay N` | `2` | seconds to wait after each AI call (helps with free-tier limits) |
| `--retries N` | `3` | retries (waiting 20 s, 40 s, ...) when the API reports a rate limit |
| `--host --port --dbname --user --password --schema` | environment / defaults | database connection |
| `--api-key KEY` | `GEMINI_API_KEY` | LLM API key |

AI answers are cached in `.llm_cache`, so re-running a batch does not call the AI again
for queries it has already answered (delete the folder to start fresh).

## 6. How it works (files)

| File | Role |
|---|---|
| `project.py` | entry point: starts the GUI, or runs a batch of queries with `--batch` |
| `interface.py` | Tkinter GUI: inputs, the two plan trees, the differences panel |
| `preprocessing.py` | reads the schema, runs `EXPLAIN (FORMAT JSON)`, builds the LLM prompt, calls the LLM, converts both plans to one tree format |
| `comparison.py` | matches nodes between the two plans, classifies the differences, computes the rate |
| `tpch_schema.sql` | CREATE TABLE statements for the TPC-H database |
| `example_queries.sql` | eight example queries |
| `test_comparison.py` | offline tests for `comparison.py` |

To run the tests (no database or key needed): `python -m unittest test_comparison -v`

## 7. Troubleshooting

| Message | What to do |
|---|---|
| `psycopg2 is not installed` | run `pip install -r requirements.txt` |
| `No module named '_tkinter'` / `tkinter` | reinstall Python from python.org with the "tcl/tk and IDLE" option ticked |
| `Could not connect to PostgreSQL ... Connection refused` | the server is not running or the port is wrong; start the PostgreSQL service |
| `Could not connect to PostgreSQL ... password authentication failed` | wrong user or password |
| `PostgreSQL rejected the query: relation "x" does not exist` | the SQL names a table that is not in the chosen schema; check the Database and Schema fields |
| `No API key` | set `GEMINI_API_KEY` or paste the key into the GUI |
| `LLM API returned HTTP 404` | the model name is not available to your key: set `GEMINI_MODEL` (see section 8) |
| `LLM API returned HTTP 400/403` | the API key is invalid or not allowed to use the model |
| `LLM API returned HTTP 429` | free-tier rate limit reached: wait a minute and click Compare again |
| `The LLM reply was not valid JSON` | click Compare again; if it repeats, delete the `.llm_cache` folder |

LLM replies are cached in a `.llm_cache` folder next to the code, so the same query
is not sent twice. Delete that folder to force a fresh answer from the AI.

## 8. The AI model, and using a different AI

The default model is `gemini-3.1-flash-lite`. Google's documentation (checked October 2026). This has the most quota available for free keys as of now.
recommends the 3.x Flash models for new projects and says the older `gemini-2.5-flash`
is only still served to projects that already used it. Model names change over time:
if you get HTTP 404, look up a current Flash model in Google AI Studio and set
`GEMINI_MODEL` (for example `set GEMINI_MODEL=gemini-3.5-flash-lite`). The same
model must be used for all experiments so that the results are comparable.

Only the function `ask_llm` in `preprocessing.py` talks to the provider. To use
another LLM, rewrite that function so it returns the model's text reply.
