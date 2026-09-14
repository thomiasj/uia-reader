"""
uia-reader: an MCP server that reads Windows UI Automation trees instead of screenshots.

Why this exists: reading a native app's on-screen state via a screenshot requires a
vision model to infer text/labels/state from pixels -- slow, imprecise, and expensive.
Windows already tracks the real structured state of every window (control types, names,
text content, enabled/focused/checked state) via the UI Automation API. This server
exposes that directly as text, the same way a browser's accessibility tree beats
screenshotting a browser tab.

Meaningful-node filtering: pure layout containers (Pane/Group/Custom/etc. with no name)
are still traversed but not printed, so the output reflects real content structure
instead of the deep wrapper-div-style nesting these apps often have. Indentation tracks
printed depth, not raw tree depth, so filtered-out wrappers don't produce misleading
indentation.
"""

import json
import os
import re
import subprocess
import sys
import time

# Windows console codepages (e.g. cp1252) can't encode characters some apps put in
# window titles (zero-width spaces, emoji, etc.) -- force UTF-8 so a stray character
# can't crash the server mid-response.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pywinauto import Desktop
from pywinauto import uia_defines as uia_defs
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("uia-reader")

# Control types worth printing on their own -- containers get traversed but not shown
# unless they have a name (e.g. a named region) or landmark role.
MEANINGFUL_TYPES = {
    "Button", "Text", "Edit", "MenuBar", "MenuItem", "ToolBar", "StatusBar",
    "CheckBox", "RadioButton", "ComboBox", "List", "ListItem", "Tab", "TabItem",
    "Hyperlink", "Image", "Slider", "ProgressBar", "Tree", "TreeItem", "Header",
    "HeaderItem", "Table", "DataGrid", "DataItem", "Document", "Calendar",
}

# Raised from 3000 -- real-world use found the default was silently insufficient for
# Chromium-based browser windows, where the toolbar/tab-strip subtree alone can contain
# thousands of nodes, exhausting the budget (traversal is depth-first) before ever
# reaching actual page content as a later sibling. This is a real budget increase, not
# just cosmetic -- see the max_depth fix below for the other half of this bug.
MAX_NODES = 8000


# For these control types, the accessible *name* is just a label ("Text editor") --
# the actual content lives in the control's value/text, which name never carries.
# Discovered 2026-08-24: a Notepad Document control's typed content was invisible to
# read_window until this was added, even though click/type actions worked correctly --
# the read and write paths were checking two different properties.
VALUE_BEARING_TYPES = {"Edit", "Document", "ComboBox"}


def _norm(s):
    """Collapse a control name to exactly the form the read tools print.

    Every tool here tells the caller to copy the name it displayed. That only holds if
    display and matching agree. They didn't: _dump printed names with newlines turned into
    spaces, while _find_elements matched the raw name -- so a control whose name wraps was
    shown one way and matchable only another, and an exact-match click on the printed name
    failed with "No element found". Found 2026-09-11 against Deskflow, whose radio button is
    really "Use this computer's keyboard and mouse\n(make this computer the server)". The
    tool was lying about its own output. Both paths now go through this.
    """
    return re.sub(r"\s+", " ", (s or "").replace("\n", " ")).strip()


# Ordinary window chrome. A tree containing nothing but these describes a window whose
# real content is not exposed to UI Automation -- which is NOT the same as an empty
# window, and reads identically to one. Reported 2026-09-11 from real use: ShareMouse's
# Monitor Manager returned only [Window]/[MenuBar]/Minimize/Maximize/Close while showing
# a full drag-and-drop display arrangement, and find_in_window returned "no matches" for
# navigation items plainly visible on screen. An absent signal read as a negative finding
# is the most expensive mistake this tool can cause, so it now says so out loud.
_CHROME_TYPES = {"Window", "TitleBar", "MenuBar"}
_CHROME_NAMES = {"minimize", "maximize", "close", "restore", "system", "application"}
_LINE_RE = re.compile(r"^\s*\[([A-Za-z]+)\]\s*(.*)$")

# Below this many non-chrome controls, a visually substantial window is almost certainly
# custom-drawn rather than genuinely empty.
SPARSE_CONTENT_THRESHOLD = 2
# Smaller than roughly 200x200 and a window can legitimately hold almost nothing.
SPARSE_MIN_AREA = 40000


