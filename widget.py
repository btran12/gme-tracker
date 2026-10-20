"""GME mini widget - a small always-on-top panel that docks above the Windows taskbar.

Headlines your unrealised total gain, with the live GME quote as the smaller line
beneath it. Quotes come from Yahoo Finance's public chart endpoint, so no API key is
needed.

Run:  pythonw widget.py     (or: python widget.py --check  to test the quote feed)
"""

from __future__ import annotations

import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "widget-config.json"
TRACKER_PATH = HERE / "index.html"
STARTUP_DIR = (
    Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
)
AUTOSTART_FILE = STARTUP_DIR / "GME Widget.bat"

QUOTES_URL = "https://query1.finance.yahoo.com/v8/finance/chart/GME"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

POLL_OPEN = 30_000
POLL_EXTENDED = 120_000
POLL_CLOSED = 600_000
CACHE_WRITE_INTERVAL = 300.0

BG = "#15181d"
FG = "#e8eaed"
MUTED = "#9aa4b2"
UP = "#3fb950"
DOWN = "#f85149"
BORDER = "#2d333b"
CHROME = "#010203"  # magic colour made click-through/transparent

FULL_SIZE = (330, 132)
FULL_PAD = 14
# Largest first: the hero gain shrinks until it fits rather than clipping.
HERO_SIZES = (30, 26, 22, 19, 16, 13)

COMPACT_SIZE = (168, 34)
COMPACT_PAD = 12
COMPACT_MAX_WIDTH = 400
COMPACT_GAIN_SIZES = (13, 11, 10, 9)

DEFAULTS = {
    "shares": 0.0,
    "cost_basis": 0.0,
    "compact": False,
    "always_on_top": True,
    "autostart": False,
    "position": None,
    "last_quote": None,
}


# --------------------------------------------------------------------------- config


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- quotes


