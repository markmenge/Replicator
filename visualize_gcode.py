#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Tuple


def parse_gcode(path: Path) -> dict[str, Any]:
    pos_abs = True  # G90/G91
    ext_abs = True  # M82/M83
    x = y = z = e = a = 0.0
    last_e = 0.0
    layer = 0
    layer_by_z: dict[float, int] = {}
    segments: list[tuple[float, float, float, float, float, int, bool, float | None]] = []
    # (x0,y0,z0,x1,y1,z1,layer,is_extrusion,nozzle_temp)

    nozzle_temps: list[float] = []
    bed_temps: list[float] = []
    first_blocking_heat_line: int | None = None
    first_extrusion_line: int | None = None
    current_nozzle_temp: float | None = None
    nozzle_events: list[tuple[int, str, float]] = []  # (line, cmd, target)
    bed_events: list[tuple[int, str, float]] = []     # (line, cmd, target)
    progress_events: list[tuple[int, int]] = []       # (line, percent)

    def current_layer_for_z(zval: float) -> int:
        # Use ;LAYER:N when available else bucket by Z with small tolerance
        zs = round(zval, 3)
        if zs in layer_by_z:
            return layer_by_z[zs]
        nonlocal layer
        layer_by_z[zs] = layer
        return layer

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        # Diagnostics accumulators
        e_abs_mode_initial: bool | None = None
        m82_lines: list[int] = []
        m83_lines: list[int] = []
        first_e_line: int | None = None
        first_a_line: int | None = None
        extr_segments = 0
        travel_segments = 0
        total_path_extr_mm = 0.0
        total_path_travel_mm = 0.0
        total_e_mm = 0.0
        total_a_mm = 0.0
        retracts = 0
        e_eps = 1e-4
        for ln, raw in enumerate(fh, start=1):
            line = raw.rstrip("\r\n")
            code = line.split(";", 1)[0].strip()
            if not code:
                # parse layer hint comments too
                if line.startswith(";LAYER:"):
                    try:
                        layer = int(line.split(":", 1)[1].strip())
                    except Exception:
                        pass
                continue

            parts = code.split()
            cmd = parts[0].upper()
            # Handle axis zeroing without treating as extrusion
            if cmd == "G92":
                # Reset axes (commonly E). Only update stored values; don't create segments.
                for p in parts[1:]:
                    if len(p) < 2:
                        continue
                    axis = p[0].upper()
                    try:
                        val = float(p[1:])
                    except ValueError:
                        continue
                    if axis == "E":
                        e = val
                    elif axis == "A":
                        a = val
                continue

            if cmd == "G90":
                pos_abs = True
            elif cmd == "G91":
                pos_abs = False
            elif cmd == "M82":
                ext_abs = True
                if e_abs_mode_initial is None:
                    e_abs_mode_initial = True
                m82_lines.append(ln)
            elif cmd == "M83":
                ext_abs = False
                if e_abs_mode_initial is None:
                    e_abs_mode_initial = False
                m83_lines.append(ln)
            elif cmd in ("M104", "M109"):
                # Nozzle temps
                target = _param(parts[1:], {"S", "R"})
                if target is not None:
                    nozzle_temps.append(target)
                    current_nozzle_temp = target
                    nozzle_events.append((ln, cmd, target))
                    if cmd == "M109" and target >= 170 and first_blocking_heat_line is None:
                        first_blocking_heat_line = ln
            elif cmd in ("M140", "M190"):
                target = _param(parts[1:], {"S", "R"})
                if target is not None:
                    bed_temps.append(target)
                    bed_events.append((ln, cmd, target))
            elif cmd in ("M73",):
                pct = _param(parts[1:], {"P"})
                if pct is not None:
                    try:
                        progress_events.append((ln, int(round(pct))))
                    except Exception:
                        pass

            if cmd not in ("G0", "G1"):
                continue

            nx, ny, nz, ne, na = x, y, z, e, a
            had_e = False
            had_a = False
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
                    if first_e_line is None:
                        first_e_line = ln
                    had_e = True
                elif axis == "A":
                    na = (val if ext_abs else a + val)
                    if first_a_line is None:
                        first_a_line = ln
                    had_a = True

            is_extrusion = False
            # Determine extrusion by E delta (with tolerance)
            if ext_abs:
                de = (ne - e) if had_e else 0.0
                da = (na - a) if had_a else 0.0
            else:
                de = (ne - e) if had_e else 0.0  # since ne already e+delta when had_e; else 0
                da = (na - a) if had_a else 0.0
            combined = de + da
            is_extrusion = combined > e_eps

            if is_extrusion and first_extrusion_line is None:
                first_extrusion_line = ln

            # Record segment only on XY movement
            if (nx != x) or (ny != y) or (nz != z):
                seg_layer = layer if line.startswith(";LAYER:") else current_layer_for_z(nz)
                segments.append((x, y, z, nx, ny, nz, seg_layer, is_extrusion, current_nozzle_temp))
                # track path stats (XY only)
                dxy = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
                if is_extrusion:
                    extr_segments += 1
                    total_path_extr_mm += dxy
                else:
                    travel_segments += 1
                    total_path_travel_mm += dxy
            # accumulate extrusion amounts
            if de > e_eps:
                total_e_mm += de
            elif de < -e_eps:
                retracts += 1
            if da > e_eps:
                total_a_mm += da
            elif da < -e_eps:
                retracts += 1

            x, y, z, e, a = nx, ny, nz, ne, na

    return {
        "segments": segments,
        "nozzle_temps": nozzle_temps,
        "bed_temps": bed_temps,
        "first_blocking_heat_line": first_blocking_heat_line,
        "first_extrusion_line": first_extrusion_line,
        "nozzle_events": nozzle_events,
        "bed_events": bed_events,
        "progress_events": progress_events,
        "extruder_diag": {
            "initial_mode_absolute": e_abs_mode_initial,
            "m82_count": len(m82_lines),
            "m83_count": len(m83_lines),
            "first_e_line": first_e_line,
            "first_a_line": first_a_line,
            "extr_segments": extr_segments,
            "travel_segments": travel_segments,
            "path_extr_mm": round(total_path_extr_mm, 1),
            "path_travel_mm": round(total_path_travel_mm, 1),
            "total_e_mm": round(total_e_mm, 3),
            "total_a_mm": round(total_a_mm, 3),
            "retracts": retracts,
            "eps": e_eps,
        },
    }


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


