# Replicator Workspace Notes

This project allows users to simply describe an object they want, and then a short time later it will appear in the 3d printer.


## Recommended Starting Point

- `replicator.py`
  Tkinter desktop app for prompt/voice-to-OpenSCAD-to-printer. It saves settings to `replicator.json`, supports Whisper voice input, renders OpenSCAD previews, can visualize G-code in 3D, and can run full slice/upload/print through `print_scad.py`.

## Python Scripts

- `upload_gcode.py`
  Current safest entry point for local file upload. It uploads a file to the printer's HTTP endpoint and now includes local G-code preflight checks, a summary, and an Enter-to-continue confirmation before actual upload.

- `print_file.py`
  Starts a print for a printer-side file that has already been uploaded to the Centauri printer.

- `print_scad.py`
  Best-effort SCAD pipeline that exports STL via OpenSCAD, slices with OrcaSlicer, verifies thumbnail presence in the resulting G-code, and then uploads via `upload_gcode.py`. It auto-detects active machine/process/filament defaults from `OrcaSlicer.conf` and resolves them to preset JSON files.

- `upload_gcode.py`
  Uploads a local file to `http://<host>/uploadFile/upload` with multipart form data. Also performs local G-code checks for homing, temperature commands, motion ranges, extrusion sequencing, and non-standard commands. Supports `--check-only` to validate without uploading.

- `centauri_probe.py`
  WebSocket-based probe script for the Centauri printer on port `3030`. Can request attributes, status, file lists, file details, and attempt a start-print command for a printer-side file. Best used for protocol exploration rather than routine printing.

- `print_file.py`
  Starts a print for a file that already exists on the printer. It attempts short LAN discovery to learn the printer's mainboard ID, checks current status when possible, and then sends `start_print`.

- `print_scad.py`
  Pipeline script for `SCAD -> STL -> G-code -> upload`. It calls OpenSCAD to export STL, attempts to call OrcaSlicer with a likely CLI shape, verifies whether the produced G-code contains an embedded thumbnail, and uploads the result through `upload_gcode.py`. Because OrcaSlicer CLI behavior is not fully verified here, this script is best treated as best-effort automation rather than confirmed production flow.
  - Extra flags:
    - `--upload-url`: override printer upload endpoint (useful for simulator, e.g. `http://127.0.0.1:8080/uploadFile/upload`)
    - `--sim-stub-slicer`: skip Orca and emit a tiny, safe G-code with an embedded thumbnail
    - `--ws-port`: override printer WebSocket port for `pycentauri` (sets `PYCENTAURI_WS_PORT`)
    - `--ensure-heat-order`: insert blocking `M190/M109` before first extrusion if missing/late
    - `--ensure-prime-strip`: add a two-line bed prime strip if not detected pre-layer

- `replicator.py`
  Main GUI app. Use menus (`File`, `Settings`) and the main prompt/checkbox controls instead of CLI flags.
  - Core features:
    - Prompt or Whisper voice input
    - Optional preview image (`Show Preview`)
    - Optional 3D G-code visualization (`Visualize G-code in 3D before print`)
    - Optional upload/start (`Print`)
    - Persistent settings in `replicator.json`

## Is This Project Name-Plate Only?

No. Name plates are one supported workflow, but the project is a general natural-language-to-print pipeline.

- Good targets:
  - Name plates and badges
  - Board-game pieces and tokens
  - Small functional parts (clips, holders, spacers)
  - Simple decorative models

`replicator.py` now includes extra prompt guidance for board-game token style requests (for example Monopoly-like tokens), not just name-plate prompts.

## Monopoly Tokens / Board-Game Pieces

Yes, this is now possible from the app.

Recommended flow:

1. Open `replicator.py`.
2. Enter a prompt such as:
   - `Create a Monopoly-style top hat token, 20mm wide, 3.5mm thick, no supports.`
   - `Make a small race-car board game token, 22mm long, flat base, printable without supports.`
3. Enable `Show Preview`.
4. Enable `Visualize G-code in 3D before print`.
5. Enable `Print` only when the preview and toolpath look correct.

Prompt tips for tokens:

- Include size in mm (`18-24mm` wide is a good starting range).
- Say `no supports` and `flat base`.
- Ask for `thicker features` and `no fragile details`.
- For raised/debossed symbols, request depth around `0.8mm`.

## Project Structure (in progress)

- `research/` — reverse-engineering utilities and artifacts:
  - `research/scan_printer_js.py`
  - `archive/` (legacy assets remain here for now)
- `build/` — generated STL, G-code, and previews
- Root scripts — printing pipeline and utilities remain at the top level for now

## Troubleshooting: Heat Order, Priming, and Visualization


