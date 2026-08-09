#!/usr/bin/env python3
import argparse
import asyncio
import base64
import os
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from print_file import DEFAULT_MAINBOARD_ID, PrinterError, normalize_printer_filename, prompt_to_continue as prompt_to_start_print, start_print
from upload_gcode import upload_gcode_file


DEFAULT_OPENSCAD = Path(r"C:\Program Files\OpenSCAD\openscad.exe")
DEFAULT_ORCA = Path(r"D:\Program Files\OrcaSlicer\orca-slicer.exe")
DEFAULT_BUILD_DIR = Path(__file__).with_name("build")
DEFAULT_ORCA_CONF = Path(r"C:\Users\Mark\AppData\Roaming\OrcaSlicer\OrcaSlicer.conf")
DEFAULT_ORCA_USER_DIR = Path(r"C:\Users\Mark\AppData\Roaming\OrcaSlicer\user\default")
DEFAULT_ORCA_SYSTEM_DIR = Path(r"C:\Users\Mark\AppData\Roaming\OrcaSlicer\system")
DEFAULT_THUMBNAIL_SIZE = 144

BED_TYPE_LABELS = {
    "1": "Cool Plate",
    "2": "Engineering Plate",
    "3": "Smooth PEI Plate",
    "4": "Textured PEI Plate",
    "5": "High Temp Plate",
}

ORCA_SETTING_LABELS = {
    "machine": "Printer",
    "machine_model": "Printer Model",
    "process": "Process",
    "filament": "Filament",
}

ORCA_LOADABLE_SETTING_TYPES = {"machine", "machine_model", "process", "filament"}


@dataclass(frozen=True)
class OrcaSettingSelection:
    selected_path: Path
    load_paths: tuple[Path, ...]


def parse_orca_inherits(value) -> list[str]:
    if isinstance(value, str):
        separators = [";", "|", ","]
        values = [value]
        for separator in separators:
            if any(separator in item for item in values):
                values = [part for item in values for part in item.split(separator)]
        return [item.strip() for item in values if item.strip()]

    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            names.extend(parse_orca_inherits(item))
        return names

    return []


def dedupe_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return tuple(unique)


def get_orca_setting_type(path: Path, data: dict | None = None) -> str | None:
    if data is None:
        data = read_json_file(path)
    setting_type = data.get("type")
    if isinstance(setting_type, str) and setting_type:
        return setting_type

    path_parts = {part.lower() for part in path.parts}
    if "machine" in path_parts:
        return "machine"
    if "process" in path_parts:
        return "process"
    if "filament" in path_parts:
        return "filament"
    return None


def read_json_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")


def run_command(command: list[str], label: str, dry_run: bool) -> None:
    print(f"{label} command:")
    print("  " + subprocess.list2cmdline(command))
    if dry_run:
        print(f"Dry run enabled; skipping {label.lower()} execution.")
        return

    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.stdout:
        print(completed.stdout)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}")


def preset_namespace_root(path: Path, preset_root: Path) -> Path | None:
    try:
        relative = path.resolve().relative_to(preset_root.resolve())
    except ValueError:
        return None

    parts = relative.parts
    for index, part in enumerate(parts):
        if part.lower() in {"machine", "process", "filament"} and index > 0:
            return preset_root / Path(*parts[:index + 1])
    return None


def find_inherited_orca_setting_file(
    inherited_name: str,
    current_path: Path,
    user_dir: Path,
    system_dir: Path,
) -> Path | None:
    filename = f"{inherited_name}.json"
    candidates: list[Path] = []

    same_folder = current_path.parent / filename
    if same_folder.exists():
        candidates.append(same_folder)

    for preset_root in (user_dir, system_dir):
        namespace_root = preset_namespace_root(current_path, preset_root)
        if namespace_root and namespace_root.exists():
            candidates.extend(sorted(namespace_root.glob(f"**/{filename}")))

    if user_dir.exists():
        candidates.extend(sorted(user_dir.glob(f"**/{filename}")))
    if system_dir.exists():
        candidates.extend(sorted(system_dir.glob(f"**/{filename}")))

    unique = dedupe_paths(candidates)
    return unique[0] if unique else None