def _content_node_count(lines):
    """Count emitted nodes that aren't ordinary window chrome."""
    n = 0
    for line in lines:
        m = _LINE_RE.match(line)
        if not m:
            continue
        ctrl_type, label = m.group(1), m.group(2).strip()
        if ctrl_type in _CHROME_TYPES:
            continue
        if label.lower() in _CHROME_NAMES:
            continue
        n += 1
    return n


def _sparse_tree_note(win, lines):
    """Flag a window that is visually substantial but exposes essentially nothing."""
    content = _content_node_count(lines)
    if content > SPARSE_CONTENT_THRESHOLD:
        return ""
    try:
        rect = win.rectangle()
        width, height = rect.width(), rect.height()
    except Exception:
        return ""
    if width * height < SPARSE_MIN_AREA:
        return ""
    return (
        f"\n\n[uia-reader note] This window is {width}x{height} but exposed only "
        f"{content} control(s) beyond window chrome. Read that as \"not exposed to UI "
        "Automation\", NOT as \"the window is empty\" -- the read succeeded and found "
        "nothing to report. Custom-drawn/owner-drawn surfaces (canvases, diagram and "
        "arrangement views, some custom navigation lists) are painted as pixels with no "
        "UIA representation at all. Fall back to a screenshot, plus zoom for detail, for "
        "this window. Note one window can mix both: standard controls elsewhere in it may "
        "still read perfectly, so a sparse result here doesn't condemn the whole app.\n"
        "Before concluding that, though, READ IT ONCE MORE. A window that normally exposes "
        "a full tree was observed collapsing to a single node exactly once in testing "
        "(2026-09-11, not reproducible across four further attempts) -- so a transient "
        "sparse read is possible, presumably mid-repaint or while the app is busy. A "
        "second read costs nothing and separates \"never exposed\" from \"not ready yet\"."
    )


def _dump(elem, printed_depth, lines, counter, max_depth):
    if counter[0] >= MAX_NODES:
        return
    counter[0] += 1

    try:
        info = elem.element_info
        name = _norm(info.name)
        ctrl_type = info.control_type
    except Exception as e:
        lines.append("  " * printed_depth + f"<error reading element: {e}>")
        return

    value = ""
    if ctrl_type in VALUE_BEARING_TYPES:
        try:
            value = _norm(elem.window_text())
        except Exception:
            pass

    is_meaningful = bool(name) or bool(value) or ctrl_type in MEANINGFUL_TYPES
    if is_meaningful:
        display_name = name[:300] if name else ""
        if value and value != name:
            display_name = f'{display_name}: "{value[:300]}"' if display_name else f'"{value[:300]}"'
        lines.append("  " * printed_depth + f"[{ctrl_type}] {display_name}")
        next_depth = printed_depth + 1
    else:
        next_depth = printed_depth

    # Cutoff is on STRUCTURAL (printed) depth, not raw tree depth -- matches what the
    # indentation in the output actually represents, and stops deep unnamed-wrapper
    # nesting (common in Chromium's internal tree) from burning through max_depth
    # before reaching any real content.
    if next_depth > max_depth:
        lines.append("  " * next_depth + f"... (truncated, max_depth={max_depth} hit)")
        return

    try:
        for child in elem.children():
            _dump(child, next_depth, lines, counter, max_depth)
            if counter[0] >= MAX_NODES:
                lines.append("  " * next_depth + "... (truncated, MAX_NODES hit)")
                return
    except Exception:
        pass


@mcp.tool()
def list_windows(only_visible: bool = True) -> str:
    """List top-level windows currently on screen, with title and control class.

    Use this first to find the exact window title to pass to read_window(). Titles
    are matched by case-insensitive substring, so any distinctive fragment works --
    no need to worry about a leading "*" or other unsaved-changes indicator.
    """
    desktop = Desktop(backend="uia")
    lines = []
    for w in desktop.windows():
        try:
            if only_visible and not w.is_visible():
                continue
            title = w.window_text().strip()
            if not title:
                continue
            lines.append(f"'{title}' | class={w.friendly_class_name()}")
        except Exception:
            continue
    return "\n".join(lines) if lines else "(no visible titled windows found)"


