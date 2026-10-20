"""Render widget.py in a throwaway process and save its real pixels as PNG.

Uses PrintWindow(PW_RENDERFULLCONTENT): BitBlt/CopyFromScreen misses colour-keyed
layered windows, which is what -transparentcolor makes this panel.
"""

import ctypes
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

ROOT = Path(r"C:\Users\baotr\GIT\gme-tracker")
OUT = Path(__file__).with_name("shots")
OUT.mkdir(exist_ok=True)

spec = importlib.util.spec_from_file_location("gmewidget", ROOT / "widget.py")
w = importlib.util.module_from_spec(spec)
sys.modules["gmewidget"] = w
spec.loader.exec_module(w)

real = json.loads((ROOT / "widget-config.json").read_text(encoding="utf-8"))
# Scratch config so the throwaway instance never writes to the user's real one.
w.CONFIG_PATH = Path(tempfile.mkdtemp(prefix="gmewidget-shot-")) / "config.json"
w.set_dpi_aware()

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_ulong), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort), ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_ulong), ("biSizeImage", ctypes.c_ulong),
        ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_ulong), ("biClrImportant", ctypes.c_ulong),
    ]


def capture(hwnd: int) -> Image.Image:
    rect = w.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    hdc_screen = user32.GetDC(0)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    gdi32.SelectObject(hdc, bmp)
    user32.PrintWindow(hwnd, hdc, 2)  # PW_RENDERFULLCONTENT

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight = width, -height  # negative = top-down
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(hdc, bmp, 0, height, buf, ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(0, hdc_screen)
    return img


def flatten(img: Image.Image, scale: int = 3) -> Image.Image:
    """Composite over a mid-tone so the colour-keyed corners read as transparent."""
    bg = Image.new("RGBA", img.size, (86, 108, 140, 255))
    flat = Image.alpha_composite(bg, img).convert("RGB")
    return flat.resize((flat.width * scale, flat.height * scale), Image.LANCZOS)


app = w.GmeWidget()
app.cfg["shares"] = real.get("shares") or 0.0
app.cfg["cost_basis"] = real.get("cost_basis") or 0.0
app.cfg["position"] = [160, 160]
app.cfg["compact"] = False
app._apply_view()
app._place()

# Pump the event loop so the live fetch lands and we capture real numbers.
deadline = time.time() + 8
while time.time() < deadline:
    app.update()
    if app.quote and app.quote.get("fetched_at", 0) > time.time() - 30:
        break
    time.sleep(0.05)

app.render()
app.geometry("+160+160")
app.update()
app.update_idletasks()
hwnd = int(app.wm_frame(), 16)

quote = app.quote
print("quote:", "live" if quote and quote.get("fetched_at", 0) > time.time() - 60 else "cached")
print("price:", quote and quote["price"], "shares:", app.cfg["shares"], "cost:", app.cfg["cost_basis"])

flatten(capture(hwnd)).save(OUT / "full.png")
print("full panel:", app._size)

app.toggle_compact()
app.update()
app.update_idletasks()
app.geometry("+160+320")
app.update()
app.update_idletasks()
flatten(capture(int(app.wm_frame(), 16))).save(OUT / "compact.png")
print("compact panel:", app._size)

app.toggle_compact()
app.update()
app.destroy()
print("saved:", OUT)