def collect_orca_cli_load_files(
    path: Path,
    user_dir: Path,
    system_dir: Path,
    seen: set[Path] | None = None,
) -> tuple[Path, ...]:
    if seen is None:
        seen = set()

    current = path.resolve()
    if current in seen:
        return ()
    seen.add(current)

    data = read_json_file(current)
    load_files: list[Path] = []
    for inherited_name in parse_orca_inherits(data.get("inherits")):
        inherited_path = find_inherited_orca_setting_file(inherited_name, current, user_dir, system_dir)
        if inherited_path is not None:
            load_files.extend(collect_orca_cli_load_files(inherited_path, user_dir, system_dir, seen))

    if data.get("type") in ORCA_LOADABLE_SETTING_TYPES:
        load_files.append(current)
    elif not load_files:
        load_files.append(current)

    return dedupe_paths(load_files)


def resolve_orca_setting_files(candidate: Path, user_dir: Path, system_dir: Path) -> tuple[Path, ...]:
    data = read_json_file(candidate)
    setting_type = get_orca_setting_type(candidate, data)
    if setting_type == "filament":
        return collect_orca_cli_load_files(candidate, user_dir, system_dir)

    if data.get("type") in ORCA_LOADABLE_SETTING_TYPES:
        return (candidate,)

    inherited_files: list[Path] = []
    for inherited_name in parse_orca_inherits(data.get("inherits")):
        inherited_path = find_inherited_orca_setting_file(inherited_name, candidate, user_dir, system_dir)
        if inherited_path is not None:
            inherited_files.extend(resolve_orca_setting_files(inherited_path, user_dir, system_dir))
    if inherited_files:
        return dedupe_paths(inherited_files)

    return (candidate,)


def make_orca_setting_selection(candidate: Path, user_dir: Path, system_dir: Path) -> OrcaSettingSelection:
    return OrcaSettingSelection(
        selected_path=candidate,
        load_paths=resolve_orca_setting_files(candidate, user_dir, system_dir),
    )


def get_orca_ancestor_chain(
    path: Path,
    user_dir: Path,
    system_dir: Path,
    seen: set[Path] | None = None,
) -> list[Path]:
    if seen is None:
        seen = set()

    current = path.resolve()
    if current in seen:
        return []
    seen.add(current)

    data = read_json_file(current)
    ancestors: list[Path] = []
    for inherited_name in parse_orca_inherits(data.get("inherits")):
        inherited_path = find_inherited_orca_setting_file(inherited_name, current, user_dir, system_dir)
        if inherited_path is None:
            continue
        inherited_path = inherited_path.resolve()
        ancestors.append(inherited_path)
        ancestors.extend(get_orca_ancestor_chain(inherited_path, user_dir, system_dir, seen))
    return ancestors


def describe_orca_setting_file(path: Path) -> tuple[str, str, str | None]:
    data = read_json_file(path)
    setting_type = get_orca_setting_type(path, data)

    label = ORCA_SETTING_LABELS.get(str(setting_type), "Setting")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        name = path.stem
    inherited_names = parse_orca_inherits(data.get("inherits"))
    inherits = "; ".join(inherited_names) if inherited_names else None
    return label, name, inherits


def print_orca_setting_selection(setting: OrcaSettingSelection, user_dir: Path, system_dir: Path, indent: str = "  ") -> None:
    label, name, inherits = describe_orca_setting_file(setting.selected_path)
    print(f"{indent}{label}: {name}")
    if inherits:
        print(f"{indent}  inherits: {inherits}")
    print(f"{indent}  selected file: {setting.selected_path}")
    if setting.load_paths != (setting.selected_path,):
        for load_path in setting.load_paths:
            print(f"{indent}  CLI load file: {load_path}")

    ancestors = get_orca_ancestor_chain(setting.selected_path, user_dir, system_dir)
    if ancestors:
        print(f"{indent}  ancestor chain:")
        for index, ancestor in enumerate(ancestors, start=1):
            _ancestor_label, ancestor_name, ancestor_inherits = describe_orca_setting_file(ancestor)
            suffix = f" (inherits: {ancestor_inherits})" if ancestor_inherits else ""
            print(f"{indent}    {index}. {ancestor_name}{suffix}")
            print(f"{indent}       file: {ancestor}")


def safe_setting_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value.strip())
    return safe.strip("._") or "setting"