@mcp.tool()
def read_window(title: str, max_depth: int = 40) -> str:
    """Read a window's actual content as structured text via UI Automation.

    `title` matches anywhere in the window title, case-insensitively -- pass any
    distinctive fragment shown by list_windows. Returns each meaningful control's type
    and text/name, indented by structural depth -- this is the real rendered content
    (message text, button labels, live status), not an
    image. Use this instead of a screenshot whenever you need to know what a window
    actually says or what state its controls are in.

    Chromium-based browser windows nest real page content much deeper than native app
    windows typically do (their own toolbar/tab-strip chrome alone can be dozens of
    levels deep) -- the default here (40) was raised specifically for that; if a browser
    window's dump looks like it's missing content, try an even higher value before
    assuming the content isn't there.
    """
    win = _locate_window(title)
    if win is None:
        return f"No window found matching title starting with: {title!r}"

    lines = []
    counter = [0]
    _dump(win, 0, lines, counter, max_depth)
    result = "\n".join(lines) if lines else "Window found but no readable content (may be empty or unsupported)."
    return result + _sparse_tree_note(win, lines) + _browser_warning(win.window_text())


def _find_elements(elem, name, control_type, exact_match, results, counter=None, seen=None):
    # counter has no depth/max_depth cap deliberately -- a match could legitimately be
    # very deep (e.g. inside a browser page), and unlike _dump this doesn't need to
    # bound *output size*, just total work. MAX_NODES alone (no depth cutoff) is the
    # right guard here.
    #
    # `seen` holds UIA runtime ids already visited. Without a depth cap, a tree that loops
    # back on itself (Chromium's can, transiently) would reach the same control again and
    # again until MAX_NODES. Real use hit "39 elements matched" on 2026-09-13 for a button
    # find_in_window showed exactly once; not reproducible at rest, so this guards the
    # plausible cause and the listing below shows ids so a repeat would be diagnosable.
    if counter is None:
        counter = [0]
    if seen is None:
        seen = set()
    if counter[0] >= MAX_NODES:
        return
    counter[0] += 1

    try:
        info = elem.element_info
        elem_name = _norm(info.name)
        elem_type = info.control_type
        rid = tuple(info.runtime_id or ())
    except Exception:
        return
    if rid:
        if rid in seen:
            return
        seen.add(rid)

    name_ok = True
    if name is not None:
        # Normalise the needle too -- a caller copying a wrapped name out of read_window
        # pastes the displayed (space-joined) form, and that must match.
        wanted = _norm(name)
        name_ok = (elem_name == wanted) if exact_match else (wanted.lower() in elem_name.lower())
    type_ok = control_type is None or elem_type == control_type

    if name_ok and type_ok and (name is not None or control_type is not None):
        results.append(elem)

    try:
        for child in elem.children():
            _find_elements(child, name, control_type, exact_match, results, counter, seen)
            if counter[0] >= MAX_NODES:
                return
    except Exception:
        pass


BROWSER_MARKERS = ("microsoft edge", "google chrome", "mozilla firefox")


def _browser_warning(win_title):
    """Flag a real, confirmed limitation: UIA reflects whichever tab is CURRENTLY
    ACTIVE in a browser window, with no way to target a specific background tab.
    Reproduced live 2026-08-30 (open tab A, background it, open+activate tab B in the
    same window -- the window's title and UIA content immediately follow tab B, not A)
    after this was hit in real use trying to verify a background tab's state.

    Not a bug in this server to "fix" -- it's an accurate reflection of what's actually
    on screen, same as a screenshot would show. The real fix is routing browser-tab
    reads to the dedicated browser tools (read_page/get_page_text, DevTools Protocol
    -based) instead, which don't depend on which tab happens to be visually active.
    """
    t = (win_title or "").lower().replace("​", "")
    if any(m in t for m in BROWSER_MARKERS):
        return (
            "\n\n[uia-reader note] This window is a browser. UI Automation reflects "
            "whichever tab is CURRENTLY ACTIVE/visible in this window -- there is no "
            "way to target a specific background tab, and what's shown here silently "
            "becomes the wrong tab's content the moment a different tab is active, "
            "with no error raised. For reading browser tab content, prefer the "
            "dedicated browser tools (e.g. read_page/get_page_text) instead -- they "
            "read via the browser's own APIs and work correctly regardless of which "
            "tab is visually active. Reproduced live 2026-08-30."
        )
    return ""


def _element_browser_warning(elem):
    try:
        return _browser_warning(elem.top_level_parent().window_text())
    except Exception:
        return ""


