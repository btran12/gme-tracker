"""Regression test for widget.py layout: no text may escape the rounded panel.

Run:  python test_widget_layout.py
Exits non-zero if any rendered text item overflows the panel in either view.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

TARGET = Path(__file__).with_name("widget.py")
spec = importlib.util.spec_from_file_location("gmewidget", TARGET)
w = importlib.util.module_from_spec(spec)
sys.modules["gmewidget"] = w
spec.loader.exec_module(w)

# Never touch the user's real config: point the module at a scratch file.
w.CONFIG_PATH = Path(tempfile.mkdtemp(prefix="gmewidget-test-")) / "config.json"
w.set_dpi_aware()

CASES = {
    "typical": dict(price=27.88, prev=27.22, shares=500.0, cost=23.4135),
    "big move": dict(price=27.88, prev=19.10, shares=500.0, cost=4.87),
    "thousands of shares": dict(price=1234.56, prev=999.99, shares=123456.7891, cost=0.0001),
    "no cost basis": dict(price=27.88, prev=27.22, shares=100.0, cost=0.0),
    "negative": dict(price=3.21, prev=9.87, shares=1.0, cost=45.00),
    "no holdings": dict(price=27.88, prev=27.22, shares=0.0, cost=0.0),
    "huge price": dict(price=99999.99, prev=88888.88, shares=999999.0, cost=12345.6789),
}

failures = []
app = w.GmeWidget()
app.withdraw()

for name, case in CASES.items():
    chg = case["price"] - case["prev"]
    app.quote = {
        "price": case["price"], "prev_close": case["prev"], "change": chg,
        "change_pct": chg / case["prev"] * 100, "as_of": 1791993816, "tz_offset": -14400,
        "pre_open": 0, "post_close": 0, "rth_open": 0, "rth_close": 0,
    }
    app.error = None
    app.cfg["shares"] = case["shares"]
    app.cfg["cost_basis"] = case["cost"]
    for compact in (False, True):
        app.cfg["compact"] = compact
        app._apply_view()
        app.update_idletasks()
        app.render()
        app.update_idletasks()
        W, H = app._size
        view = "compact" if compact else "full"
        overflow = []
        for item in app.canvas.find_all():
            if app.canvas.type(item) != "text":
                continue  # panel polygon and divider are drawn inside by construction
            x1, y1, x2, y2 = app.canvas.bbox(item)
            if x1 < 2 or y1 < 2 or x2 > W - 2 or y2 > H - 2:
                txt = app.canvas.itemcget(item, "text") or "<empty>"
                overflow.append(f"{txt!r} bbox=({x1},{y1},{x2},{y2}) limit=({W},{H})")
        status = "OK " if not overflow else "FAIL"
        print(f"[{status}] {view:7s} {name}  panel={W}x{H}")
        for line in overflow:
            print(f"         overflow: {line}")
            failures.append((view, name, line))

app.destroy()
print()
print("LAYOUT OK - no overflow" if not failures else f"LAYOUT FAILURES: {len(failures)}")
sys.exit(1 if failures else 0)