def write_effective_filament_setting(load_paths: tuple[Path, ...], output_dir: Path) -> Path:
    if not load_paths:
        raise RuntimeError("Cannot build an effective filament setting without input files")

    merged: dict = {}
    for path in load_paths:
        merged.update(read_json_file(path))

    selected = read_json_file(load_paths[-1])
    selected_name = selected.get("name")
    if not isinstance(selected_name, str) or not selected_name.strip():
        selected_name = load_paths[-1].stem

    merged["type"] = "filament"
    merged["name"] = selected_name
    merged.pop("inherits", None)

    output_dir.mkdir(parents=True, exist_ok=True)
    effective_path = output_dir / f"{safe_setting_filename(selected_name)}.effective.filament.json"
    effective_path.write_text(json.dumps(merged, indent=2), encoding="utf-8", newline="\n")
    return effective_path


def find_orca_preset_by_name(name: str, user_dir: Path, system_dir: Path) -> Path | None:
    normalized_name = name.strip()
    if not normalized_name:
        return None

    user_matches = sorted(user_dir.glob(f"**/{normalized_name}.json")) if user_dir.exists() else []
    if user_matches:
        return user_matches[0]

    system_matches = sorted(system_dir.glob(f"**/{normalized_name}.json")) if system_dir.exists() else []
    if system_matches:
        return system_matches[0]

    return None


def resolve_filament_setting_file(value: str, user_dir: Path, system_dir: Path) -> OrcaSettingSelection:
    candidate = Path(value)
    if candidate.exists():
        return make_orca_setting_selection(candidate.resolve(), user_dir, system_dir)

    match = find_orca_preset_by_name(value, user_dir, system_dir)
    if match is None:
        raise FileNotFoundError(f"Filament preset not found by path or name: {value}")
    return make_orca_setting_selection(match.resolve(), user_dir, system_dir)


def read_orca_conf(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    # OrcaSlicer adds a trailing checksum line beginning with '#'.
    cleaned = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def get_active_orca_profile(conf_data: dict) -> dict:
    machine_name = conf_data.get("presets", {}).get("machine")
    profiles = conf_data.get("orca_presets")
    if not isinstance(profiles, list) or not profiles:
        raise RuntimeError("No orca_presets entries in OrcaSlicer.conf")

    if isinstance(machine_name, str) and machine_name:
        for profile in profiles:
            if isinstance(profile, dict) and profile.get("machine") == machine_name:
                return profile

    fallback = profiles[-1]
    if not isinstance(fallback, dict):
        raise RuntimeError("Invalid orca_presets entry in OrcaSlicer.conf")
    return fallback


def auto_detect_orca_setting_files(conf_path: Path, user_dir: Path, system_dir: Path) -> tuple[list[OrcaSettingSelection], str | None]:
    ensure_exists(conf_path, "OrcaSlicer config")
    conf_data = read_orca_conf(conf_path)
    active_profile = get_active_orca_profile(conf_data)

    machine_name = active_profile.get("machine")
    process_name = active_profile.get("process")
    filament_name = active_profile.get("filament")

    if not isinstance(machine_name, str) or not machine_name:
        raise RuntimeError("Missing active machine preset in OrcaSlicer.conf")
    if not isinstance(process_name, str) or not process_name:
        raise RuntimeError("Missing active process preset in OrcaSlicer.conf")
    if not isinstance(filament_name, str) or not filament_name:
        raise RuntimeError("Missing active filament preset in OrcaSlicer.conf")

    machine_match = find_orca_preset_by_name(machine_name, user_dir, system_dir)
    process_match = find_orca_preset_by_name(process_name, user_dir, system_dir)

    if machine_match is None:
        raise FileNotFoundError(f"Machine preset not found by name: {machine_name}")
    if process_match is None:
        raise FileNotFoundError(f"Process preset not found by name: {process_name}")

    machine_setting = make_orca_setting_selection(machine_match.resolve(), user_dir, system_dir)
    process_setting = make_orca_setting_selection(process_match.resolve(), user_dir, system_dir)
    filament_setting = resolve_filament_setting_file(filament_name, user_dir, system_dir)

    bed_type_raw = active_profile.get("curr_bed_type", conf_data.get("app", {}).get("curr_bed_type"))
    bed_type = BED_TYPE_LABELS.get(str(bed_type_raw)) if bed_type_raw is not None else None

    return [machine_setting, process_setting, filament_setting], bed_type


def export_stl(scad_path: Path, stl_path: Path, openscad_exe: Path, dry_run: bool) -> None:
    ensure_exists(openscad_exe, "OpenSCAD executable")
    ensure_exists(scad_path, "OpenSCAD model")
    stl_path.parent.mkdir(parents=True, exist_ok=True)
    command = [str(openscad_exe), "-o", str(stl_path), str(scad_path)]
    run_command(command, "OpenSCAD export", dry_run)
    if not dry_run:
        ensure_exists(stl_path, "Exported STL")


def render_thumbnail_png(
    scad_path: Path,
    png_path: Path,
    openscad_exe: Path,
    size: int,
    dry_run: bool,
) -> None:
    ensure_exists(openscad_exe, "OpenSCAD executable")
    ensure_exists(scad_path, "OpenSCAD model")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(openscad_exe),
        "-o",
        str(png_path),
        f"--imgsize={size},{size}",
        "--viewall",
        "--autocenter",
        "--projection=o",
        str(scad_path),
    ]
    run_command(command, "OpenSCAD thumbnail", dry_run)
    if not dry_run:
        ensure_exists(png_path, "Rendered thumbnail PNG")