def _locate_window(title):
    # Substring, case-insensitive match -- not anchored to the start. A prefix anchor
    # breaks the moment a title gains a leading indicator (Notepad's unsaved-changes
    # "*", a browser tab's favicon-adjacent text, etc.), which is common enough that
    # substring matching is the safer default.
    desktop = Desktop(backend="uia")
    needle = title.lower()
    for w in desktop.windows():
        try:
            if needle in w.window_text().lower():
                return w
        except Exception:
            continue
    return None


def _resolve_target(title, name, control_type, exact_match, index):
    """Shared lookup/disambiguation logic for click_element and type_into_element.

    Returns (element, None) on a clean unique match, or (None, error_message) --
    including the deliberate "ambiguous, here are your options" case, which is a
    result, not a failure.
    """
    win = _locate_window(title)
    if win is None:
        return None, f"No window found matching title starting with: {title!r}"

    matches = []
    _find_elements(win, name, control_type, exact_match, matches)

    if not matches:
        return None, (
            f"No element found with name={name!r} control_type={control_type!r} "
            f"in window {title!r}. Try read_window() or find_in_window() first to "
            f"confirm the exact visible name."
        )

    if index is not None:
        if index < 0 or index >= len(matches):
            return None, f"index {index} out of range -- only {len(matches)} match(es) found."
        return matches[index], None

    if len(matches) > 1:
        def where(m):
            # Id and position tell "several different controls" apart from "one control
            # reached several ways" -- the names alone can be byte-identical either way.
            try:
                r = m.element_info.rectangle
                return f" id={'.'.join(map(str, m.element_info.runtime_id or ()))} at ({r.left},{r.top})"
            except Exception:
                return ""
        listing = "\n".join(
            f"  [{i}] {m.element_info.control_type} '{m.element_info.name}'{where(m)}"
            for i, m in enumerate(matches)
        )
        return None, (
            f"{len(matches)} elements matched name={name!r} -- nothing was clicked/typed "
            f"into, to avoid guessing wrong. Re-call with `index` set:\n{listing}"
        )

    return matches[0], None


# --- labelled ghost cursor ---------------------------------------------------------------

# Optional per-machine label map, kept OUT of the code on purpose: callers.json beside this
# file maps working-folder names to the short names their windows are titled with (and
# optional marker colours). It is private configuration -- a list of someone's projects --
# so it is gitignored in the public repo. Without it, the label is the folder name itself.
def _load_callers():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "callers.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {k.lower(): v for k, v in (data.get("folders") or {}).items()}
    except Exception:
        return {}


_CALLER_BY_FOLDER = _load_callers()
_GHOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ghost_cursor.py")
_GLIDE_SECONDS = 0.38  # matches the marker's glide, so it arrives before the action happens


def _caller_label(caller):
    if caller:
        return caller.strip()[:14]
    folder = os.path.basename(os.getcwd())
    return (_CALLER_BY_FOLDER.get(folder.lower()) or folder[:14] or "Claude")


def _caller_window_point(label):
    # EXACT title match, deliberately -- substring matching can't address one-letter titles
    # like "I" or "B" (they match any window containing that letter).
    try:
        for w in Desktop(backend="uia").windows():
            if w.window_text() == label:
                r = w.rectangle()
                return (r.left + (r.right - r.left) // 2, r.top + 60)
    except Exception:
        pass
    return None


def _show_ghost(target, label):
    """Launch the marker and return how long to wait for it to arrive (0 if not shown)."""
    try:
        r = target.rectangle()
        tx, ty = r.left + (r.right - r.left) // 2, r.top + (r.bottom - r.top) // 2
        start = _caller_window_point(label)
        args = [sys.executable, _GHOST, str(tx), str(ty), label,
                str(start[0]) if start else "-", str(start[1]) if start else "-", "1800"]
        # stdout/stderr MUST be detached: this server speaks MCP over stdout, and a child
        # inheriting it would corrupt every subsequent tool response.
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=0x08000000 | 0x00000008)  # NO_WINDOW | DETACHED
        return _GLIDE_SECONDS if start else 0.12
    except Exception:
        return 0.0


# --- cursor-free activation ---------------------------------------------------------------