def render_plot(data: dict[str, Any], *, out: Path | None, view3d: bool, size: int) -> None:
    try:
        import matplotlib.pyplot as plt
        from matplotlib.collections import LineCollection
        from matplotlib import cm, colors
        from mpl_toolkits.mplot3d.art3d import Line3DCollection  # type: ignore
    except Exception as e:
        print("ERROR: matplotlib is required. Install with: pip install matplotlib", file=sys.stderr)
        raise SystemExit(1) from e

    segs = data["segments"]
    # Build a colormap over observed nozzle temps
    temp_values = [seg[8] for seg in segs if seg[8] is not None]
    tmin = min(temp_values) if temp_values else 0.0
    tmax = max(temp_values) if temp_values else 1.0
    if tmax == tmin:
        tmax = tmin + 1.0
    norm = colors.Normalize(vmin=tmin, vmax=tmax)
    cmap = cm.plasma

    def color_for(temp: float | None, extr: bool) -> tuple[float, float, float, float]:
        # Non-extruding moves always gray
        if not extr:
            return (0.6, 0.6, 0.6, 0.45)
        # Extrusions colored by current nozzle temp
        if temp is None:
            return (0.3, 0.8, 0.9, 0.9)
        rgba = cmap(norm(temp))
        return (rgba[0], rgba[1], rgba[2], 0.9)

    if view3d:
        fig = plt.figure(figsize=(8, 6))
        ax = fig.add_subplot(111, projection='3d')
        lines = []
        colors = []
        for x0, y0, z0, x1, y1, z1, layer, extr, temp in segs:
            lines.append([(x0, y0, z0), (x1, y1, z1)])
            colors.append(color_for(temp, extr))
        lc = Line3DCollection(lines, colors=colors, linewidths=0.6)
        ax.add_collection3d(lc)
        # Fixed 0..size cube
        ax.set_xlim(0, size)
        ax.set_ylim(0, size)
        ax.set_zlim(0, size)
        ax.set_xlabel('X (mm)')
        ax.set_ylabel('Y (mm)')
        ax.set_zlabel('Z (mm)')
        ax.view_init(elev=30, azim=-60)
    else:
        fig, ax = plt.subplots(figsize=(8, 6))
        lines2d = []
        colors = []
        for x0, y0, _z0, x1, y1, _z1, layer, extr, temp in segs:
            lines2d.append([(x0, y0), (x1, y1)])
            colors.append(color_for(temp, extr))
        lc = LineCollection(lines2d, colors=colors, linewidths=0.6)
        ax.add_collection(lc)
        # Fixed 0..size square
        ax.set_xlim(0, size)
        ax.set_ylim(0, size)
        ax.set_xlabel('X (mm)')
        ax.set_ylabel('Y (mm)')
        ax.set_aspect('equal', adjustable='box')
        ax.grid(True, alpha=0.2)

    mins = _safe_min(data.get("nozzle_temps", []))
    maxs = _safe_max(data.get("nozzle_temps", []))
    note = f"Nozzle temps: {mins if mins is not None else '-'} to {maxs if maxs is not None else '-'} C"
    fig.suptitle(note)

    # Add a colorbar for nozzle temp
    if temp_values:
        import matplotlib.pyplot as plt  # for ScalarMappable
        from matplotlib import cm, colors as _colors
        sm = cm.ScalarMappable(cmap=cmap, norm=norm)
        sm.set_array([])
        cbar = fig.colorbar(sm, ax=(ax if view3d else ax), pad=0.02)
        cbar.set_label('Nozzle temp (C)')

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=150, bbox_inches='tight')
        print(f"Saved preview: {out}")
    else:
        plt.show()


