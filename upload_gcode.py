#!/usr/bin/env python3
import argparse
import hashlib
import os
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests


DEFAULT_HOST = "192.168.1.156"
KNOWN_GCODE_COMMANDS = {
    "G0",
    "G1",
    "G2",
    "G3",
    "G4",
    "G10",
    "G17",
    "G18",
    "G19",
    "G20",
    "G21",
    "G28",
    "G90",
    "G91",
    "G92",
    "M82",
    "M83",
    "M84",
    "M104",
    "M106",
    "M107",
    "M109",
    "M140",
    "M190",
    "M201",
    "M203",
    "M204",
    "M205",
    "M220",
    "M221",
    "M400",
    "M73",
}

MOTION_COMMANDS = {"G0", "G1", "G2", "G3"}
NOZZLE_TEMPERATURE_COMMANDS = {"M104", "M109"}
BED_TEMPERATURE_COMMANDS = {"M140", "M190"}
TRUTHY_VALUES = {"1", "true", "yes", "on"}
FALSY_VALUES = {"0", "false", "no", "off"}


@dataclass
class Finding:
    severity: str
    message: str
    line_no: int | None = None


@dataclass
class PreflightSummary:
    findings: list[Finding] = field(default_factory=list)
    custom_commands: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    nozzle_temperatures: list[float] = field(default_factory=list)
    bed_temperatures: list[float] = field(default_factory=list)
    min_axis: dict[str, float] = field(default_factory=dict)
    max_axis: dict[str, float] = field(default_factory=dict)
    saw_homing: bool = False
    saw_absolute_positioning: bool = False
    saw_relative_positioning: bool = False
    saw_absolute_extrusion: bool = False
    saw_relative_extrusion: bool = False
    saw_extrusion: bool = False
    saw_blocking_nozzle_heat: bool = False
    warned_extrusion_before_blocking_heat: bool = False
    first_blocking_heat_line: int | None = None
    first_extrusion_line: int | None = None

    def add(self, severity: str, message: str, line_no: int | None = None) -> None:
        self.findings.append(Finding(severity=severity, message=message, line_no=line_no))

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "WARNING")