def _cursor_pos():
    import ctypes
    import ctypes.wintypes as W
    p = W.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def _set_cursor_pos(xy):
    import ctypes
    ctypes.windll.user32.SetCursorPos(int(xy[0]), int(xy[1]))


# --- action scope ------------------------------------------------------------------------
#
# Reading is safe anywhere; acting is not. Before 2026-09-12 click/type could act in ANY
# window on the machine, and once clicks stopped moving the real mouse, the user no longer
# had even that cue that something was happening. So every action is scoped by default to
# the calling session's own window, with two explicit, separately-named opt-outs:
#
#   allow_other_window   -- an ordinary app window (Edge, a settings dialog, ...).
#   allow_other_session  -- another Claude window. Deliberately stricter: typing into or
#                           clicking in another session acts with THAT session's
#                           permissions, not yours -- cross-session permission laundering.
#                           Send that session a message instead.

def _process_exe(pid):
    import ctypes
    import ctypes.wintypes as W
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = W.HANDLE
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = W.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        k32.CloseHandle(h)


def _owning_window(target, title):
    try:
        top = target.top_level_parent()
        if top is not None:
            return top
    except Exception:
        pass
    return _locate_window(title)


def _scope_refusal(target, title, who, allow_other_window, allow_other_session, verb):
    """Return a refusal message if this action is out of scope, else None."""
    win = _owning_window(target, title)
    try:
        win_title = win.window_text()
    except Exception:
        win_title = title
    if win_title == who:
        return None  # the caller's own window
    try:
        exe = _process_exe(win.element_info.process_id)
    except Exception:
        exe = ""
    if exe == "claude.exe":
        if allow_other_session:
            return None
        return (
            f"Refused: {win_title!r} is another Claude window, not {who!r}'s own. Nothing was "
            f"{verb}. Clicking or typing into another session acts with that session's "
            f"permissions rather than yours -- cross-session permission laundering. Send it a "
            f"message instead. Only pass allow_other_session=True when the user has asked for "
            f"that specific action in that specific session."
        )
    if allow_other_window:
        return None
    return (
        f"Refused: {win_title!r} is not {who!r}'s own window. Nothing was {verb}. Actions "
        f"outside the calling session's window need allow_other_window=True -- pass it when "
        f"the task genuinely requires this window (e.g. focusing a browser tab), so acting "
        f"elsewhere on the user's machine is always a deliberate choice."
    )


def _activate_without_mouse(target):
    """Activate a control WITHOUT the real pointer, pressing it AT MOST ONCE.

    Returns (method, verified) once a method has been sent, or None only if NO method could
    be sent at all. `verified` is True when the control's own state confirmed the change,
    False when it was sent but the state didn't move, there is no state to check, or the
    call reported an error.

    THE RULE: move on to the next method only when the current one could not be SENT --
    the control doesn't offer that pattern, so getting the interface failed. Once a press
    call has been made, stop, whatever happened next:

    - A slow effect is not a missing effect (2026-09-12: the old version gave up on a state
      check after 80ms and tried the next method).
    - An error from the press call is not proof it wasn't delivered (2026-09-14): an app can
      act on the press and then fail to answer. A stand-in that counts and then throws was
      pressed TWICE by the previous version -- invoke, then its default action -- which then
      reported "Nothing was clicked".

    A second press is not a retry; it acts on whatever the screen shows now. That matters
    here because a Claude session was archived at 17:32:46 on 2026-09-12 while this tool
    was being tested against its options menu (which contains Archive). What exactly pressed
    Archive is not established -- see the note in the README.
    """
    def wait_for(read, before, timeout=1.0):
        # Poll instead of one short sleep: effects can be asynchronous.
        end = time.time() + timeout
        while time.time() < end:
            try:
                if read() != before:
                    return True
            except Exception:
                return False
            time.sleep(0.05)
        return False

    def sent_but_errored(method, e):
        return (f"{method} (the call reported an error after it was made, so it may still have "
                f"taken effect: {type(e).__name__})", False)

    try:
        invoke = target.iface_invoke
    except Exception:
        invoke = None  # not offered: nothing sent
    if invoke is not None:
        try:
            invoke.Invoke()
        except Exception as e:
            return sent_but_errored("invoke", e)
        return ("invoke", False)  # no generic state to confirm against

    try:
        tg = target.iface_toggle
        before = tg.CurrentToggleState
    except Exception:
        tg = None
    if tg is not None:
        try:
            tg.Toggle()
        except Exception as e:
            return sent_but_errored("toggle", e)
        return ("toggle", wait_for(lambda: tg.CurrentToggleState, before))

    try:
        si = target.iface_selection_item
        was = si.CurrentIsSelected
    except Exception:
        si = None
    if si is not None:
        try:
            si.Select()
        except Exception as e:
            return sent_but_errored("select", e)
        return ("select", True if was else wait_for(lambda: si.CurrentIsSelected, False))

    try:
        ec = target.iface_expand_collapse
        state = ec.CurrentExpandCollapseState  # 0 collapsed, 1 expanded, 2 partial, 3 leaf
    except Exception:
        ec = None
    if ec is not None and state != 3:
        opening = state != 1
        method = "expand" if opening else "collapse"
        try:
            (ec.Expand if opening else ec.Collapse)()
        except Exception as e:
            return sent_but_errored(method, e)
        return (method, wait_for(lambda: ec.CurrentExpandCollapseState, state))

    # pywinauto 0.6.9 wrappers have no `iface_legacy_iaccessible` attribute, so until
    # 2026-09-14 this step raised AttributeError, was caught, and never ran for any control.
    # The pattern is only reachable through uia_defines. Edge's saved tab-group buttons are a
    # real case that needs it: ExpandCollapse + default action "Press", and when the button
    # reports itself as a leaf (state 3) the expand step above is skipped.
    try:
        la = uia_defs.get_elem_interface(target.element_info.element, "LegacyIAccessible")
        action = la.CurrentDefaultAction
    except Exception:
        la, action = None, None
    if la is not None and action:  # an empty default action means there is nothing to send
        try:
            la.DoDefaultAction()
        except Exception as e:
            return sent_but_errored(f"default action ({action})", e)
        return (f"default action ({action})", False)

    return None


