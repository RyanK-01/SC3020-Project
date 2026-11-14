from __future__ import annotations

import os
import queue
import re
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import comparison
import preprocessing as pp

# Box colour for each PlanNode.status
STATUS_COLORS = {
    "unchanged": "#c8e6c9",   # green  - same in both plans
    "changed":   "#ffe0b2",   # orange - same work, different operator
    "missing":   "#ffcdd2",   # red    - only in PostgreSQL's plan
    "extra":     "#bbdefb",   # blue   - only in the AI plan
    "ignored":   "#eeeeee",   # grey   - helper / parallel node, not compared
}
LEGEND = [
    ("unchanged", "Same in both"), 
    ("changed", "Different operator"),
    ("missing", "Only in PostgreSQL"), 
    ("extra", "Only in AI plan"),
    ("ignored", "Not compared (Hash / Gather)")
]

# Raw data the user can inspect: key in App.raw -> button / window title
RAW_VIEWS = {
    "explain": "View EXPLAIN output",
    "prompt":  "View LLM prompt",
    "reply":   "View LLM response",
}

# tree drawing sizes in pixels; box heights grow to fit the node's text
BOX_W = 200
X_GAP = 210          # horizontal distance between neighbouring leaf columns
ROW_GAP = 28         # vertical space between the bottom of a row and the next row
BOX_PAD = 6          # padding between a box's border and its text
MARGIN = 20
MAX_COND_CHARS = 90  # max num of chars displayed in condition text

HEADER_FONT = ("TkTextFont", 9, "bold")
DETAIL_FONT = ("TkTextFont", 8)

# Splits PlanNode.conditions ("Hash Cond: x; Filter: y") back into its parts
_COND_SPLIT = re.compile(r"; (?=(?:" + "|".join(re.escape(k) for k in pp._CONDITION_KEYS) + r"): )")
# PostgreSQL type casts such as ::date, ::text or ::timestamp without time zone
_CAST = re.compile(r"::(?:timestamp|time)(?: with(?:out)? time zone)?"
                   r"|::(?:character varying|double precision)"
                   r"|::[a-z_]+(?:\[\])?")

# ---------------------------------------------------------------------------
# Tree layout and drawing
# ---------------------------------------------------------------------------
def condition_lines(node) -> list:
    """ build the "key: condition" lines shown under a node's name """
    lines = []
    if node.conditions:
        for part in _COND_SPLIT.split(node.conditions):
            key, _, value = part.partition(": ")
            lines.append((key, value))
    if node.sort_key:
        lines.append(("Sort Key", node.sort_key))

    result = []
    for key, value in lines:
        value = _CAST.sub("", value).strip()        # remove psql's type casts
        if len(value) > MAX_COND_CHARS:
            value = value[:MAX_COND_CHARS - 3].rstrip() + "..."     # cut-off excess chars with "..."
        result.append(f"{key}: {value}")
    return result

