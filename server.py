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

import re
import sys

# Windows console codepages (e.g. cp1252) can't encode characters some apps put in
# window titles (zero-width spaces, emoji, etc.) -- force UTF-8 so a stray character
# can't crash the server mid-response.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pywinauto import Desktop
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

# Raised from 3000 -- Refinery found the default was silently insufficient for
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


def _find_elements(elem, name, control_type, exact_match, results, counter=None):
    # counter has no depth/max_depth cap deliberately -- a match could legitimately be
    # very deep (e.g. inside a browser page), and unlike _dump this doesn't need to
    # bound *output size*, just total work. MAX_NODES alone (no depth cutoff) is the
    # right guard here.
    if counter is None:
        counter = [0]
    if counter[0] >= MAX_NODES:
        return
    counter[0] += 1

    try:
        info = elem.element_info
        elem_name = _norm(info.name)
        elem_type = info.control_type
    except Exception:
        return

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
            _find_elements(child, name, control_type, exact_match, results, counter)
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
    after Refinery hit this for real trying to verify a background tab's state.

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
        listing = "\n".join(
            f"  [{i}] {m.element_info.control_type} '{m.element_info.name}'"
            for i, m in enumerate(matches)
        )
        return None, (
            f"{len(matches)} elements matched name={name!r} -- nothing was clicked/typed "
            f"into, to avoid guessing wrong. Re-call with `index` set:\n{listing}"
        )

    return matches[0], None


@mcp.tool()
def click_element(
    title: str,
    name: str,
    control_type: str = None,
    exact_match: bool = True,
    index: int = None,
) -> str:
    """Click (invoke) a button or other actionable control by its visible name.

    This is a real action with real side effects -- it can press Send, Delete, Submit,
    whatever the target actually does. Requires an EXACT name match by default (set
    exact_match=False only if you've confirmed via read_window/find_in_window that a
    substring is safely unambiguous). If more than one element matches, nothing is
    clicked -- the response lists every match with its index so you can retry with
    that index rather than this tool guessing which one you meant.

    Tries UIA's native invoke() first (no cursor movement needed); falls back to a
    simulated click_input() if the control doesn't support invoke.
    """
    target, err = _resolve_target(title, name, control_type, exact_match, index)
    if err:
        return err

    warning = _element_browser_warning(target)
    label = f"[{target.element_info.control_type}] '{target.element_info.name}'"
    try:
        target.invoke()
        return f"Invoked {label}." + warning
    except Exception:
        pass
    try:
        target.click_input()
        return f"Clicked (via click_input fallback) {label}." + warning
    except Exception as e:
        return f"Found {label} but failed to click it: {e}"


@mcp.tool()
def type_into_element(
    title: str,
    name: str,
    text: str,
    control_type: str = "Edit",
    exact_match: bool = True,
    index: int = None,
) -> str:
    """Type text into an editable control (text box, chat input, etc.) by its name.

    Overwrites any existing content in the field. Same disambiguation rule as
    click_element: an exact name match is required by default, and an ambiguous match
    types nothing -- it lists candidates instead.

    `control_type` defaults to "Edit" since that's what most text inputs report as;
    pass None to search all control types if the target isn't a standard Edit control
    (e.g. a rich-text area often reports as "Document" instead).
    """
    target, err = _resolve_target(title, name, control_type, exact_match, index)
    if err:
        return err

    warning = _element_browser_warning(target)
    label = f"[{target.element_info.control_type}] '{target.element_info.name}'"

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