@mcp.tool()
def click_element(
    title: str,
    name: str,
    control_type: str = None,
    exact_match: bool = True,
    index: int = None,
    allow_mouse: bool = False,
    show_cursor: bool = True,
    caller: str = None,
    allow_other_window: bool = False,
    allow_other_session: bool = False,
) -> str:
    """Click (activate) a control by its visible name -- WITHOUT moving the user's mouse.

    SCOPE: by default this only acts in the calling session's OWN window. Another app's
    window needs allow_other_window=True. Another Claude window needs
    allow_other_session=True -- deliberately separate, because acting in another session
    uses that session's permissions (send it a message instead). Refusals click nothing.

    This is a real action with real side effects -- it can press Send, Delete, Submit,
    whatever the target actually does. Requires an EXACT name match by default (set
    exact_match=False only if you've confirmed via read_window/find_in_window that a
    substring is safely unambiguous). If more than one element matches, nothing is
    clicked -- the response lists every match with its index so you can retry with
    that index rather than this tool guessing which one you meant.

    The user's mouse pointer is theirs. This tries every UI Automation method that
    activates a control directly -- invoke, toggle, select, expand/collapse, and the
    control's default action -- and never touches the real pointer unless you pass
    allow_mouse=True. Even then the pointer is put back where the user left it.

    A labelled marker shows which sibling is acting and where: it starts in the calling
    session's own window, glides to the target and fades, and cannot be clicked or take
    focus. `caller` overrides the label (defaults to the session's short name). Pass
    show_cursor=False to act without it.
    """
    target, err = _resolve_target(title, name, control_type, exact_match, index)
    if err:
        return err

    warning = _element_browser_warning(target)
    label = f"[{target.element_info.control_type}] '{target.element_info.name}'"
    who = _caller_label(caller)

    refusal = _scope_refusal(target, title, who, allow_other_window, allow_other_session,
                             "clicked")
    if refusal:
        return refusal

    if show_cursor:
        wait = _show_ghost(target, who)
        if wait:
            time.sleep(wait)

    done = _activate_without_mouse(target)
    if done:
        method, verified = done
        if verified:
            return (f"Activated {label} via {method} -- confirmed by the control's own state. "
                    f"Your mouse was not touched." + warning)
        return (f"Sent {method} to {label} ONCE, but its effect is NOT confirmed -- the "
                f"control's state didn't change within a second, or it has no state to check. "
                f"Your mouse was not touched. Verify with read_window() before assuming it "
                f"worked, and do NOT simply click again: if the effect was just slow, a second "
                f"press acts on whatever the screen shows now." + warning)

    if not allow_mouse:
        return (
            f"Found {label}, but it supports no cursor-free way to activate it (no invoke, "
            f"toggle, select, expand/collapse or default action). Nothing was clicked. "
            f"Re-call with allow_mouse=True to use the user's real mouse pointer -- it will "
            f"be moved for the click and then put back."
        ) + warning

    before = _cursor_pos()
    try:
        target.click_input()
        return (f"Clicked {label} with the real mouse (allow_mouse=True); pointer returned "
                f"to {before}." + warning)
    except Exception as e:
        return f"Found {label} but failed to click it even with the mouse: {e}"
    finally:
        _set_cursor_pos(before)