def layout_tree(root) -> dict:
    """ computes a grid position for every node of a plan tree. """
    positions: dict = {}
    next_column = [0]

    def place(node, depth):
        """
        Place a node and its subtree.
        """
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
    """ draw a plan tree on a canvas, with the root at the top. """
    # clear any previous drawing
    canvas.delete("all")
    if root is None:
        return
    positions = layout_tree(root)
    text_w = BOX_W - 2 * BOX_PAD

    # 1. create each node's text (name on top, conditions below) and measure it
    texts, heights = {}, {}
    for node in root.walk():
        header = canvas.create_text(0, 0, text=node.label(), width=text_w,
                                    justify="center", anchor="n", font=HEADER_FONT)
        height = canvas.bbox(header)[3] - canvas.bbox(header)[1]
        detail = None
        lines = condition_lines(node)
        if lines:
            detail = canvas.create_text(0, 0, text="\n".join(lines), width=text_w,
                                        justify="left", anchor="nw", font=DETAIL_FONT)
            height += 2 + canvas.bbox(detail)[3] - canvas.bbox(detail)[1]
        texts[id(node)] = (header, detail)
        heights[id(node)] = height + 2 * BOX_PAD

    # 2. find tallest box in each level and set height to match it
    depth_count = max(depth for _, depth in positions.values()) + 1
    row_h = [0] * depth_count
    for node in root.walk():
        depth = positions[id(node)][1]
        row_h[depth] = max(row_h[depth], heights[id(node)])
    row_top = [MARGIN]
    for h in row_h[:-1]:
        row_top.append(row_top[-1] + h + ROW_GAP)

    def box(node):
        """ return the (left, top, right, bottom) canvas coordinates of a node's box """
        column, depth = positions[id(node)]
        left = MARGIN + column * X_GAP
        top = row_top[depth]
        return left, top, left + BOX_W, top + heights[id(node)]

    # 3. position the text, draw boxes and lines
    for node in root.walk():
        left, top, right, bottom = box(node)
        header, detail = texts[id(node)]
        canvas.coords(header, (left + right) / 2, top + BOX_PAD)
        if detail is not None:
            canvas.coords(detail, left + BOX_PAD, canvas.bbox(header)[3] + 2)
        rect = canvas.create_rectangle(left, top, right, bottom, outline="#444",
                                       fill=STATUS_COLORS.get(node.status, "white"))
        canvas.tag_lower(rect, header)
        for child in node.children:
            c_left, c_top, c_right, _ = box(child)
            line = canvas.create_line((left + right) / 2, bottom,
                                      (c_left + c_right) / 2, c_top, width=1.5)
            canvas.tag_lower(line)                 # lines sit behind every box

    x0, y0, x1, y1 = canvas.bbox("all")
    canvas.configure(scrollregion=(x0 - MARGIN, y0 - MARGIN, x1 + MARGIN, y1 + MARGIN))

