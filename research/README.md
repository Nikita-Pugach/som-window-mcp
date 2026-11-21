# research

Two throwaway probes, kept because they produced the numbers in
[`../docs/uia-matrix.md`](../docs/uia-matrix.md) and because they are the
smallest possible reproduction of the two things this project depends on:

- `probe.py` — can `PrintWindow` capture a window that is covered by another
  window, and where exactly does the DWM border offset sit?
- `uia_probe.py` — how big and how fast is the UI Automation tree of a given
  app, and does an elevated target return an empty tree?

Both are standalone: `python research/probe.py --match chrome`. They are
superseded by the package in `src/` and are not imported by it.