def is_preflight_enabled(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in FALSY_VALUES:
        return False
    if normalized in TRUTHY_VALUES:
        return True
    return True


def md5_file(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_float(token: str) -> float | None:
    try:
        return float(token)
    except ValueError:
        return None


def extract_parameter(tokens: list[str], names: set[str]) -> float | None:
    for token in tokens:
        if not token:
            continue
        parameter_name = token[0].upper()
        if parameter_name in names and len(token) > 1:
            value = parse_float(token[1:])
            if value is not None:
                return value
    return None


def format_temperature_range(values: list[float]) -> str:
    if not values:
        return "not set"
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        return f"{maximum:g} C"
    return f"{minimum:g} C to {maximum:g} C"


def format_axis_range(summary: "PreflightSummary", axis: str) -> str:
    if axis not in summary.min_axis or axis not in summary.max_axis:
        return "not observed"
    return f"{summary.min_axis[axis]:g} to {summary.max_axis[axis]:g}"


def summarize_mode(absolute_seen: bool, relative_seen: bool, absolute_name: str, relative_name: str) -> str:
    if absolute_seen and relative_seen:
        return f"{absolute_name} and {relative_name}"
    if absolute_seen:
        return absolute_name
    if relative_seen:
        return relative_name
    return "not declared"


def inspect_motion(tokens: list[str], line_no: int, summary: PreflightSummary) -> None:
    feed_rate = extract_parameter(tokens, {"F"})
    if feed_rate is not None and feed_rate > 60000:
        summary.add("WARNING", f"Very high feed rate F{feed_rate:g} mm/min", line_no)

    coordinates: dict[str, float] = {}
    for token in tokens:
        if len(token) < 2:
            continue
        parameter_name = token[0].upper()
        if parameter_name not in {"X", "Y", "Z", "E"}:
            continue
        value = parse_float(token[1:])
        if value is None:
            continue
        coordinates[parameter_name] = value

    for axis in ("X", "Y", "Z"):
        if axis not in coordinates:
            continue
        value = coordinates[axis]
        summary.min_axis[axis] = value if axis not in summary.min_axis else min(summary.min_axis[axis], value)
        summary.max_axis[axis] = value if axis not in summary.max_axis else max(summary.max_axis[axis], value)

        if axis in {"X", "Y"} and value < -5:
            summary.add("WARNING", f"Negative {axis} travel looks unusual: {value:g}", line_no)
        if axis == "Z" and value < -1:
            summary.add("WARNING", f"Negative Z travel looks unusual: {value:g}", line_no)
        if axis == "Z" and value > 500:
            summary.add("WARNING", f"Very large Z move: {value:g}", line_no)
        if axis in {"X", "Y"} and abs(value) > 1000:
            summary.add("WARNING", f"Very large {axis} move: {value:g}", line_no)

    extrusion_amount = coordinates.get("E")
    if extrusion_amount is None:
        return

    summary.saw_extrusion = True
    if summary.first_extrusion_line is None:
        summary.first_extrusion_line = line_no
    if not summary.saw_homing:
        summary.add("WARNING", "Extrusion move appears before any G28 homing command", line_no)
    if not summary.saw_blocking_nozzle_heat and not summary.warned_extrusion_before_blocking_heat:
        summary.add("WARNING", "Extrusion move appears before a blocking hotend heat command such as M109", line_no)
        summary.warned_extrusion_before_blocking_heat = True
    if abs(extrusion_amount) > 50:
        summary.add("WARNING", f"Large single extrusion move E{extrusion_amount:g}", line_no)


def preflight_gcode(file_path: str) -> PreflightSummary:
    summary = PreflightSummary()

    with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            code = raw_line.split(";", 1)[0].strip()
            if not code:
                continue

            tokens = code.split()
            command = tokens[0].upper()

            if command == "G28":
                summary.saw_homing = True
            elif command == "G90":
                summary.saw_absolute_positioning = True
            elif command == "G91":
                summary.saw_relative_positioning = True
            elif command == "M82":
                summary.saw_absolute_extrusion = True
            elif command == "M83":
                summary.saw_relative_extrusion = True

            if command in NOZZLE_TEMPERATURE_COMMANDS:
                target = extract_parameter(tokens[1:], {"S", "R"})
                if target is not None:
                    summary.nozzle_temperatures.append(target)
                    if command == "M109" and target >= 170:
                        summary.saw_blocking_nozzle_heat = True
                        if summary.first_blocking_heat_line is None:
                            summary.first_blocking_heat_line = line_no
                    if target > 300:
                        summary.add("ERROR", f"Nozzle temperature is above 300 C: {target:g}", line_no)
                    elif target > 260:
                        summary.add("WARNING", f"Nozzle temperature is unusually high: {target:g} C", line_no)

            if command in BED_TEMPERATURE_COMMANDS:
                target = extract_parameter(tokens[1:], {"S", "R"})
                if target is not None:
                    summary.bed_temperatures.append(target)
                    if target > 120:
                        summary.add("ERROR", f"Bed temperature is above 120 C: {target:g}", line_no)
                    elif target > 90:
                        summary.add("WARNING", f"Bed temperature is unusually high: {target:g} C", line_no)

            if command in MOTION_COMMANDS:
                inspect_motion(tokens[1:], line_no, summary)

            if command.startswith(("G", "M", "T")):
                if command not in KNOWN_GCODE_COMMANDS and not command.startswith("T"):
                    summary.custom_commands[command].append(line_no)
            else:
                summary.custom_commands[command].append(line_no)

    if not summary.saw_homing:
        summary.add("ERROR", "No G28 homing command was found before upload")
    if not summary.saw_absolute_positioning and not summary.saw_relative_positioning:
        summary.add("WARNING", "No explicit G90 or G91 positioning mode was found")
    if not summary.saw_absolute_extrusion and not summary.saw_relative_extrusion:
        summary.add("WARNING", "No explicit M82 or M83 extrusion mode was found")
    if summary.saw_extrusion and not summary.nozzle_temperatures:
        summary.add("ERROR", "Extrusion moves were found but no nozzle temperature command was detected")
    if summary.saw_extrusion and not summary.saw_blocking_nozzle_heat and not summary.warned_extrusion_before_blocking_heat:
        summary.add("WARNING", "Extrusion moves were found without a blocking hotend heat command such as M109")

    if summary.custom_commands:
        custom_preview = []
        for command, line_numbers in sorted(summary.custom_commands.items()):
            custom_preview.append(f"{command} (line {line_numbers[0]})")
        summary.add(
            "WARNING",
            "Non-standard or printer-specific commands were found: " + ", ".join(custom_preview[:8]),
        )

    return summary


def _iso_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def append_jsonl(log_path: Path, event: str, data: dict) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": _iso_now(), "event": event, "data": data}
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{rec}\n")
    except Exception:
        # Logging must not break uploads
        pass


def print_preflight_summary(file_path: str, summary: PreflightSummary) -> None:
    print("G-code preflight summary")
    print(f"  File: {file_path}")
    print(f"  Homing: {'yes' if summary.saw_homing else 'no'}")
    print(
        "  Positioning mode: "
        + summarize_mode(summary.saw_absolute_positioning, summary.saw_relative_positioning, "absolute", "relative")
    )
    print(
        "  Extrusion mode: "
        + summarize_mode(summary.saw_absolute_extrusion, summary.saw_relative_extrusion, "absolute", "relative")
    )
    print(f"  Nozzle temperatures: {format_temperature_range(summary.nozzle_temperatures)}")
    print(f"  Bed temperatures: {format_temperature_range(summary.bed_temperatures)}")
    print(f"  X range: {format_axis_range(summary, 'X')}")
    print(f"  Y range: {format_axis_range(summary, 'Y')}")
    print(f"  Z range: {format_axis_range(summary, 'Z')}")
    print(f"  Non-standard commands: {len(summary.custom_commands)}")
    print(f"  Errors: {summary.error_count}")
    print(f"  Warnings: {summary.warning_count}")

    if summary.findings:
        print("  Findings:")
        for finding in summary.findings:
            location = f"line {finding.line_no}: " if finding.line_no is not None else ""
            print(f"    [{finding.severity}] {location}{finding.message}")
    else:
        print("  Findings: none")


def prompt_to_continue(prompt: str) -> bool:
    try:
        input(prompt)
        return True
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
        return False


def upload_gcode_file(
    file_path: str,
    *,
    host: str = DEFAULT_HOST,
    upload_url: str | None = None,
    check: str = "1",
    offset: str = "0",
    upload_uuid: str | None = None,
    check_only: bool = False,
    yes: bool = False,
    timeout: int = 300,
    log_jsonl: str | None = None,
) -> requests.Response | None:
    resolved_path = os.path.abspath(file_path)
    if not os.path.exists(resolved_path):
        raise FileNotFoundError(f"file not found: {resolved_path}")

    log_path = Path(log_jsonl) if log_jsonl else None

    if is_preflight_enabled(check):
        summary = preflight_gcode(resolved_path)
        print_preflight_summary(resolved_path, summary)
        if log_path is not None:
            append_jsonl(
                log_path,
                "preflight",
                {
                    "file": resolved_path,
                    "size": os.path.getsize(resolved_path),
                    "nozzle_temps": summary.nozzle_temperatures,
                    "bed_temps": summary.bed_temperatures,
                    "blocking_heat": summary.saw_blocking_nozzle_heat,
                    "first_blocking_heat_line": summary.first_blocking_heat_line,
                    "first_extrusion_line": summary.first_extrusion_line,
                    "extrusion_before_blocking": (
                        summary.first_extrusion_line is not None
                        and (summary.first_blocking_heat_line is None or summary.first_extrusion_line < summary.first_blocking_heat_line)
                    ),
                    "errors": summary.error_count,
                    "warnings": summary.warning_count,
                },
            )
        if summary.error_count:
            raise RuntimeError("Preflight failed; upload was not attempted.")
        if check_only:
            print("Preflight passed; check-only mode requested, so no upload was attempted.")
            return None
        if not yes and not prompt_to_continue("Press Enter to proceed with upload, or Ctrl+C to cancel..."):
            raise KeyboardInterrupt
    elif check_only:
        raise RuntimeError("--check-only requires preflight to be enabled. Use a truthy --check value.")

    total_size = os.path.getsize(resolved_path)
    file_md5 = md5_file(resolved_path)
    effective_uuid = upload_uuid or uuid.uuid4().hex
    url = upload_url or f"http://{host}/uploadFile/upload"
    data = {
        "TotalSize": str(total_size),
        "Uuid": effective_uuid,
        "Offset": str(offset),
        "Check": str(check),
        "S-File-MD5": file_md5,
    }

    filename = os.path.basename(resolved_path)
    with open(resolved_path, "rb") as handle:
        files = {"File": (filename, handle, "application/octet-stream")}
        print(f"Uploading to: {url}")
        print(f"File: {resolved_path}")
        print(f"Size: {total_size}")
        print(f"MD5:  {file_md5}")
        print(f"Uuid: {effective_uuid}")
        response = requests.post(url, data=data, files=files, timeout=timeout)

    print(f"HTTP {response.status_code}")
    print(response.text)
    if log_path is not None:
        append_jsonl(
            log_path,
            "upload_result",
            {
                "file": resolved_path,
                "url": url,
                "status": response.status_code,
                "ok": 200 <= response.status_code < 300,
            },
        )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Upload failed with HTTP {response.status_code}")
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a G-code file to an Elegoo Centauri Carbon printer")
    parser.add_argument("file", help="Path to G-code file to upload")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Printer host/IP (ignored if --upload-url is provided)")
    parser.add_argument("--upload-url", default=None, help="Explicit upload URL, e.g. http://127.0.0.1:8080/uploadFile/upload")
    parser.add_argument("--check", default="1", help="Printer upload Check field. Truthy values also enable local preflight.")
    parser.add_argument("--offset", default="0", help="Offset form value")
    parser.add_argument("--uuid", dest="upload_uuid", default=None, help="Upload UUID (defaults to random hex)")
    parser.add_argument("--check-only", action="store_true", help="Run local G-code checks and exit without uploading")
    parser.add_argument("--yes", action="store_true", help="Skip the Enter confirmation prompt after preflight")
    parser.add_argument("--log-jsonl", default=str(Path("build/logs/upload_events.jsonl").resolve()), help="Append structured events to this JSONL file")
    args = parser.parse_args()

    try:
        upload_gcode_file(
            args.file,
            host=args.host,
            upload_url=args.upload_url,
            check=args.check,
            offset=args.offset,
            upload_uuid=args.upload_uuid,
            check_only=args.check_only,
            yes=args.yes,
            log_jsonl=args.log_jsonl,
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, RuntimeError, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())