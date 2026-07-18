#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass
class PrimeDetection:
    present: bool
    orientation: str | None  # 'horizontal' or 'vertical'
    y_band: float | None
    x_band: float | None
    length_mm: float
    z: float | None


def _param(tokens: Iterable[str], names: set[str]) -> float | None:
    for t in tokens:
        if not t:
            continue
        k = t[0].upper()
        if k in names and len(t) > 1:
            try:
                return float(t[1:])
            except ValueError:
                pass
    return None


def summarize_temps(gcode: list[str]) -> tuple[list[float], list[float]]:
    noz: list[float] = []
    bed: list[float] = []
    for raw in gcode:
        code = raw.split(";", 1)[0].strip().upper()
        if not code:
            continue
        parts = code.split()
        if parts[0] in ("M104", "M109"):
            v = _param(parts[1:], {"S", "R"})
            if v is not None:
                noz.append(v)
        elif parts[0] in ("M140", "M190"):
            v = _param(parts[1:], {"S", "R"})
            if v is not None:
                bed.append(v)
    return noz, bed


def detect_prime_strip(gcode: list[str]) -> PrimeDetection:
    # Fast heuristic: look at early extrusions near a bed edge and check for a long straight line.
    pos_abs = True
    ext_abs = True
    x = y = z = e = a = 0.0

    def seg_len(p0: tuple[float, float], p1: tuple[float, float]) -> float:
        return ((p1[0]-p0[0])**2 + (p1[1]-p0[1])**2) ** 0.5

    horiz_buckets: dict[int, float] = {}
    vert_buckets: dict[int, float] = {}
    bucket_eps = 1.0  # mm banding
    max_lines = 1500  # only need the start
    min_x_obs: float | None = None
    min_y_obs: float | None = None

    reached_first_layer = False
    prelayer_segments: list[tuple[float, float, float, float, bool]] = []  # (x0,y0,x1,y1,extr)
    for i, raw in enumerate(gcode, start=1):
        if i > max_lines:
            break
        code = raw.split(";", 1)[0].strip()
        if not code:
            continue
        # If we see a layer marker, stop prime accumulation (prime occurs before layer 0)
        if raw.lstrip().startswith(";LAYER:"):
            reached_first_layer = True
            break
        parts = code.split()
        cmd = parts[0].upper()
        if cmd == "G90":
            pos_abs = True
            continue
        if cmd == "G91":
            pos_abs = False
            continue
        if cmd == "M82":
            ext_abs = True
            continue
        if cmd == "M83":
            ext_abs = False
            continue

        if cmd not in ("G0", "G1"):
            continue

        nx, ny, nz, ne, na = x, y, z, e, a
        for p in parts[1:]:
            if len(p) < 2:
                continue
            axis = p[0].upper()
            try:
                val = float(p[1:])
            except ValueError:
                continue
            if axis == "X":
                nx = (val if pos_abs else x + val)
            elif axis == "Y":
                ny = (val if pos_abs else y + val)
            elif axis == "Z":
                nz = (val if pos_abs else z + val)
            elif axis == "E":
                ne = (val if ext_abs else e + val)
            elif axis == "A":
                na = (val if ext_abs else a + val)

        is_extrusion = ((ne - e + na - a) > 1e-6) if ext_abs else ((ne + na) > 1e-6)
        if is_extrusion:
            # Track observed minimums for near-edge comparison
            min_x_obs = nx if min_x_obs is None else min(min_x_obs, nx, x)
            min_y_obs = ny if min_y_obs is None else min(min_y_obs, ny, y)
            # Horizontal band: same Y within 1 mm, accumulate X distance
            y_key = int(round(ny / bucket_eps))
            horiz_buckets[y_key] = horiz_buckets.get(y_key, 0.0) + seg_len((x, y), (nx, ny))
            # Vertical band: same X within 1 mm, accumulate Y distance
            x_key = int(round(nx / bucket_eps))
            vert_buckets[x_key] = vert_buckets.get(x_key, 0.0) + seg_len((x, y), (nx, ny))

        # Keep pre-layer move
        prelayer_segments.append((x, y, nx, ny, is_extrusion))
        x, y, z, e, a = nx, ny, nz, ne, na

    # Find the longest band near an edge (<= 10 mm) and long enough (>= 60 mm)
    best_h = max(horiz_buckets.items(), key=lambda kv: kv[1]) if horiz_buckets else (None, 0.0)
    best_v = max(vert_buckets.items(), key=lambda kv: kv[1]) if vert_buckets else (None, 0.0)

    # Convert band index back to coordinate
    y_band = (best_h[0] if best_h[0] is not None else None)
    if y_band is not None:
        y_band = float(y_band)
    x_band = (best_v[0] if best_v[0] is not None else None)
    if x_band is not None:
        x_band = float(x_band)

    # Pattern-based check: look for two long, nearly-horizontal consecutive extrusions with small Y gap
    def _is_long(line: tuple[float,float,float,float,bool]) -> tuple[bool,float,float]:
        x0,y0,x1,y1,ext = line
        length = ((x1-x0)**2 + (y1-y0)**2)**0.5
        return (ext and length >= 120.0 and abs(y1-y0) <= 1.0), length, (y0+y1)/2.0

    for idx in range(len(prelayer_segments)-1):
        ok1, l1, yb1 = _is_long(prelayer_segments[idx])
        ok2, l2, yb2 = _is_long(prelayer_segments[idx+1])
        if ok1 and ok2 and abs(yb2 - yb1) <= 3.0:
            # found classic two-line prime
            return PrimeDetection(True, 'horizontal', (yb1+yb2)/2.0, None, l1+l2, None)

    # Heuristics (band accumulation as fallback)
    h_len = best_h[1]
    v_len = best_v[1]

    # Consider near the minimum observed edge in the start window
    y_edge_ref = min_y_obs if min_y_obs is not None else 0.0
    x_edge_ref = min_x_obs if min_x_obs is not None else 0.0
    edge_tol = 8.0
    h_edge = (y_band is not None and abs(y_band - y_edge_ref) <= edge_tol)
    v_edge = (x_band is not None and abs(x_band - x_edge_ref) <= edge_tol)

    # Accept long band anywhere (>=80mm), or shorter if it's near an edge (>=60mm)
    if (h_len >= 80.0) or (h_edge and h_len >= 60.0 and h_len >= v_len):
        return PrimeDetection(True, 'horizontal', y_band, None, h_len, None)
    if (v_len >= 80.0) or (v_edge and v_len >= 60.0):
        return PrimeDetection(True, 'vertical', None, x_band, v_len, None)
    return PrimeDetection(False, None, None, None, 0.0, None)