def fetch_quote() -> dict:
    """Return the current GME quote. Raises on any network or parse failure."""
    params = urllib.parse.urlencode({"interval": "1d", "range": "1d"})
    request = urllib.request.Request(
        f"{QUOTES_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)

    meta = payload["chart"]["result"][0]["meta"]
    price = float(meta["regularMarketPrice"])
    prev_close = float(meta["chartPreviousClose"])
    period = meta["currentTradingPeriod"]
    # chartPreviousClose is only the true previous session close when range=1d.
    return {
        "price": price,
        "prev_close": prev_close,
        "change": price - prev_close,
        "change_pct": (price - prev_close) / prev_close * 100 if prev_close else 0.0,
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "as_of": int(meta["regularMarketTime"]),
        "tz_offset": int(meta.get("gmtoffset", 0)),
        "pre_open": int(period["pre"]["start"]),
        "post_close": int(period["post"]["end"]),
        "rth_open": int(period["regular"]["start"]),
        "rth_close": int(period["regular"]["end"]),
        "fetched_at": time.time(),
    }


def poll_delay(quote: dict | None) -> int:
    """Poll fast during regular hours, slower pre/post, slowest when shut."""
    if not quote:
        return POLL_OPEN
    now = time.time()
    if quote["rth_open"] <= now < quote["rth_close"]:
        return POLL_OPEN
    if quote["pre_open"] <= now < quote["post_close"]:
        return POLL_EXTENDED
    return POLL_CLOSED


def market_state(quote: dict | None) -> str:
    if not quote:
        return "CONNECTING"
    now = time.time()
    if quote["rth_open"] <= now < quote["rth_close"]:
        return "OPEN"
    if quote["pre_open"] <= now < quote["post_close"]:
        return "PRE/POST"
    return "CLOSED"


def exchange_time(quote: dict) -> str:
    tz = timezone(timedelta(seconds=quote["tz_offset"]))
    return datetime.fromtimestamp(quote["as_of"], tz).strftime("%H:%M")


# --------------------------------------------------------------------------- win32


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def set_dpi_aware() -> None:
    """Make geometry maths use physical pixels so the panel is crisp and well placed."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def work_area() -> RECT:
    rect = RECT()
    ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
    return rect


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


ctypes.windll.user32.MonitorFromPoint.argtypes = [POINT, ctypes.c_uint]
ctypes.windll.user32.MonitorFromPoint.restype = ctypes.c_void_p


def work_area_at(x: int, y: int) -> RECT:
    """Work area of the monitor containing (x, y), so drags onto other monitors stick."""
    monitor = ctypes.windll.user32.MonitorFromPoint(POINT(x, y), 2)  # DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if monitor and ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return info.rcWork
    return work_area()


def default_position(width: int, height: int) -> tuple[int, int]:
    """Bottom-right corner of the work area, i.e. just above the taskbar."""
    area = work_area()
    margin = 12
    return area.right - width - margin, area.bottom - height - margin


def set_autostart(enabled: bool) -> bool:
    """Create or remove a Startup-folder launcher. Only called from the menu."""
    try:
        if not enabled:
            AUTOSTART_FILE.unlink(missing_ok=True)
            return False
        interpreter = Path(sys.executable).with_name("pythonw.exe")
        if not interpreter.exists():
            interpreter = Path(sys.executable)
        AUTOSTART_FILE.write_text(
            f'@echo off\r\nstart "" "{interpreter}" "{Path(__file__).resolve()}"\r\n',
            encoding="utf-8",
        )
        return True
    except OSError:
        return not enabled


# --------------------------------------------------------------------------- helpers


def fmt_shares(value: float) -> str:
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_money(value: float, decimals: int = 0) -> str:
    return f"${value:,.{decimals}f}"


def fmt_signed_money(value: float, decimals: int = 0) -> str:
    """Always signed, so a gain reads as +$7,714 and a loss as -$7,714."""
    return f"{'-' if value < 0 else '+'}${abs(value):,.{decimals}f}"


def rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


# --------------------------------------------------------------------------- widget


class GmeWidget(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        self.quote: dict | None = self.cfg.get("last_quote")
        self.error: str | None = None
        self._results: queue.Queue = queue.Queue()
        self._busy = False
        self._fetch_job: str | None = None
        self._last_cache_write = 0.0
        self._drag_offset: tuple[int, int] | None = None

        self.overrideredirect(True)
        self.title("GME Widget")

        self._fonts: dict[tuple, tkfont.Font] = {}  # measure cache, keyed by font tuple
        self._size = FULL_SIZE

        self.canvas = tk.Canvas(
            self, highlightthickness=0, bd=0, bg=BG, width=1, height=1
        )
        self.canvas.pack(fill="both", expand=True)
        self._enable_transparency()

        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Double-Button-1>", lambda _e: self.toggle_compact())
        self.canvas.bind("<Button-3>", self._show_menu)

        self._apply_view()
        self._place()
        self.render()

        self.after(250, self._drain_results)
        self._kick()
        self.protocol("WM_DELETE_WINDOW", self.quit_widget)

    # -- window chrome ------------------------------------------------------

    def _enable_transparency(self) -> None:
        """Rounded corners via a magic colour; fall back to a square panel."""
        try:
            self.config(bg=CHROME)
            self.attributes("-transparentcolor", CHROME)
            self.canvas.config(bg=CHROME)
        except tk.TclError:
            self.config(bg=BG)
            self.canvas.config(bg=BG)

    def _measure(self, text: str, spec: tuple) -> int:
        """Pixel width of `text` in the font described by `spec`, e.g. ("Segoe UI", 9, "bold")."""
        font = self._fonts.get(spec)
        if font is None:
            weight = spec[2] if len(spec) > 2 else "normal"
            font = tkfont.Font(self, family=spec[0], size=spec[1], weight=weight)
            self._fonts[spec] = font
        return font.measure(text)

    def _fit_size(self, text: str, max_width: int, sizes: tuple[int, ...], weight: str = "bold") -> int:
        """Largest of `sizes` that still fits, so text shrinks instead of clipping."""
        for size in sizes:
            if self._measure(text, ("Segoe UI", size, weight)) <= max_width:
                return size
        return sizes[-1]

    def _view_size(self) -> tuple[int, int]:
        if not self.cfg["compact"]:
            return FULL_SIZE
        return self._compact_width(), COMPACT_SIZE[1]

    def _compact_width(self) -> int:
        """Size the compact panel to its own text so a large gain can never clip."""
        if not self.quote:
            return COMPACT_SIZE[0]
        needed = COMPACT_PAD * 2 + sum(
            gap + self._measure(text, font)
            for gap, text, font, _ in self._compact_segments()
        )
        # Round up to 8px so a drifting price doesn't resize the window every tick.
        return max(COMPACT_SIZE[0], min(COMPACT_MAX_WIDTH, ((needed + 7) // 8) * 8))

    def _apply_view(self) -> None:
        self._size = self._view_size()
        width, height = self._size
        self.canvas.config(width=width, height=height)
        self.geometry(f"{width}x{height}")
        try:
            self.attributes("-topmost", bool(self.cfg["always_on_top"]))
        except tk.TclError:
            pass
        self.update_idletasks()

    def _sync_size(self) -> None:
        """Resize in place when the compact text changes length."""
        if self._view_size() != self._size:
            self._apply_view()
            self._clamp()

    def _clamp(self) -> None:
        width, height = self._size
        area = work_area_at(self.winfo_x() + width // 2, self.winfo_y() + height // 2)
        x = max(area.left, min(self.winfo_x(), area.right - width))
        y = max(area.top, min(self.winfo_y(), area.bottom - height))
        self.geometry(f"+{x}+{y}")

    def _place(self) -> None:
        width, height = self._size
        saved = self.cfg.get("position")
        if saved and len(saved) == 2:
            x, y = saved
        else:
            x, y = default_position(width, height)
        area = work_area_at(x + width // 2, y + height // 2)
        x = max(area.left, min(x, area.right - width))
        y = max(area.top, min(y, area.bottom - height))
        self.geometry(f"+{x}+{y}")

    # -- data ---------------------------------------------------------------

    def _kick(self) -> None:
        self._fetch_job = None
        if not self._busy:
            self._busy = True
            threading.Thread(target=self._worker, daemon=True).start()
        self._schedule(poll_delay(self.quote))

    def _worker(self) -> None:
        try:
            self._results.put(("ok", fetch_quote()))
        except Exception as exc:  # network, DNS, TLS, rate limit, schema drift
            self._results.put(("err", f"{type(exc).__name__}: {exc}"))

    def _schedule(self, delay_ms: int) -> None:
        if self._fetch_job:
            self.after_cancel(self._fetch_job)
        self._fetch_job = self.after(delay_ms, self._kick)

    def _drain_results(self) -> None:
        while True:
            try:
                kind, payload = self._results.get_nowait()
            except queue.Empty:
                break
            self._busy = False
            if kind == "ok":
                self.quote = payload
                self.error = None
                self._cache_quote()
            else:
                self.error = payload
            self.render()
        self.after(250, self._drain_results)

    def _cache_quote(self) -> None:
        """Persist the last good quote so a cold start still shows a number."""
        now = time.time()
        if now - self._last_cache_write < CACHE_WRITE_INTERVAL:
            return
        self._last_cache_write = now
        self.cfg["last_quote"] = self.quote
        save_config(self.cfg)

    # -- rendering ----------------------------------------------------------

    def render(self) -> None:
        c = self.canvas
        self._sync_size()
        c.delete("all")
        width, height = self._size
        rounded_rect(
            c, 1, 1, width - 1, height - 1, 10, fill=BG, outline=BORDER, width=1
        )
        if self.cfg["compact"]:
            self._render_compact(width, height)
        else:
            self._render_full(width, height)

    def _status_text(self) -> tuple[str, str]:
        if self.error and not self.quote:
            return "OFFLINE", DOWN
        if self.error:
            return f"STALE {exchange_time(self.quote)} ET", DOWN
        if not self.quote:
            return "CONNECTING", MUTED
        state = market_state(self.quote)
        return f"{state} {exchange_time(self.quote)} ET", (UP if state == "OPEN" else MUTED)

    def _total_gain(self) -> float | None:
        """Unrealised P/L, or None until both a share count and a cost basis are set."""
        if not self.quote:
            return None
        shares = float(self.cfg.get("shares") or 0.0)
        cost = float(self.cfg.get("cost_basis") or 0.0)
        if shares <= 0 or cost <= 0:
            return None
        return (self.quote["price"] - cost) * shares

    def _compact_segments(self) -> list[tuple[int, str, tuple, str]]:
        """(gap, text, font, colour) laid out left to right, total gain first.

        Both the measure and the draw pass walk this one list, so the strip can never
        be sized against text it does not then render.
        """
        if not self.quote:
            return [(0, "GME  --", ("Segoe UI", 10, "bold"), MUTED)]
        q = self.quote
        arrow = "\u25b2" if q["change"] >= 0 else "\u25bc"
        day_colour = UP if q["change"] >= 0 else DOWN
        pct = f"{arrow}{abs(q['change_pct']):.2f}%"
        head = [(0, "GME", ("Segoe UI", 8), MUTED)]
        total = self._total_gain()
        if total is None:
            return head + [
                (10, f"{q['price']:,.2f}", ("Segoe UI", 12, "bold"), FG),
                (8, pct, ("Segoe UI", 9, "bold"), day_colour),
            ]
        tail = [
            (10, f"{q['price']:,.2f}", ("Segoe UI", 9, "bold"), MUTED),
            (6, pct, ("Segoe UI", 8), day_colour),
        ]
        used = COMPACT_PAD * 2 + sum(
            gap + self._measure(text, font) for gap, text, font, _ in head + tail
        )
        text = fmt_signed_money(total)
        size = self._fit_size(text, COMPACT_MAX_WIDTH - used, COMPACT_GAIN_SIZES)
        gain = [(10, text, ("Segoe UI", size, "bold"), UP if total >= 0 else DOWN)]
        return head + gain + tail

    def _render_compact(self, width: int, height: int) -> None:
        c = self.canvas
        mid = height / 2
        x = COMPACT_PAD
        for gap, text, font, colour in self._compact_segments():
            x += gap
            c.create_text(x, mid, anchor="w", text=text, font=font, fill=colour)
            x += self._measure(text, font)

    def _render_full(self, width: int, height: int) -> None:
        """Headline the total gain, with the live quote demoted to the line beneath it."""
        c = self.canvas
        pad = FULL_PAD
        centre = width / 2
        inner = width - 2 * pad
        caption_y, hero_y, quote_y = 33, 60, 92
        divider_y, row_y = height - 27, height - 11

        status, status_colour = self._status_text()
        c.create_text(pad, 15, anchor="w", text="GME \u00b7 NYSE", font=("Segoe UI", 8), fill=MUTED)
        c.create_text(width - pad, 15, anchor="e", text=status, font=("Segoe UI", 8), fill=status_colour)

        if not self.quote:
            c.create_text(centre, hero_y, text="--", font=("Segoe UI", 26, "bold"), fill=MUTED)
            c.create_text(
                pad, row_y, anchor="w",
                text=self.error or "Waiting for a quote\u2026",
                font=("Segoe UI", 8), fill=DOWN if self.error else MUTED,
            )
            return

        q = self.quote
        shares = float(self.cfg.get("shares") or 0.0)
        cost = float(self.cfg.get("cost_basis") or 0.0)
        arrow = "\u25b2" if q["change"] >= 0 else "\u25bc"
        day_colour = UP if q["change"] >= 0 else DOWN
        total = self._total_gain()

        if total is not None:
            total_pct = (q["price"] - cost) / cost * 100
            caption = f"TOTAL GAIN \u00b7 avg cost {fmt_money(cost, 2)}"
            hero = fmt_signed_money(total)
            hero_pct = f"{total_pct:+.1f}%"
            hero_colour = UP if total >= 0 else DOWN
            detail = f"{fmt_shares(shares)} sh \u00b7 value {fmt_money(shares * q['price'])}"
        elif shares > 0:
            caption = "POSITION VALUE"
            hero = fmt_money(shares * q["price"])
            hero_pct = None
            hero_colour = FG
            detail = f"{fmt_shares(shares)} sh \u00b7 set avg cost for total gain"
        else:
            caption = "LAST PRICE"
            hero = f"{q['price']:,.2f}"
            hero_pct = None
            hero_colour = FG
            detail = "Right-click \u2192 Edit holdings to track your position"

        c.create_text(centre, caption_y, text=caption, font=("Segoe UI", 8), fill=MUTED)

        pct_font = ("Segoe UI", 11, "bold")
        pct_gap = 8 if hero_pct else 0
        pct_width = self._measure(hero_pct, pct_font) + pct_gap if hero_pct else 0
        hero_font = ("Segoe UI", self._fit_size(hero, inner - pct_width, HERO_SIZES), "bold")
        hero_width = self._measure(hero, hero_font)
        hero_x = centre - (hero_width + pct_width) / 2
        c.create_text(hero_x, hero_y, anchor="w", text=hero, font=hero_font, fill=hero_colour)
        if hero_pct:
            c.create_text(
                hero_x + hero_width + pct_gap, hero_y, anchor="w",
                text=hero_pct, font=pct_font, fill=hero_colour,
            )

        # The quote line: price is dropped when it is already the hero above.
        change = f"{arrow} {q['change']:+,.2f} ({q['change_pct']:+.2f}%)"
        change_font = ("Segoe UI", 9, "bold")
        price = "" if shares == 0 else f"{q['price']:,.2f}"
        price_font = ("Segoe UI", 10)
        price_width = self._measure(price, price_font) + 8 if price else 0
        quote_x = centre - (price_width + self._measure(change, change_font)) / 2
        if price:
            c.create_text(quote_x, quote_y, anchor="w", text=price, font=price_font, fill=MUTED)
        c.create_text(
            quote_x + price_width, quote_y, anchor="w",
            text=change, font=change_font, fill=day_colour,
        )

        c.create_line(pad, divider_y, width - pad, divider_y, fill=BORDER)

        if shares == 0:
            c.create_text(pad, row_y, anchor="w", text=detail, font=("Segoe UI", 8), fill=MUTED)
            return

        day_gain = shares * q["change"]  # today's P/L on the whole position
        right = f"Today {fmt_signed_money(day_gain)}"
        size = 9
        while size > 7 and (
            self._measure(detail, ("Segoe UI", size))
            + self._measure(right, ("Segoe UI", size, "bold"))
            + 14
            > inner
        ):
            size -= 1
        c.create_text(pad, row_y, anchor="w", text=detail, font=("Segoe UI", size), fill=MUTED)
        c.create_text(
            width - pad, row_y, anchor="e", text=right,
            font=("Segoe UI", size, "bold"), fill=UP if day_gain >= 0 else DOWN,
        )

    # -- interaction --------------------------------------------------------

    def _drag_start(self, event) -> None:
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event) -> None:
        if not self._drag_offset:
            return
        dx, dy = self._drag_offset
        self.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _drag_end(self, _event) -> None:
        if self._drag_offset:
            self.cfg["position"] = [self.winfo_x(), self.winfo_y()]
            save_config(self.cfg)
        self._drag_offset = None

    def _show_menu(self, event) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Refresh now", command=self.refresh_now)
        menu.add_command(
            label="Compact view" if not self.cfg["compact"] else "Full view",
            command=self.toggle_compact,
        )
        menu.add_command(label="Edit holdings\u2026", command=self.edit_holdings)
        menu.add_checkbutton(
            label="Always on top",
            command=self.toggle_topmost,
            onvalue=True,
            offvalue=False,
            variable=tk.BooleanVar(value=bool(self.cfg["always_on_top"])),
        )
        menu.add_command(label="Reset position", command=self.reset_position)
        menu.add_separator()
        if TRACKER_PATH.exists():
            menu.add_command(label="Open full tracker", command=self.open_tracker)
        menu.add_command(
            label="Stop starting with Windows"
            if self.cfg.get("autostart")
            else "Start with Windows",
            command=self.toggle_autostart,
        )
        menu.add_separator()
        if self.error:
            menu.add_command(label=f"Last error: {self.error[:60]}", state="disabled")
        menu.add_command(label="Quit", command=self.quit_widget)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def refresh_now(self) -> None:
        self._kick()
        self.render()

    def toggle_compact(self) -> None:
        self.cfg["compact"] = not self.cfg["compact"]
        save_config(self.cfg)
        self._apply_view()
        self._place()
        self.render()

    def toggle_topmost(self) -> None:
        self.cfg["always_on_top"] = not self.cfg["always_on_top"]
        save_config(self.cfg)
        try:
            self.attributes("-topmost", bool(self.cfg["always_on_top"]))
        except tk.TclError:
            pass

    def reset_position(self) -> None:
        self.cfg["position"] = None
        save_config(self.cfg)
        self._place()

    def toggle_autostart(self) -> None:
        self.cfg["autostart"] = set_autostart(not self.cfg.get("autostart"))
        save_config(self.cfg)

    def open_tracker(self) -> None:
        import webbrowser

        webbrowser.open(TRACKER_PATH.as_uri())

    def edit_holdings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("GME holdings")
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)

        fields = {}
        for row, (key, label, current) in enumerate(
            [
                ("shares", "Shares held", self.cfg.get("shares") or 0.0),
                ("cost_basis", "Avg cost per share ($)", self.cfg.get("cost_basis") or 0.0),
            ]
        ):
            tk.Label(dialog, text=label, bg=BG, fg=MUTED, font=("Segoe UI", 9)).grid(
                row=row, column=0, sticky="w", padx=12, pady=(12 if row == 0 else 4, 2)
            )
            entry = tk.Entry(
                dialog, width=16, bg="#1f242c", fg=FG, insertbackground=FG,
                relief="flat", font=("Segoe UI", 10),
            )
            entry.insert(0, fmt_shares(float(current)) if current else "")
            entry.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 6))
            fields[key] = entry

        error = tk.Label(dialog, text="", bg=BG, fg=DOWN, font=("Segoe UI", 8))
        error.grid(row=2, column=0, sticky="w", padx=12)

        def apply() -> None:
            try:
                shares = float(fields["shares"].get() or 0)
                cost = float(fields["cost_basis"].get() or 0)
            except ValueError:
                error.config(text="Enter numbers only.")
                return
            if shares < 0 or cost < 0:
                error.config(text="Values cannot be negative.")
                return
            self.cfg["shares"] = shares
            self.cfg["cost_basis"] = cost
            save_config(self.cfg)
            dialog.destroy()
            self.render()

        buttons = tk.Frame(dialog, bg=BG)
        buttons.grid(row=3, column=0, pady=10)
        tk.Button(buttons, text="Save", command=apply, width=8).pack(side="left", padx=4)
        tk.Button(buttons, text="Cancel", command=dialog.destroy, width=8).pack(side="left", padx=4)

        dialog.update_idletasks()
        x = self.winfo_x() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
        y = self.winfo_y() - dialog.winfo_height() - 8
        area = work_area_at(self.winfo_x(), self.winfo_y())
        dialog.geometry(
            f"+{max(area.left, min(x, area.right - dialog.winfo_width()))}"
            f"+{max(area.top, min(y, area.bottom - dialog.winfo_height()))}"
        )
        fields["shares"].focus_set()
        dialog.grab_set()
        dialog.bind("<Return>", lambda _e: apply())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

    def quit_widget(self) -> None:
        if self._fetch_job:
            self.after_cancel(self._fetch_job)
        self.cfg["position"] = [self.winfo_x(), self.winfo_y()]
        if self.quote:
            self.cfg["last_quote"] = self.quote
        save_config(self.cfg)
        self.destroy()


def main() -> int:
    if "--check" in sys.argv:
        try:
            quote = fetch_quote()
        except Exception as exc:
            print(f"quote feed FAILED: {type(exc).__name__}: {exc}")
            return 1
        print(f"GME            {quote['price']:,.2f}")
        print(f"previous close {quote['prev_close']:,.2f}")
        print(f"change         {quote['change']:+,.2f} ({quote['change_pct']:+.2f}%)")
        print(f"day range      {quote['day_low']} - {quote['day_high']}")
        print(f"as of          {exchange_time(quote)} ET")
        print(f"market         {market_state(quote)}")
        print(f"next poll in   {poll_delay(quote) / 1000:.0f}s")
        return 0

    set_dpi_aware()
    GmeWidget().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
