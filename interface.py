"""
interface.py
============
Tkinter GUI (ships with Python on Windows).

Layout: connection + query inputs on top, the two plan trees side by side
(colour coded), and a text panel listing every difference at the bottom.

The slow work (database + LLM) runs in a background thread so the window
does not freeze; results come back through a queue.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk

import comparison
import preprocessing as pp

# Colours by PlanNode.status
STATUS_COLORS = {
    "unchanged": "#c8e6c9",   # green  - same in both plans
    "changed":   "#ffe0b2",   # orange - same piece of work, different operator
    "missing":   "#ffcdd2",   # red    - only in PostgreSQL's plan
    "extra":     "#bbdefb",   # blue   - only in the AI plan
}
LEGEND = [("unchanged", "Same in both"), ("changed", "Different operator"),
          ("missing", "Only in PostgreSQL"), ("extra", "Only in AI plan")]

BOX_W, BOX_H = 150, 52
X_GAP, Y_GAP = 170, 90
MARGIN = 20


# ---------------------------------------------------------------------------
# Tree layout and drawing
# ---------------------------------------------------------------------------
def layout_tree(root) -> dict:
    """
    Return {id(node): (column, depth)}. Leaves get consecutive columns and a
    parent sits at the average column of its children.
    """
    positions: dict = {}
    next_column = [0]

    def place(node, depth):
        if not node.children:
            column = float(next_column[0])
            next_column[0] += 1
        else:
            columns = [place(child, depth + 1) for child in node.children]
            column = sum(columns) / len(columns)
        positions[id(node)] = (column, depth)
        return column

    place(root, 0)
    return positions


def draw_tree(canvas: tk.Canvas, root) -> None:
    """Draw a PlanNode tree onto a canvas, root at the top."""
    canvas.delete("all")
    if root is None:
        return
    positions = layout_tree(root)

    def centre(node):
        column, depth = positions[id(node)]
        return MARGIN + BOX_W / 2 + column * X_GAP, MARGIN + BOX_H / 2 + depth * Y_GAP

    for node in root.walk():                       # lines first, boxes on top
        x1, y1 = centre(node)
        for child in node.children:
            x2, y2 = centre(child)
            canvas.create_line(x1, y1 + BOX_H / 2, x2, y2 - BOX_H / 2, width=1.5)

    for node in root.walk():
        x, y = centre(node)
        canvas.create_rectangle(x - BOX_W / 2, y - BOX_H / 2, x + BOX_W / 2, y + BOX_H / 2,
                                fill=STATUS_COLORS.get(node.status, "white"), outline="#444")
        canvas.create_text(x, y, text=node.label(), width=BOX_W - 8, justify="center",
                           font=("Segoe UI", 9))

    canvas.configure(scrollregion=canvas.bbox("all"))


# ---------------------------------------------------------------------------
# Application window
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Query Plan Comparison: PostgreSQL vs AI")
        root.geometry("1200x800")
        self.results = queue.Queue()
        self.fields: dict = {}
        self._build_widgets()

    # ---- widgets ----------------------------------------------------------
    def _build_widgets(self):
        cfg = pp.DBConfig.from_env()
        top = ttk.LabelFrame(self.root, text="Connection")
        top.pack(fill="x", padx=8, pady=4)
        entries = [("Host", "host", cfg.host), ("Port", "port", cfg.port),
                   ("Database", "dbname", cfg.dbname), ("Schema", "schema", cfg.schema),
                   ("User", "user", cfg.user), ("Password", "password", cfg.password),
                   ("LLM API key", "api_key", os.environ.get("GEMINI_API_KEY", ""))]
        for i, (label, name, default) in enumerate(entries):
            ttk.Label(top, text=label).grid(row=0, column=2 * i, padx=(6, 2), sticky="e")
            var = tk.StringVar(value=default)
            show = "*" if name in ("password", "api_key") else ""
            ttk.Entry(top, textvariable=var, width=11, show=show).grid(row=0, column=2 * i + 1)
            self.fields[name] = var

        qframe = ttk.LabelFrame(self.root, text="SQL query")
        qframe.pack(fill="x", padx=8, pady=4)
        self.query_text = tk.Text(qframe, height=6, font=("Consolas", 10))
        self.query_text.pack(fill="x", padx=4, pady=4)
        buttons = ttk.Frame(qframe)
        buttons.pack(fill="x", padx=4, pady=(0, 4))
        self.compare_button = ttk.Button(buttons, text="Compare", command=self.on_compare)
        self.compare_button.pack(side="left")
        ttk.Button(buttons, text="Load demo plans (no DB / LLM)",
                   command=self.on_demo).pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(buttons, textvariable=self.status_var).pack(side="left", padx=12)

        legend = ttk.Frame(self.root)
        legend.pack(fill="x", padx=8)
        for status, text in LEGEND:
            tk.Label(legend, text=f" {text} ", bg=STATUS_COLORS[status],
                     relief="solid", borderwidth=1).pack(side="left", padx=4, pady=2)

        panes = ttk.PanedWindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=4)
        self.pg_canvas = self._make_canvas(panes, "PostgreSQL plan")
        self.llm_canvas = self._make_canvas(panes, "AI plan")

        dframe = ttk.LabelFrame(self.root, text="Differences")
        dframe.pack(fill="x", padx=8, pady=4)
        self.rate_var = tk.StringVar(value="Difference rate: -")
        ttk.Label(dframe, textvariable=self.rate_var).pack(anchor="w", padx=4)
        self.diff_text = tk.Text(dframe, height=9, wrap="word", state="disabled")
        self.diff_text.pack(fill="x", padx=4, pady=4)

    def _make_canvas(self, panes, title):
        frame = ttk.LabelFrame(panes, text=title)
        panes.add(frame, weight=1)
        canvas = tk.Canvas(frame, bg="white")
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
        ybar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        xbar.pack(side="bottom", fill="x")
        ybar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        return canvas

    # ---- actions ----------------------------------------------------------
    def on_demo(self):
        pg, llm = pp.sample_plans()
        self.show(pg, llm, comparison.compare_plans(pg, llm))
        self.status_var.set("Showing built-in demo plans.")

    def on_compare(self):
        query = self.query_text.get("1.0", "end").strip()
        if not query:
            self.status_var.set("Please enter an SQL query.")
            return
        values = {name: var.get().strip() for name, var in self.fields.items()}
        self.compare_button.state(["disabled"])
        self.status_var.set("Fetching plans...")
        threading.Thread(target=self._worker, args=(query, values), daemon=True).start()
        self.root.after(100, self._poll)

    def _worker(self, query, values):
        """Runs in a background thread - must not touch any widget."""
        conn = None
        try:
            cfg = pp.DBConfig(host=values["host"], port=values["port"],
                              dbname=values["dbname"], user=values["user"],
                              password=values["password"], schema=values["schema"])
            conn = pp.connect_db(cfg)
            schema = pp.get_schema(conn, cfg.schema)
            pg_plan = pp.get_postgres_plan(conn, query, cfg.schema)
            llm_plan = pp.get_llm_plan(query, schema, values["api_key"] or None)
            result = comparison.compare_plans(pg_plan, llm_plan)
            self.results.put(("ok", pg_plan, llm_plan, result))
        except Exception as exc:                      # shown to the user, not swallowed
            self.results.put(("error", str(exc)))
        finally:
            if conn is not None:
                conn.close()

    def _poll(self):
        try:
            message = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll)
            return
        self.compare_button.state(["!disabled"])
        if message[0] == "error":
            self.status_var.set("Error: " + message[1])
        else:
            _, pg, llm, result = message
            self.show(pg, llm, result)
            self.status_var.set("Done.")

    def show(self, pg_plan, llm_plan, result):
        draw_tree(self.pg_canvas, pg_plan)
        draw_tree(self.llm_canvas, llm_plan)
        self.rate_var.set(
            f"Difference rate: {result.rate:.0%}   (changed {result.changed}, "
            f"only in PostgreSQL {result.missing}, only in AI {result.extra}, "
            f"matched {result.matched})")
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        if result.differences:
            for i, diff in enumerate(result.differences, 1):
                self.diff_text.insert("end", f"{i}. [{diff.kind}] {diff.text}\n")
        else:
            self.diff_text.insert("end", "The two plans are equivalent.\n")
        self.diff_text.configure(state="disabled")


def launch_gui():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