def build_orca_command(
    orca_exe: Path,
    stl_path: Path,
    gcode_path: Path,
    slicer_settings: list[OrcaSettingSelection],
    extra_args: list[str],
) -> list[str]:
    ensure_exists(orca_exe, "OrcaSlicer executable")
    command = [str(orca_exe), "--slice", "0"]
    if slicer_settings:
        load_settings: list[str] = []
        load_filaments: list[str] = []
        for setting in slicer_settings:
            if get_orca_setting_type(setting.selected_path) == "filament":
                effective_filament = write_effective_filament_setting(setting.load_paths, gcode_path.parent)
                load_filaments.append(str(effective_filament))
                continue

            for path in setting.load_paths:
                if get_orca_setting_type(path) == "filament":
                    load_filaments.append(str(path))
                else:
                    load_settings.append(str(path))
        if load_settings:
            command.extend(["--load-settings", ";".join(load_settings)])
        if load_filaments:
            command.extend(["--load-filaments", ";".join(load_filaments)])
            command.append("--load-defaultfila")
    command.extend(["--outputdir", str(gcode_path.parent)])
    command.extend(extra_args)
    command.append(str(stl_path))
    return command


def slice_stl(
    stl_path: Path,
    gcode_path: Path,
    orca_exe: Path,
    slicer_settings: list[OrcaSettingSelection],
    extra_args: list[str],
    dry_run: bool,
) -> None:
    command = build_orca_command(orca_exe, stl_path, gcode_path, slicer_settings, extra_args)
    gcode_path.parent.mkdir(parents=True, exist_ok=True)
    if not dry_run and gcode_path.exists():
        gcode_path.unlink()
    run_command(command, "OrcaSlicer", dry_run)
    if dry_run:
        return

    if gcode_path.exists():
        return

    candidates = sorted(gcode_path.parent.glob("*.gcode"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"Sliced G-code not found in output directory: {gcode_path.parent}")

    generated_path = candidates[0]
    if generated_path.resolve() != gcode_path.resolve():
        shutil.copy2(generated_path, gcode_path)
    ensure_exists(gcode_path, "Sliced G-code")


def gcode_has_thumbnail(gcode_path: Path) -> bool:
    with open(gcode_path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "thumbnail begin" in line.lower():
                return True
    return False


def strip_thumbnail_block(text: str) -> str:
    start_marker = "; THUMBNAIL_BLOCK_START"
    end_marker = "; THUMBNAIL_BLOCK_END"
    if start_marker not in text or end_marker not in text:
        return text

    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    while end < len(text) and text[end] in "\r\n":
        end += 1
    return text[:start] + text[end:]


def build_thumbnail_block(png_path: Path, size: int) -> str:
    encoded = base64.b64encode(png_path.read_bytes()).decode("ascii")
    wrapped = [encoded[index:index + 76] for index in range(0, len(encoded), 76)]
    lines = [
        "; THUMBNAIL_BLOCK_START",
        "",
        ";",
        f"; thumbnail begin {size}x{size} {len(encoded)}",
    ]
    lines.extend(f"; {line}" for line in wrapped)
    lines.extend([
        "; thumbnail end",
        "; THUMBNAIL_BLOCK_END",
        "",
    ])
    return "\n".join(lines)


def embed_thumbnail_in_gcode(gcode_path: Path, png_path: Path, size: int) -> None:
    original = gcode_path.read_text(encoding="utf-8", errors="replace")
    stripped = strip_thumbnail_block(original)
    block = build_thumbnail_block(png_path, size)

    header_end_marker = "; HEADER_BLOCK_END"
    if header_end_marker in stripped:
        insert_at = stripped.index(header_end_marker) + len(header_end_marker)
        if insert_at < len(stripped) and stripped[insert_at] == "\r":
            insert_at += 1
        if insert_at < len(stripped) and stripped[insert_at] == "\n":
            insert_at += 1
        updated = stripped[:insert_at] + "\n" + block + stripped[insert_at:]
    else:
        updated = block + stripped

    gcode_path.write_text(updated, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a SCAD model to STL, slice it with OrcaSlicer, upload the resulting G-code, and start the print."
    )
    parser.add_argument("scad_file", help="Path to .scad model")
    parser.add_argument("--host", default="192.168.1.156", help="Printer host/IP for upload step")
    parser.add_argument("--upload-url", default=None, help="Explicit upload URL (overrides --host), e.g. http://127.0.0.1:8080/uploadFile/upload")
    parser.add_argument("--openscad-exe", default=str(DEFAULT_OPENSCAD), help="Path to openscad executable")
    parser.add_argument("--orca-exe", default=str(DEFAULT_ORCA), help="Path to OrcaSlicer executable")
    parser.add_argument("--orca-conf", default=str(DEFAULT_ORCA_CONF), help="Path to OrcaSlicer.conf used to select active machine/process/filament")
    parser.add_argument("--orca-user-dir", default=str(DEFAULT_ORCA_USER_DIR), help="OrcaSlicer user preset directory used for auto-detecting machine/process settings")
    parser.add_argument("--orca-system-dir", default=str(DEFAULT_ORCA_SYSTEM_DIR), help="OrcaSlicer system preset directory used to resolve inherited presets")
    parser.add_argument("--build-dir", default=str(DEFAULT_BUILD_DIR), help="Directory for generated STL and G-code")
    parser.add_argument("--stl-output", default=None, help="Optional explicit STL output path")
    parser.add_argument("--gcode-output", default=None, help="Optional explicit G-code output path")
    parser.add_argument("--slicer-setting", action="append", default=[], help="Machine or process settings JSON to pass to OrcaSlicer; repeat as needed")
    parser.add_argument("--filament-preset", default=None, help="Optional OrcaSlicer filament preset name or JSON path")
    parser.add_argument("--orca-arg", action="append", default=[], help="Additional argument to pass through to OrcaSlicer; repeat as needed")
    parser.add_argument("--allow-missing-thumbnail", action="store_true", help="Do not fail if the produced G-code does not contain an embedded thumbnail")
    parser.add_argument("--thumbnail-size", type=int, default=DEFAULT_THUMBNAIL_SIZE, help="Embedded thumbnail size in pixels")
    parser.add_argument("--sim-stub-slicer", action="store_true", help="Generate a small known-good G-code with thumbnail instead of invoking OrcaSlicer")
    parser.add_argument("--skip-slice", action="store_true", help="Stop after STL export")
    parser.add_argument("--skip-upload", action="store_true", help="Do not upload the resulting G-code")
    parser.add_argument("--skip-print", action="store_true", help="Do not start the uploaded G-code on the printer")
    parser.add_argument("--mainboard-id", default=DEFAULT_MAINBOARD_ID, help="Optional printer mainboard ID to avoid discovery")
    parser.add_argument("--ws-port", type=int, default=3030, help="Override printer WebSocket port (used by simulator)")
    parser.add_argument("--auto-level", dest="auto_leveling", action="store_true", default=False, help="Enable auto leveling when starting the print")
    parser.add_argument("--no-auto-level", dest="auto_leveling", action="store_false", help="Disable auto leveling when starting the print")
    parser.add_argument("--timelapse", action="store_true", help="Enable timelapse if supported by the printer")
    parser.add_argument("--dry-run", action="store_true", help="Print external commands without executing them")
    parser.add_argument("--yes", action="store_true", help="Skip upload confirmation in upload_gcode.py")
    parser.add_argument("--ensure-prime-strip", action="store_true", help="Analyze and inject a priming strip before upload if one is not detected")
    parser.add_argument("--ensure-heat-order", action="store_true", help="Ensure blocking M190/M109 before any extrusion; auto-fix if needed")
    args = parser.parse_args()

    scad_path = Path(args.scad_file).resolve()
    build_dir = Path(args.build_dir).resolve()
    base_name = scad_path.stem
    stl_path = Path(args.stl_output).resolve() if args.stl_output else build_dir / f"{base_name}.stl"
    gcode_path = Path(args.gcode_output).resolve() if args.gcode_output else build_dir / f"{base_name}.gcode"
    thumbnail_path = build_dir / f"{base_name}-preview.png"
    openscad_exe = Path(args.openscad_exe)
    orca_exe = Path(args.orca_exe)
    orca_conf_path = Path(args.orca_conf).resolve()
    orca_user_dir = Path(args.orca_user_dir).resolve()
    orca_system_dir = Path(args.orca_system_dir).resolve()
    slicer_settings = [make_orca_setting_selection(Path(setting).resolve(), orca_user_dir, orca_system_dir) for setting in args.slicer_setting]
    detected_bed_type: str | None = None
    used_auto_detect = not slicer_settings
    if used_auto_detect:
        slicer_settings, detected_bed_type = auto_detect_orca_setting_files(orca_conf_path, orca_user_dir, orca_system_dir)
        if slicer_settings:
            print(f"Auto-detected OrcaSlicer settings from {orca_conf_path}:")
            for setting in slicer_settings:
                print_orca_setting_selection(setting, orca_user_dir, orca_system_dir)
            if detected_bed_type:
                print(f"  Bed type: {detected_bed_type}")
        else:
            print("No OrcaSlicer settings were auto-detected from config; slicing will rely on Orca defaults.")

    if args.filament_preset:
        filament_setting = resolve_filament_setting_file(args.filament_preset, orca_user_dir, orca_system_dir)
        slicer_settings = [
            setting for setting in slicer_settings
            if all(read_json_file(load_path).get("type") != "filament" for load_path in setting.load_paths)
        ]
        slicer_settings.append(filament_setting)
        print_orca_setting_selection(filament_setting, orca_user_dir, orca_system_dir, indent="")

    orca_args = list(args.orca_arg)
    if used_auto_detect and detected_bed_type and "--curr-bed-type" not in orca_args:
        orca_args.extend(["--curr-bed-type", detected_bed_type])

    export_stl(scad_path, stl_path, openscad_exe, args.dry_run)

    if args.skip_slice:
        print(f"Stopping after STL export: {stl_path}")
        return 0

    if args.sim_stub_slicer and not args.dry_run:
        # Write a tiny safe G-code stub with basic preheat, home, and a few layers
        gcode_path.parent.mkdir(parents=True, exist_ok=True)
        stub = [
            "; HEADER_BLOCK_END",
            "; Generated by print_scad.py --sim-stub-slicer",
            "M190 S60 ; bed temp",
            "M109 S210 ; nozzle temp",
            "G90 ; absolute pos",
            "M82 ; absolute extrusion",
            "G28 ; home all",
            "G1 Z0.2 F600",
        ]
        # Add a few fake layers with comments Orca typically emits
        for i in range(1, 21):
            stub.append(f";LAYER:{i}")
            stub.append("G1 X10 Y10 E0.5 F1200")
            stub.append("G1 X20 Y10 E1.0 F1200")
            stub.append("G1 X20 Y20 E1.5 F1200")
            stub.append("G1 X10 Y20 E2.0 F1200")
        stub.append("M104 S0")
        stub.append("M140 S0")
        gcode_path.write_text("\n".join(stub) + "\n", encoding="utf-8")
    else:
        slice_stl(stl_path, gcode_path, orca_exe, slicer_settings, orca_args, args.dry_run)

    if not args.dry_run:
        has_thumbnail = gcode_has_thumbnail(gcode_path)
        if not has_thumbnail:
            render_thumbnail_png(scad_path, thumbnail_path, openscad_exe, args.thumbnail_size, dry_run=False)
            # Emit the PNG path so GUI logs can show an inline preview
            print(f"Thumbnail PNG: {thumbnail_path}")
            embed_thumbnail_in_gcode(gcode_path, thumbnail_path, args.thumbnail_size)
            has_thumbnail = gcode_has_thumbnail(gcode_path)
        print(f"Thumbnail embedded: {'yes' if has_thumbnail else 'no'}")
        if not has_thumbnail and not args.allow_missing_thumbnail:
            raise RuntimeError(
                "The sliced G-code does not contain an embedded thumbnail. "
                "Use an OrcaSlicer profile/config that enables thumbnails, or pass --allow-missing-thumbnail."
            )

        analyzer = Path(__file__).with_name("analyze_and_fix_gcode.py")
        if analyzer.exists() and (args.ensure_heat_order or args.ensure_prime_strip):
            temp_path = gcode_path
            if args.ensure_heat_order:
                fixed_heat = temp_path.with_suffix(".heat.gcode")
                res = subprocess.run(
                    [sys.executable, str(analyzer), str(temp_path), "--fix-heat", "--out", str(fixed_heat)],
                    capture_output=True, text=True
                )
                sys.stdout.write(res.stdout)
                if res.stderr:
                    sys.stderr.write(res.stderr)
                if res.returncode == 0 and fixed_heat.exists():
                    temp_path = fixed_heat
                    print(f"Using heat-fixed G-code: {temp_path}")
            if args.ensure_prime_strip:
                fixed_prime = temp_path.with_suffix(".prime.gcode")
                res = subprocess.run(
                    [sys.executable, str(analyzer), str(temp_path), "--inject", "--out", str(fixed_prime)],
                    capture_output=True, text=True
                )
                sys.stdout.write(res.stdout)
                if res.stderr:
                    sys.stderr.write(res.stderr)
                if res.returncode == 0 and fixed_prime.exists():
                    temp_path = fixed_prime
                    print(f"Using prime-injected G-code: {temp_path}")
            gcode_path = temp_path

        if args.ensure_prime_strip:
            analyzer = Path(__file__).with_name("analyze_and_fix_gcode.py")
            if analyzer.exists():
                fixed_path = gcode_path.with_suffix(".prime.gcode")
                result = subprocess.run(
                    [sys.executable, str(analyzer), str(gcode_path), "--inject", "--out", str(fixed_path)],
                    capture_output=True,
                    text=True,
                )
                sys.stdout.write(result.stdout)
                if result.stderr:
                    sys.stderr.write(result.stderr)
                if result.returncode == 0 and fixed_path.exists():
                    gcode_path = fixed_path
                    print(f"Using prime-injected G-code: {gcode_path}")

    if args.skip_upload:
        if not args.skip_print:
            raise RuntimeError("Cannot start a print when upload is skipped. Use --skip-print too, or run print_file.py for an existing printer-side file.")
        print(f"Skipping upload. Generated G-code: {gcode_path}")
        return 0

    if args.dry_run:
        print(f"Dry run enabled; upload step not executed. Target G-code would be: {gcode_path}")
        if not args.skip_print:
            printer_filename = normalize_printer_filename(str(gcode_path))
            print(f"Dry run enabled; print step not executed. Printer-side filename would be: {printer_filename}")
        return 0

    upload_gcode_file(str(gcode_path), host=args.host, upload_url=args.upload_url, yes=args.yes)

    if args.skip_print:
        print(f"Upload complete. Not starting print because --skip-print was requested. Printer-side filename: {gcode_path.name}")
        return 0

    # Ensure pycentauri uses the desired WS port (simulator override)
    os.environ["PYCENTAURI_WS_PORT"] = str(args.ws_port)
    printer_filename = normalize_printer_filename(str(gcode_path))
    print(f"Uploaded file is expected on printer as: {printer_filename}")
    if not args.yes and not prompt_to_start_print(printer_filename, args.host):
        raise SystemExit(130)

    asyncio.run(
        start_print(
            args.host,
            printer_filename,
            args.auto_leveling,
            args.timelapse,
            args.mainboard_id,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, subprocess.SubprocessError, PrinterError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)