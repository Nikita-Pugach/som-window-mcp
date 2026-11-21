# UIA measurements (step 0.5)

Measured on Windows 11 (Russian locale), Python 3.12, `uiautomation` 2.0.29,
per-monitor DPI awareness V2, non-elevated process.

Method: `research/uia_probe.py` walks the UI Automation tree from the window root using
`GetChildren()`, depth-limited, and reports node counts and wall time. "interact"
counts only control types that are worth marking for an agent (Button, Edit,
TabItem, MenuItem, ListItem, CheckBox, ComboBox, TreeItem, Hyperlink, ...).

| Window | Stack | Depth | Nodes | Interact | Named | Time |
|---|---|---|---|---|---|---|
| Notepad | WinUI 3 | 4 | 27 | 15 | 24 | 23–29 ms |
| Calculator | UWP / WinUI 3 | 4 | 27 | 8 | 22 | 30–42 ms |
| Calculator | UWP / WinUI 3 | 7 | 89 | 37 | 84 | 112–135 ms |
| Explorer (System32) | Composition | 7 | 99 | 54 | 80 | 248–315 ms |
| Chrome, no flag | Chromium | 4 | 8 | 0 | 2 | 15 ms |
| Chrome, AX shell only | Chromium | 6 | 55 | 7 | 11 | 51–55 ms |
| Chrome, `--force-renderer-accessibility` | Chromium | 8 | 173 | 44 | 41 | 182 ms |
| AIDA64 (elevated) | Delphi | 7 | 2 | 0 | 1 | 3–10 ms |
| Desktop (`Program Manager`) | — | 4 | 2 | 0 | 1 | 5 ms |

## Conclusions

**1. `max_depth=3` is wrong; the default must be 6.**
Calculator exposes 8 interactive controls at depth 4 and 37 at depth 7 — the
keypad simply does not appear earlier. Any fixed shallow limit silently
produces a UI that looks empty.

**2. Cost is ~2.5 ms per node across processes.**
Explorer: 99 nodes in 248–315 ms. A 4000-node tree would take ~10 s, which no
agent will tolerate. A node budget (default 400–600) plus a subprocess timeout
is mandatory, and `CacheRequest` batching is the obvious optimisation.

**3. UIPI failure looks exactly like an empty window: 2 nodes, no error.**
AIDA64 runs elevated by default; from a non-elevated process the tree is
`Window` + `TitleBar` and nothing else, and no exception is raised. Returning
that to an agent would make it conclude "the window has no buttons". The tool
must report `elevated: true` and explain the permission problem instead.

**4. Chromium and Electron expose nothing by default.**
Chrome without a flag: 8 nodes, 0 interactive, forever (polled for 9 s). Merely
touching UIA later raises the shell to 55 nodes, but the page DOM still never
appears. With `--force-renderer-accessibility` the same window yields 173 nodes
including `Document` and `Text`.

Consequence: for Chrome, VS Code, Slack, Discord, Cursor and every other
Chromium app, the UIA path requires relaunching the app with the flag, or it
needs the screenshot fallback. This is why the project must stay hybrid rather
than "UIA only".

**5. Not tested yet.**
Qt (CrystalDiskInfo was closed) and a non-elevated Delphi app, so the
"partial tree" case is still unverified. `ApplicationFrameHost` UWP windows work
fine and expose real `AutomationId`s (`Close`, `negateButton`,
`MemoryButton`).