def _autoscale2d(ax, lines: list[list[tuple[float, float]]]) -> None:
    xs = [p[0] for seg in lines for p in seg]
    ys = [p[1] for seg in lines for p in seg]
    if not xs or not ys:
        return
    ax.set_xlim(min(xs), max(xs))
    ax.set_ylim(min(ys), max(ys))


def _autoscale3d(ax, lines: list[list[tuple[float, float, float]]]) -> None:
    xs = [p[0] for seg in lines for p in seg]
    ys = [p[1] for seg in lines for p in seg]
    zs = [p[2] for seg in lines for p in seg]
    if not xs or not ys or not zs:
        return
    ax.set_xlim(min(xs), max(xs))
    ax.set_ylim(min(ys), max(ys))
    ax.set_zlim(min(zs), max(zs))


def main() -> int:
    ap = argparse.ArgumentParser(description="Visualize a G-code toolpath (2D/3D)")
    ap.add_argument("gcode", help="Path to .gcode file")
    ap.add_argument("--out", default=None, help="Optional output image path (PNG)")
    ap.add_argument("--view", choices=["2d", "3d"], default="2d", help="Plot style: 2d or 3d")
    ap.add_argument("--size", type=int, default=256, help="Fixed axis size (mm) for X/Y/Z (default 256)")
    args = ap.parse_args()

    gpath = Path(args.gcode).resolve()
    if not gpath.exists():
        print(f"ERROR: file not found: {gpath}")
        return 1

    data = parse_gcode(gpath)
    if data["first_extrusion_line"] is not None and (
        data["first_blocking_heat_line"] is None or data["first_extrusion_line"] < data["first_blocking_heat_line"]
    ):
        print(
            f"WARNING: first extrusion at line {data['first_extrusion_line']} occurs before blocking M109 at "
            f"{data['first_blocking_heat_line'] or '-'}"
        )
    print(
        f"Nozzle temps observed: {data['nozzle_temps'] or 'none'}; "
        f"Bed temps: {data['bed_temps'] or 'none'}"
    )

    # Print concise event previews
    nz_ev = data.get("nozzle_events", []) or []
    bd_ev = data.get("bed_events", []) or []
    pr_ev = data.get("progress_events", []) or []

    def _fmt_setpoints(ev: list[tuple[int, str, float]], max_items: int = 6) -> str:
        if not ev:
            return "-"
        head = ", ".join([f"L{ln}:{cmd} {int(val)}" for (ln, cmd, val) in ev[:max_items]])
        if len(ev) > max_items:
            head += f" (+{len(ev)-max_items} more)"
        return head

    def _fmt_progress(ev: list[tuple[int, int]], max_items: int = 8) -> str:
        if not ev:
            return "-"
        head = ", ".join([f"L{ln}:P{pct}%" for (ln, pct) in ev[:max_items]])
        if len(ev) > max_items:
            head += f" (+{len(ev)-max_items} more)"
        return head

    print(f"Nozzle setpoints: {_fmt_setpoints(nz_ev)}")
    print(f"Bed setpoints:    {_fmt_setpoints(bd_ev)}")
    if pr_ev:
        print(f"Progress (M73): {_fmt_progress(pr_ev)}")

    # Extruder diagnostics
    diag = data.get("extruder_diag", {}) or {}
    if diag:
        mode = "absolute" if diag.get("initial_mode_absolute") else ("relative" if diag.get("initial_mode_absolute") is not None else "unknown")
        print(
            f"Extruder diag: mode={mode}, M82={diag.get('m82_count',0)}, M83={diag.get('m83_count',0)}, "
            f"first E at L{diag.get('first_e_line','-')}, first A at L{diag.get('first_a_line','-')}"
        )
        print(
            f"  segments: extr={diag.get('extr_segments',0)}, travel={diag.get('travel_segments',0)} | "
            f"path mm: extr={diag.get('path_extr_mm',0.0)}, travel={diag.get('path_travel_mm',0.0)}"
        )
        print(
            f"  extruded: E={diag.get('total_e_mm',0.0)} mm, A={diag.get('total_a_mm',0.0)} mm | retractions={diag.get('retracts',0)} (eps={diag.get('eps')})"
        )

    out = Path(args.out) if args.out else None
    render_plot(data, out=out, view3d=(args.view == "3d"), size=args.size)
    return 0


def _safe_min(vals: list[float]) -> float | None:
    return min(vals) if vals else None


def _safe_max(vals: list[float]) -> float | None:
    return max(vals) if vals else None


if __name__ == "__main__":
    raise SystemExit(main())