def inject_prime_strip(src_lines: list[str]) -> list[str]:
    # Insert after last M109 (blocking hotend heat) if present, else after first G28
    insert_idx = None
    last_m109 = None
    first_g28 = None
    for i, raw in enumerate(src_lines):
        code = raw.split(";", 1)[0].strip().upper()
        if code.startswith("M109 ") or code == "M109":
            last_m109 = i
        if first_g28 is None and (code.startswith("G28 ") or code == "G28"):
            first_g28 = i
    if last_m109 is not None:
        insert_idx = last_m109 + 1
    elif first_g28 is not None:
        insert_idx = first_g28 + 1
    else:
        insert_idx = 0

    # Use relative extrusion for the prime, restore absolute if file used it later
    uses_absolute_extrusion = any("M82" in (ln.split(";",1)[0].upper()) for ln in src_lines[:1000])
    prime = [
        "; PRIME_STRIP_START",
        "M83 ; relative extrusion",
        "G92 E0",
        "G1 Z0.28 F1200",
        "G1 X5 Y5 F6000",
        "G1 X200 Y5 E12 F1200",
        "G1 X200 Y7 F6000",
        "G1 X5 Y7 E12 F1200",
        "G92 E0",
    ]
    if uses_absolute_extrusion:
        prime.append("M82 ; back to absolute extrusion")
    prime.append("; PRIME_STRIP_END")

    out = src_lines[:insert_idx] + [ln + ("\n" if not ln.endswith("\n") else "") for ln in prime] + src_lines[insert_idx:]
    return out


