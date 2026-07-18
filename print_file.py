import argparse
import asyncio
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYCENTAURI_SRC = REPO_ROOT / "pycentauri" / "src"
if str(PYCENTAURI_SRC) not in sys.path:
    sys.path.insert(0, str(PYCENTAURI_SRC))

from pycentauri import Printer
from pycentauri.client import PrinterError
from pycentauri.discovery import discover as discover_printers
from pycentauri.models import PrintStatus


PRINTER_HOST = "192.168.1.156"
DEFAULT_MAINBOARD_ID = os.environ.get("PYCENTAURI_MAINBOARD_ID")
ACTIVE_PRINT_STATES = {
    PrintStatus.PRINTING,
    PrintStatus.PRINT_START,
    PrintStatus.PREHEATING,
    PrintStatus.AUTO_LEVELING,
    PrintStatus.RESONANCE_TESTING,
    PrintStatus.PAUSING,
    PrintStatus.PAUSED,
    PrintStatus.RESUMING,
    PrintStatus.STOPPING,
    PrintStatus.FILE_CHECKING,
    PrintStatus.PRINTER_CHECKING,
}


def normalize_printer_filename(value: str) -> str:
    candidate = Path(value)
    return candidate.name if candidate.name else value


def prompt_to_continue(filename: str, host: str) -> bool:
    try:
        input(
            f"About to start printer-side file '{filename}' on {host}. "
            "Press Enter to continue, or Ctrl+C to cancel..."
        )
        return True
    except KeyboardInterrupt:
        print("\nPrint request cancelled.")
        return False


def describe_status(status) -> str:
    return (
        f"state={status.state} "
        f"print_status={status.print_status} "
        f"filename={status.filename or '-'} "
        f"progress={status.progress if status.progress is not None else '-'}"
    )


async def resolve_target(host: str, mainboard_id: str | None) -> tuple[str, str | None]:
    if mainboard_id:
        return host, mainboard_id
    found = await discover_printers(timeout=1.0, retries=2)
    for printer in found:
        if printer.host == host and printer.mainboard_id:
            return host, printer.mainboard_id
    return host, None


async def start_print(
    host: str,
    filename: str,
    auto_leveling: bool,
    timelapse: bool,
    mainboard_id: str | None,
) -> None:
    host, resolved_mainboard_id = await resolve_target(host, mainboard_id)
    print(f"Connecting to printer at {host}...")
    if resolved_mainboard_id:
        print(f"Resolved mainboard ID: {resolved_mainboard_id}")
    else:
        print("Mainboard ID discovery did not return a result; continuing without it.")

    async with await Printer.connect(host, enable_control=True, mainboard_id=resolved_mainboard_id) as printer:
        print("Connected.")
        try:
            status = await printer.status(timeout=5.0)
        except asyncio.TimeoutError:
            status = None
            print("Warning: timed out waiting for printer status push; proceeding with start_print anyway.")

        if status is not None:
            print(f"Current status: {describe_status(status)}")
            if status.print_status in ACTIVE_PRINT_STATES:
                raise RuntimeError(
                    "Refusing to start a new print because the printer already appears busy. "
                    f"Current job: {status.filename or '<unknown>'}, print_status={status.print_status}."
                )

        result = await printer.start_print(
            filename,
            storage="local",
            auto_leveling=auto_leveling,
            timelapse=timelapse,
        )
        print(f"start_print sent for '{filename}'")
        print(result.inner)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start a print for a file that already exists on the Elegoo Centauri Carbon printer."
    )
    parser.add_argument("file", help="Printer-side file name, or a local path whose basename matches the printer file")
    parser.add_argument("--host", default=PRINTER_HOST, help="Printer IP address")
    parser.add_argument("--mainboard-id", default=DEFAULT_MAINBOARD_ID, help="Optional printer mainboard ID to avoid discovery")
    parser.add_argument("--auto-level", dest="auto_leveling", action="store_true", default=False, help="Enable auto leveling")
    parser.add_argument("--no-auto-level", dest="auto_leveling", action="store_false", help="Disable auto leveling")
    parser.add_argument("--timelapse", action="store_true", help="Enable timelapse if supported")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt before sending start_print")
    args = parser.parse_args()

    printer_filename = normalize_printer_filename(args.file)
    print(f"Requested file: {args.file}")
    print(f"Using printer-side filename: {printer_filename}")
    print("This command assumes the file already exists on the printer's local storage.")

    if not args.yes and not prompt_to_continue(printer_filename, args.host):
        raise SystemExit(130)

    await start_print(args.host, printer_filename, args.auto_leveling, args.timelapse, args.mainboard_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (RuntimeError, PrinterError) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)