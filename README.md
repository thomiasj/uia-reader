# uia-reader

An MCP server that reads a Windows window's real content via the UI Automation API
instead of screenshotting it and asking a vision model to guess.

## Why

A screenshot is the screen's raw pixel data — that's already "the binary." What's
actually missing is the *structured* layer above it: the control tree Windows itself
maintains for every window (control types, names, text content, state). Browsers
already get this treatment (`read_page` reads the accessibility tree instead of
screenshotting a tab); this does the same thing for native desktop windows.

Validated 2026-08 against a real CCD (Claude Code Desktop) window: the full chat
transcript, message action buttons, live streaming status, token counts, and model/
effort settings were all readable as exact structured text — no vision, no guessing.

## Setup

```bash
pip install -r requirements.txt
```

## Register as an MCP server

Register at **user scope** (`-s user`) so it's available from any project, not just
the directory you happened to run the command from — `claude mcp add`'s default scope
is local-to-the-current-project, which is easy to get wrong by accident.

```bash
claude mcp add uia-reader -s user -- python "C:\path\to\uia-reader\server.py"
```

Replace `C:\path\to\uia-reader` with wherever you cloned this repo.

## Tools

- `list_windows(only_visible=True)` — list on-screen windows with title + control class.
  Use this first to find the exact title to pass to the others.
- `read_window(title, max_depth=40)` — full structured content dump for a window whose
  title contains `title` (case-insensitive substring, anywhere in the title — not
  anchored to the start; see the title-matching note below). Meaningful controls only
  (empty layout wrappers are traversed but not printed), and text-bearing controls
  (Edit/Document/ComboBox) show their actual value inline, not just their label. Default
  depth is deliberately generous (40) — Chromium-based browser windows nest real content
  far deeper than native apps typically do.
- `find_in_window(title, query, max_depth=40)` — case-insensitive substring search across
  a window's content; faster than a full dump when you just need to know if/where
  something appears.
- `click_element(title, name, control_type=None, exact_match=True, index=None)` —
  invoke/click a control by its exact visible name. Ambiguous matches (more than one
  element with that name) click nothing and list candidates by index instead of
  guessing.
- `type_into_element(title, name, text, control_type="Edit", exact_match=True, index=None)`
  — write text into an editable control. Same disambiguation rule as `click_element`.

## Real bugs found and fixed during testing (2026-08-24)

- **Title matching must be substring, not prefix-anchored.** An anchored `^title.*`
  match breaks the instant a title gains a leading indicator — Notepad's unsaved-changes
  `*` was enough to make a previously-working title stop matching mid-session. Switched
  to case-insensitive substring matching everywhere.
- **A control's accessible *name* often isn't its *content*.** A Notepad text area's
  name is the static label "Text editor" — the actual typed text lives in the value,
  which `read_window` originally never looked at, so typed content was invisible to
  reads even though writes were working correctly. Fixed by pulling `window_text()` for
  value-bearing control types (Edit/Document/ComboBox) and showing it inline.
- **Simulated keystrokes (`type_keys`) can silently corrupt text.** Observed live:
  typing "...click/type test" landed as "...click/yype test" — a dropped/altered
  character with no error raised. `type_into_element` now tries atomic, pattern-based
  writes first (`set_text()`, then `ValuePattern.SetValue()` directly) and only falls
  back to simulated keystrokes — with an explicit warning in the response — if neither
  is supported by the target control.

## Real limitation found via real use (2026-08-30)

Hit this trying to verify a background browser tab's state while driving the
foreground tab with separate browser-automation tools: **UI Automation reflects whichever tab
is currently active/visible in a browser window — there is no way to target a specific
background tab.** Confirmed by direct reproduction: switching the active tab in an Edge
window immediately changes what the window's title and UIA tree show, with zero
indication of "this is stale" or "this is the wrong tab" — it just silently becomes the
new tab's content. This is accurate to what's actually on screen (same as a screenshot
would show), not a bug in the traversal logic, but it makes this server the wrong tool
for reading browser tab content specifically — read_page/get_page_text (DevTools
Protocol-based, not screen-state-based) are correct there since they don't depend on
which tab is visually active.

All four tools now detect when the target window is a browser (Edge/Chrome/Firefox) and
append an explicit warning to the response rather than returning ambiguous content
silently — cheapest fix that doesn't require guessing at tab-targeting logic this
server has no way to implement.

## Second real bug found via real use: `max_depth` was silently dead (2026-08-30)

Real-world use separately reported that reading a browser window with the (then-)default
depth returned only toolbar/chrome, missing all real page content — and that raising
`max_depth` to 40 fixed it. Checking the code before taking that fix at face value
turned up something worse: **`max_depth` was never actually passed into the traversal
function at all** — the parameter existed on `read_window`/`find_in_window` and did
nothing, silently. The real cause of the missing content was almost certainly
`MAX_NODES` (a flat 3000-node cap) being exhausted somewhere inside the browser's own
toolbar/tab-strip subtree — which can easily contain thousands of nodes — before
traversal (depth-first) ever reached the actual page content as a later sibling.

Fixed both halves: `max_depth` is now actually wired into the traversal and enforced
(on structural/printed depth, matching what the indentation represents), and `MAX_NODES`
was raised 3000 → 8000 to give real headroom. Verified live against a real Wikipedia
article in a fresh Edge window: the full article body, references, and category list
all came through correctly where the old code returned only browser chrome, with no
truncation marker hit either way. Also added the same `MAX_NODES` safety cap (with no
depth limit — a real match can legitimately be very deep) to `_find_elements`, used by
`click_element`/`type_into_element`, which had no bound of any kind before this.

## Known limitations

- Windows only (uses `pywinauto`'s UIA backend). No macOS/Linux equivalent yet — would
  need the Accessibility API (`AXUIElement`) on macOS instead.
- Depends on the target app actually implementing accessibility support. Well-built
  Electron/Chromium apps (confirmed: CCD itself) expose a rich tree; a poorly-built or
  custom-canvas-rendered app may expose little beyond generic panes, in which case
  screenshots remain the fallback.
- **An empty tree means "not exposed", not "empty" — and the two look identical.**
  Custom-drawn/owner-drawn surfaces (canvases, diagram and arrangement views, some custom
  navigation lists) are painted as pixels with no UI Automation representation at all, so
  `read_window` succeeds and describes almost nothing, and `find_in_window` returns "no
  matches" for text that is plainly visible on screen. Reading that as a negative finding
  is the most expensive mistake this tool can cause, so since 2026-09-11 both tools say so
  explicitly when a visually substantial window exposes nothing beyond window chrome. One
  window can mix both kinds: standard controls elsewhere in it may still read perfectly.
  Read the window twice before concluding — a transient sparse read has been observed once.
- Control names are whitespace-normalised (newlines collapsed to spaces) for both display
  and matching, so the name a read tool prints is the name the click/type tools accept.
  Before 2026-09-11 these disagreed, and a wrapped label like `"...keyboard and
  mouse\n(make this computer the server)"` displayed one way and matched only another.
- **No permission tiering.** The built-in computer-use tool restricts what it'll click/
  type per app category (e.g. browsers are click-blocked). This tool has no equivalent
  gating — if it's registered and callable, it can click or type into *any* window on
  the machine, including ones a more cautious policy would normally restrict. Be
  deliberate about when/whether this is registered for a session that also has broader
  autonomy.
- Click/type require an exact name match by default specifically because a wrong guess
  here has real side effects (unlike the read-only tools, which are safe by
  construction) — don't lower `exact_match` or blindly pick an `index` from an ambiguous
  list without confirming which element you actually want first.

## License

[MIT](LICENSE)