# ---------------------------------------------------------------------------
# Application window
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk):
        """ set up the window and widgets """
        self.root = root
        root.title("Query Plan Comparison: PostgreSQL vs AI")
        root.geometry("1300x900")
        self.results = queue.Queue()
        self.fields: dict = {}
        self.raw: dict = {}             # to store the response from PSQL and LLM, and the prompt sent
        self.raw_buttons: dict = {}
        self._build_widgets()

    def _build_widgets(self):
        """ creates the widgets (textbox, canvas, buttons etc.) in UI """
        cfg = pp.DBConfig.from_env()
        top = ttk.LabelFrame(self.root, text="Connection")
        top.pack(fill="x", padx=8, pady=4)

        # (label, field name, default value) for each connection input
        entries = [
            ("Host", "host", cfg.host), 
            ("Port", "port", cfg.port),
            ("Database", "dbname", cfg.dbname), 
            ("Schema", "schema", cfg.schema),
            ("User", "user", cfg.user), 
            ("Password", "password", cfg.password),
            ("LLM API key", "api_key", 
            os.environ.get("GEMINI_API_KEY", ""))
        ]
        for i, (label, name, default) in enumerate(entries):
            ttk.Label(top, text=label).grid(row=0, column=2 * i, padx=(6, 2), sticky="e")
            var = tk.StringVar(value=default)
            show = "*" if name in ("password", "api_key") else ""   # hide secrets
            ttk.Entry(top, textvariable=var, width=11, show=show).grid(row=0, column=2 * i + 1)
            self.fields[name] = var

        # query box and the plan area share a vertical PanedWindow so both can be adjustable without being fully dragged shut.
        vpanes = tk.PanedWindow(self.root, orient="vertical", sashwidth=6,
                                sashrelief="raised", showhandle=False, borderwidth=0)
        vpanes.pack(fill="both", expand=True, padx=8, pady=4)

        query_frame = ttk.LabelFrame(vpanes, text="SQL Query (drag the bar below to resize)")
        buttons = ttk.Frame(query_frame)
        buttons.pack(side="bottom", fill="x", padx=4, pady=(0, 4))   # stays visible when shrunk

        query_box = ttk.Frame(query_frame)
        query_box.pack(fill="both", expand=True, padx=4, pady=4)

        self.query_text = tk.Text(query_box, height=6, font="TkFixedFont", undo=True)
        query_scrollbar = ttk.Scrollbar(query_box, orient="vertical", command=self.query_text.yview)
        query_scrollbar.pack(side="right", fill="y")
        self.query_text.configure(yscrollcommand=query_scrollbar.set)
        self.query_text.pack(fill="both", expand=True)
        
        self.compare_button = ttk.Button(buttons, text="Compare", command=self.on_compare)
        self.compare_button.pack(side="left")
        ttk.Button(buttons, text="Load demo plans (no DB / LLM)", command=self.on_demo).pack(side="left", padx=6)
        
        # checkbox to add stats to prompt to AI
        self.stats_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(buttons, text="Send statistics to AI", variable=self.stats_var).pack(side="left", padx=6)
        ttk.Separator(buttons, orient="vertical").pack(side="left", fill="y", padx=6)
        
        # buttons to view debugging / additional info
        for key, title in RAW_VIEWS.items():
            button = ttk.Button(buttons, text=title, command=lambda k=key: self.show_raw(k))
            button.pack(side="left", padx=2)
            button.state(["disabled"])                # enabled once there is data
            self.raw_buttons[key] = button
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(buttons, textvariable=self.status_var).pack(side="left", padx=12)

        plan_area = ttk.Frame(vpanes)
        legend = ttk.Frame(plan_area)
        legend.pack(fill="x", pady=(4, 0))
        for status, text in LEGEND:
            tk.Label(legend, text=f" {text} ", bg=STATUS_COLORS[status], relief="solid", borderwidth=1).pack(side="left", padx=4, pady=2)

        panes = ttk.PanedWindow(plan_area, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(4, 0))
        self.pg_canvas = self._make_canvas(panes, "PostgreSQL plan")
        self.llm_canvas = self._make_canvas(panes, "AI plan")

        # sets minimum heights on the vertical panes to prevent full minimization
        self.root.update_idletasks()
        line = tkfont.nametofont("TkFixedFont").metrics("linespace")
        query_min = buttons.winfo_reqheight() + 5 * line + 40   # + frame label/padding
        plan_min = legend.winfo_reqheight() + 180
        vpanes.add(query_frame, minsize=query_min, stretch="never")
        vpanes.add(plan_area, minsize=plan_min, stretch="always")
        self._vpanes_min = query_min + plan_min + 6             # + sash width

        dframe = ttk.LabelFrame(self.root, text="Differences")
        dframe.pack(fill="x", padx=8, pady=4)
        self.rate_var = tk.StringVar(value="Difference rate: -")
        ttk.Label(dframe, textvariable=self.rate_var).pack(anchor="w", padx=4)
        self.diff_text = tk.Text(dframe, height=7, wrap="word", state="disabled")
        self.diff_text.pack(fill="x", padx=4, pady=4)

        # stop the window itself being shrunk so far that a pane gets squashed.
        self.root.update_idletasks()
        others = self.root.winfo_reqheight() - vpanes.winfo_reqheight()
        self.root.minsize(600, others + self._vpanes_min)

    def _make_canvas(self, panes, title):
        """create a scrollable canvas a plan tree"""
        frame = ttk.LabelFrame(panes, text=title)
        panes.add(frame, weight=1)
        canvas = tk.Canvas(frame, bg="white")
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
        ybar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        xbar.pack(side="bottom", fill="x")
        ybar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)

        # add mousewheel scrolling
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1 * (e.delta // 120 or (1 if e.delta > 0 else -1)), "units"))
        canvas.bind("<Shift-MouseWheel>", lambda e: canvas.xview_scroll(-1 * (e.delta // 120 or (1 if e.delta > 0 else -1)), "units"))
        
        return canvas

    def on_demo(self):
        """compare and display the built-in sample plan"""
        pg, llm = pp.sample_plans()
        self._set_raw({})                             # demo plans have no raw DB / LLM data
        self.show(pg, llm, comparison.compare_plans(pg, llm))
        self.status_var.set("Showing built-in demo plans.")

    def on_compare(self):
        """ reads query and configuration options then fetches and compares plans in bg-thread"""
        # clear previous results if any
        self._clear_results()

        # read query
        query = self.query_text.get("1.0", "end").strip()
        if not query:
            self.status_var.set("Please enter an SQL query.")
            return

        # read DB connection settings
        values = {name: var.get().strip() for name, var in self.fields.items()}
        values["use_stats"] = self.stats_var.get()
        self.compare_button.state(["disabled"])
        self.status_var.set("Fetching plans...")

        # poll
        threading.Thread(target=self._worker, args=(query, values), daemon=True).start()
        self.root.after(100, self._poll)

    def _clear_results(self):
        """helper function to remove the previous plans and differences."""
        draw_tree(self.pg_canvas, None)
        draw_tree(self.llm_canvas, None)
        self.pg_canvas.configure(scrollregion=(0, 0, 0, 0))
        self.llm_canvas.configure(scrollregion=(0, 0, 0, 0))
        self.rate_var.set("Difference rate: -")
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        self.diff_text.configure(state="disabled")

    def _worker(self, query, values):
        """helper function to fetch both plans and compare them (runs in a background thread)."""
        conn = None
        # Raw intermediate data, kept even if a later step fails
        raw = {"explain": None, "prompt": None, "reply": None}
        try:
            cfg = pp.DBConfig(
                host=values["host"], 
                port=values["port"],
                dbname=values["dbname"], 
                user=values["user"],
                password=values["password"], 
                schema=values["schema"]
            )
            conn = pp.connect_db(cfg)
            schema = pp.get_schema(conn, cfg.schema)
            pg_plan, raw["explain"] = pp.get_postgres_plan(conn, query, cfg.schema)
            stats = pp.get_stats(conn, cfg.schema, query) if values["use_stats"] else None
            
            llm_plan, raw["prompt"], raw["reply"] = pp.get_llm_plan(query, schema, values["api_key"] or None, stats)
            result = comparison.compare_plans(pg_plan, llm_plan)
            self.results.put(("ok", raw, pg_plan, llm_plan, result))

        except Exception as exc:                      # shown to the user, not swallowed
            if isinstance(exc, pp.LLMError):          # keep the prompt / reply for inspection
                raw["prompt"], raw["reply"] = exc.prompt, exc.reply
            self.results.put(("error", raw, str(exc)))
        
        finally:
            if conn is not None:
                conn.close()

    def _poll(self):
        """check every 100 ms whether the background thread has finished"""
        try:
            message = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll)
            return
        self.compare_button.state(["!disabled"])
        if message[0] == "error":
            _, raw, error = message
            self._set_raw(raw)
            self.status_var.set("Error: " + error)
        else:
            _, raw, pg, llm, result = message
            self._set_raw(raw)
            self.show(pg, llm, result)
            self.status_var.set("Done.")

    # ---- raw data viewers -------------------------------------------------
    def _set_raw(self, raw):
        """Store the raw data and enable only the buttons that have something to show."""
        self.raw = dict(raw)
        for key, button in self.raw_buttons.items():
            button.state(["!disabled"] if self.raw.get(key) else ["disabled"])

    def show_raw(self, key):
        title = RAW_VIEWS[key]
        text = self.raw.get(key)
        if not text:
            self.status_var.set(f"No {title.lower()} available yet.")
            return
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("800x600")
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=4, pady=4)

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

        ttk.Button(bar, text="Copy to clipboard", command=copy).pack(side="left")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        box = tk.Text(frame, wrap="none", font="TkFixedFont")
        ybar = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=box.xview)
        box.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        xbar.pack(side="bottom", fill="x")
        ybar.pack(side="right", fill="y")
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")

    def show(self, pg_plan, llm_plan, result):
        """Display two compared plans and their differences."""
        draw_tree(self.pg_canvas, pg_plan)
        draw_tree(self.llm_canvas, llm_plan)
        self.rate_var.set(
            f"Difference rate: {result.rate:.0%} \n"
            f" - Changed: {result.changed} \n" 
            f" - Only in PostgreSQL: {result.missing} \n" 
            f" - Only in AI: {result.extra} \n"
            f" - Matched: {result.matched}")
        
        # The text box is read-only; enable it briefly to replace its contents
        self.diff_text.configure(state="normal")
        self.diff_text.delete("1.0", "end")
        if result.differences:
            for i, diff in enumerate(result.differences, 1):
                self.diff_text.insert("end", f"{i}. [{diff.kind}] {diff.text}\n")
        else:
            self.diff_text.insert("end", "The two plans are equivalent.\n")
        self.diff_text.configure(state="disabled")

def launch_gui():
    """Create the main window and run the Tkinter event loop."""
    root = tk.Tk()
    App(root)
    root.mainloop()