def _fix_blocking_heat(src_lines: list[str]) -> tuple[bool, list[str]]:
    # Detect first extrusion line and available heat targets
    pos_abs = True
    ext_abs = True
    x = y = z = e = a = 0.0
    first_extrusion_line: int | None = None
    first_m109_line: int | None = None
    first_m190_line: int | None = None
    first_m104_line: int | None = None
    first_m140_line: int | None = None
    nozzle_target: float | None = None
    bed_target: float | None = None

    for i, raw in enumerate(src_lines, start=1):
        code = raw.split(";", 1)[0].strip()
        up = code.upper()
        if up.startswith("G90"):
            pos_abs = True
        elif up.startswith("G91"):
            pos_abs = False
        elif up.startswith("M82"):
            ext_abs = True
        elif up.startswith("M83"):
            ext_abs = False

        if up.startswith("M109"):
            if first_m109_line is None:
                first_m109_line = i
            if nozzle_target is None:
                t = _param(up.split()[1:], {"S", "R"})
                if t is not None:
                    nozzle_target = t
        elif up.startswith("M104"):
            if first_m104_line is None:
                first_m104_line = i
            if nozzle_target is None:
                t = _param(up.split()[1:], {"S", "R"})
                if t is not None:
                    nozzle_target = t
        elif up.startswith("M190"):
            if first_m190_line is None:
                first_m190_line = i
            if bed_target is None:
                t = _param(up.split()[1:], {"S", "R"})
                if t is not None:
                    bed_target = t
        elif up.startswith("M140"):
            if first_m140_line is None:
                first_m140_line = i
            if bed_target is None:
                t = _param(up.split()[1:], {"S", "R"})
                if t is not None:
                    bed_target = t

        if up.startswith("G0") or up.startswith("G1"):
            nx, ny, nz, ne, na = x, y, z, e, a
            parts = up.split()[1:]
            for p in parts:
                if len(p) < 2:
                    continue
                axis = p[0].upper()
                try:
                    val = float(p[1:])
                except ValueError:
                    continue
                if axis == "X":
                    nx = (val if pos_abs else x + val)
                elif axis == "Y":
                    ny = (val if pos_abs else y + val)
                elif axis == "Z":
                    nz = (val if pos_abs else z + val)
                elif axis == "E":
                    ne = (val if ext_abs else e + val)
                elif axis == "A":
                    na = (val if ext_abs else a + val)
            is_extrusion = ((ne - e + na - a) > 1e-6) if ext_abs else ((ne + na) > 1e-6)
            if is_extrusion and first_extrusion_line is None:
                first_extrusion_line = i
            x, y, z, e, a = nx, ny, nz, ne, na

    if first_extrusion_line is None:
        return False, src_lines

    need_nozzle_block = (first_m109_line is None) or (first_m109_line > first_extrusion_line)
    need_bed_block = (first_m190_line is None) or (first_m190_line > first_extrusion_line)

    if not need_nozzle_block and not need_bed_block:
        return False, src_lines

    # Choose fallback targets if not in file
    if nozzle_target is None:
        nozzle_target = 200.0
    if bed_target is None:
        bed_target = 60.0

    insert_at = first_extrusion_line - 1  # insert before first extrusion
    patch: list[str] = ["; HEAT_FIX_START\n"]
    if need_bed_block:
        patch.append(f"M190 S{bed_target:.0f} ; ensure bed at temp before extrusion\n")
    if need_nozzle_block:
        patch.append(f"M109 S{nozzle_target:.0f} ; ensure nozzle at temp before extrusion\n")
    patch.append("; HEAT_FIX_END\n")

    out = src_lines[:insert_at] + patch + src_lines[insert_at:]
    return True, out


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect or inject a priming strip in a G-code file; optionally fix blocking heat order")
    ap.add_argument("gcode", help="Path to .gcode")
    ap.add_argument("--inject", action="store_true", help="Inject a priming strip if not detected")
    ap.add_argument("--out", default=None, help="Write modified G-code to this path (required with --inject)")
    ap.add_argument("--fix-heat", action="store_true", help="Ensure blocking M190/M109 occur before first extrusion; insert if missing or late")
    args = ap.parse_args()

    gpath = Path(args.gcode).resolve()
    if not gpath.exists():
        print(f"ERROR: file not found: {gpath}")
        return 1

    lines = gpath.read_text(encoding="utf-8", errors="replace").splitlines()
    noz, bed = summarize_temps(lines)
    det = detect_prime_strip(lines)
    print(f"Nozzle temps: {noz or 'none'}; Bed temps: {bed or 'none'}")

    if args.fix_heat:
        fixed, new_lines = _fix_blocking_heat(lines)
        if fixed:
            if not args.out:
                print("ERROR: --out is required with --fix-heat")
                return 2
            Path(args.out).write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            print(f"Inserted/relocated blocking heat -> {args.out}")
            # If only fixing heat was requested, exit here.
            if not args.inject:
                return 0
            # Continue with priming detection/injection on the modified content.
            lines = new_lines
            det = detect_prime_strip(lines)
    if det.present:
        print(f"Priming strip detected: {det.orientation}, length ~{det.length_mm:.1f} mm, band X={det.x_band} Y={det.y_band}")
        return 0

    print("Priming strip NOT detected.")
    if not args.inject:
        return 2
    if not args.out:
        print("ERROR: --out is required with --inject")
        return 2

    modified = inject_prime_strip(lines)
    out_path = Path(args.out).resolve()
    out_path.write_text("\n".join(modified) + "\n", encoding="utf-8")
    print(f"Injected prime strip -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
