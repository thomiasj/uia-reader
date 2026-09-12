"""
ghost_cursor: a labelled, click-through marker showing WHICH session is acting WHERE.

Launched as a short-lived detached process by uia-reader's click/type tools. It never
touches the real mouse pointer, never takes focus, and cannot be clicked -- input passes
straight through it to whatever is underneath. It exists so the user can see a session act
without losing their own cursor to it.

Motion: appears in the requesting session's own window (if that window can be found),
glides to the target control, pulses there, then fades. The caller waits for the glide to
arrive before acting, so the marker never shows something that has already happened.

Usage (internal):
    ghost_cursor.py <target_x> <target_y> <label> <start_x|-> <start_y|-> [duration_ms]

Coordinates are physical virtual-desktop pixels, the same space UI Automation reports
element rectangles in -- so it lands correctly on any monitor, including ones offset from
the primary.
"""

import sys
import time
import ctypes
import ctypes.wintypes as W

# Match UIA's physical-pixel coordinates. Without this, a scaled display would place the
# marker in the wrong spot while the click itself landed correctly -- a marker that lies.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk

user32 = ctypes.windll.user32
# Declare real signatures. Without them ctypes passes Python ints as 32-bit, so HWND_TOPMOST
# (-1) arrived in a 64-bit HWND slot as 0x00000000FFFFFFFF -- an invalid handle -- and every
# SetWindowPos call failed silently. The marker drew, had the right styles, and sat frozen at
# (0,0) while reporting nothing. Found by measuring its position, not by looking at it.
user32.GetParent.restype = W.HWND
user32.GetParent.argtypes = [W.HWND]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [W.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [W.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowPos.restype = W.BOOL
user32.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020   # clicks pass through
WS_EX_TOOLWINDOW = 0x00000080    # no taskbar / alt-tab entry
WS_EX_NOACTIVATE = 0x08000000    # never becomes the foreground window
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = W.HWND(-1)
SWP_NOSIZE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0001, 0x0010, 0x0040
SW_SHOWNOACTIVATE = 4

KEY = "#ff00fe"  # transparency key; nothing we draw uses it

# Marker colours: optional per-session overrides from callers.json ("colours"), otherwise a
# stable colour hashed from the label -- so the same session always looks the same.
PALETTE = ["#2f7fe0", "#9b59d0", "#e07b2f", "#2fa36b", "#d0467a", "#1aa3a3", "#c9a227", "#5a6b7d"]


def _load_colours():
    import json, os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "callers.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh).get("colours") or {})
    except Exception:
        return {}


def colour_for(label):
    return _load_colours().get(label) or PALETTE[sum(map(ord, label)) % len(PALETTE)]


def main():
    tx, ty = int(sys.argv[1]), int(sys.argv[2])
    label = (sys.argv[3] if len(sys.argv) > 3 else "Claude")[:14]
    # "-" means "no start position" -- appear at the target without gliding.
    have_start = len(sys.argv) > 5 and sys.argv[4] != "-" and sys.argv[5] != "-"
    sx = int(sys.argv[4]) if have_start else None
    sy = int(sys.argv[5]) if have_start else None
    duration = int(sys.argv[6]) if len(sys.argv) > 6 else 1800
    glide_ms = 350 if sx is not None else 0

    colour = colour_for(label)
    W_, H_ = 34 + 9 * len(label) + 22, 44

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.title("uia-reader ghost cursor")
    root.configure(bg=KEY)
    root.attributes("-transparentcolor", KEY)
    root.attributes("-topmost", True)

    c = tk.Canvas(root, width=W_, height=H_, bg=KEY, highlightthickness=0, bd=0)
    c.pack()
    # Arrow tip sits at the window's (0,0), so window position == pointer position.
    arrow = [0, 0, 0, 22, 5, 17, 9, 26, 13, 24, 9, 16, 16, 16]
    c.create_polygon(arrow, fill=colour, outline="white", width=2)
    c.create_rectangle(18, 20, W_ - 2, H_ - 2, fill=colour, outline="white", width=2)
    c.create_text(18 + (W_ - 20) // 2, 20 + (H_ - 22) // 2, text=label,
                  fill="white", font=("Segoe UI", 10, "bold"))

    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or W.HWND(root.winfo_id())
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                          | WS_EX_NOACTIVATE | WS_EX_TOPMOST)

    def place(x, y):
        # SetWindowPos rather than Tk geometry: Tk reads "-100" as "100 from the right
        # edge", which would misplace the marker on any monitor left of the primary.
        ok = user32.SetWindowPos(hwnd, HWND_TOPMOST, int(x), int(y), 0, 0,
                                 SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        if not ok:
            # Never fail silently again: a marker that stops moving must say so.
            sys.stderr.write("ghost_cursor: SetWindowPos failed (err %d)\n"
                             % ctypes.GetLastError())
        return ok

    start = (sx, sy) if sx is not None else (tx, ty)
    place(*start)
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

    t0 = time.perf_counter()
    end = t0 + duration / 1000.0
    fade_from = end - 0.45

    def tick():
        now = time.perf_counter()
        el = (now - t0) * 1000.0
        if glide_ms and el < glide_ms:
            p = el / glide_ms
            p = 1 - (1 - p) ** 3  # ease-out
            place(start[0] + (tx - start[0]) * p, start[1] + (ty - start[1]) * p)
        else:
            place(tx, ty)
        if now >= fade_from:
            a = max(0.0, (end - now) / (end - fade_from))
            try:
                root.attributes("-alpha", a)
            except Exception:
                pass
        if now >= end:
            root.destroy()
            return
        root.after(16, tick)

    root.after(0, tick)
    root.mainloop()


if __name__ == "__main__":
    main()
