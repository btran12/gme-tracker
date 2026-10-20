"""Regression test for widget.py layout.

Two invariants, checked in both views across a spread of positions and quotes:
  1. no rendered text may escape the rounded panel;
  2. in the full view the total gain is the headline - largest and centred - and the
     price is visibly smaller than it.

Run:  python test_widget_layout.py
Exits non-zero if any case breaks either invariant.
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
    "user position": dict(price=27.76, prev=27.22, shares=5320.0, cost=26.57),
    "big move": dict(price=27.88, prev=19.10, shares=500.0, cost=4.87),
    "thousands of shares": dict(price=1234.56, prev=999.99, shares=123456.7891, cost=0.0001),
    "no cost basis": dict(price=27.88, prev=27.22, shares=100.0, cost=0.0),
    "negative": dict(price=3.21, prev=9.87, shares=1.0, cost=45.00),
    "no holdings": dict(price=27.88, prev=27.22, shares=0.0, cost=0.0),
    "huge price": dict(price=99999.99, prev=88888.88, shares=999999.0, cost=12345.6789),
}

failures = []


def headline_problems(app, case, W):
    """What the full view headlines: total gain, else position value, else the price."""
    boxes = {
        app.canvas.itemcget(i, "text"): app.canvas.bbox(i)
        for i in app.canvas.find_all()
        if app.canvas.type(i) == "text"
    }
    heights = {text: box[3] - box[1] for text, box in boxes.items()}
    shares, cost = case["shares"], case["cost"]
    suffix = ""
    if shares > 0 and cost > 0:
        total = (case["price"] - cost) * shares
        hero = f"{'-' if total < 0 else '+'}${abs(total):,.0f}"
        suffix = f"{(case['price'] - cost) / cost * 100:+.1f}%"
    elif shares > 0:
        hero = f"${shares * case['price']:,.0f}"
    else:
        hero = f"{case['price']:,.2f}"
    if hero not in boxes:
        return [f"headline {hero!r} is not drawn"]

    problems = []
    if heights[hero] != max(heights.values()):
        problems.append(f"headline {hero!r} is not the largest text ({heights[hero]}px)")
    # The gain and its percentage are centred as one group, so measure them together.
    group = [boxes[hero]] + ([boxes[suffix]] if suffix in boxes else [])
    centre = (min(b[0] for b in group) + max(b[2] for b in group)) / 2
    if abs(centre - W / 2) > 8:
        problems.append(f"headline group is off-centre (x={centre:.0f}, panel centre {W / 2:.0f})")
    price = f"{case['price']:,.2f}"
    if shares > 0 and heights.get(price, 0) >= heights[hero]:
        problems.append(f"price {price!r} is not smaller than the headline")
    return problems


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
        headline = [] if compact else headline_problems(app, case, W)
        if headline:
            status = "FAIL"
        print(f"[{status}] {view:7s} {name}  panel={W}x{H}")
        for line in overflow:
            print(f"         overflow: {line}")
            failures.append((view, name, line))
        for line in headline:
            print(f"         headline: {line}")
            failures.append((view, name, line))

app.destroy()
print()
print("LAYOUT OK - no overflow, gain is the headline" if not failures else f"LAYOUT FAILURES: {len(failures)}")
sys.exit(1 if failures else 0)