@mcp.tool()
def type_into_element(
    title: str,
    name: str,
    text: str,
    control_type: str = "Edit",
    exact_match: bool = True,
    index: int = None,
    show_cursor: bool = True,
    caller: str = None,
    allow_other_window: bool = False,
    allow_other_session: bool = False,
) -> str:
    """Type text into an editable control (text box, chat input, etc.) by its name.

    SCOPE: same as click_element -- the calling session's own window by default,
    allow_other_window=True for another app, allow_other_session=True for another Claude
    window. Typing into another session's chat box is the sharpest case of acting with
    permissions that aren't yours; send that session a message instead.

    Overwrites any existing content in the field. Same disambiguation rule as
    click_element: an exact name match is required by default, and an ambiguous match
    types nothing -- it lists candidates instead.

    `control_type` defaults to "Edit" since that's what most text inputs report as;
    pass None to search all control types if the target isn't a standard Edit control
    (e.g. a rich-text area often reports as "Document" instead).

    Never uses the mouse. Shows the same labelled marker as click_element so the user can
    see which sibling is typing where; pass show_cursor=False to skip it.
    """
    target, err = _resolve_target(title, name, control_type, exact_match, index)
    if err:
        return err

    warning = _element_browser_warning(target)
    label = f"[{target.element_info.control_type}] '{target.element_info.name}'"

    refusal = _scope_refusal(target, title, _caller_label(caller), allow_other_window,
                             allow_other_session, "typed")
    if refusal:
        return refusal

    if show_cursor:
        wait = _show_ghost(target, _caller_label(caller))
        if wait:
            time.sleep(wait)

    # Prefer atomic, pattern-based writes over simulated keystrokes -- keystroke
    # simulation (type_keys) was found live to occasionally corrupt input (observed:
    # "type" -> "yype") on at least one modern app, where SetValue/set_text write the
    # whole string in one call with no such risk. Try in order of reliability.
    try:
        target.set_text(text)
        return f"Set text on {label} (via set_text)." + warning
    except Exception:
        pass
    try:
        target.iface_value.SetValue(text)
        return f"Set text on {label} (via ValuePattern.SetValue)." + warning
    except Exception:
        pass
    try:
        target.set_focus()
        target.type_keys(text, with_spaces=True)
        return (
            f"Typed into {label} via simulated keystrokes (type_keys) -- this app "
            f"doesn't support the more reliable pattern-based write, so this fallback "
            f"carries a small risk of dropped/altered characters. Worth verifying with "
            f"read_window() or find_in_window()." + warning
        )
    except Exception as e:
        return f"Found {label} but failed to type into it: {e}"


@mcp.tool()
def find_in_window(title: str, query: str, max_depth: int = 40) -> str:
    """Search a window's UI Automation tree for elements whose text contains `query`.

    Faster than reading the whole tree when you just need to know whether/where a
    specific piece of text or a specific control (e.g. a button label) appears.
    Case-insensitive substring match. Returns matching lines with their control type.

    Chromium-based browser windows nest real page content much deeper than native app
    windows typically do -- the default here (40) was raised specifically for that.
    """
    win = _locate_window(title)
    if win is None:
        return f"No window found matching title starting with: {title!r}"

    lines = []
    counter = [0]
    _dump(win, 0, lines, counter, max_depth)
    q = query.lower()
    matches = [l for l in lines if q in l.lower()]
    result = "\n".join(matches) if matches else f"No matches for {query!r} in window {title!r}."
    return result + _sparse_tree_note(win, lines) + _browser_warning(win.window_text())


if __name__ == "__main__":
    mcp.run()
