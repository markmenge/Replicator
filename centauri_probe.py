import asyncio
import json
import time
from pathlib import Path

import websockets

HOST = "192.168.1.156"
WS_URL = f"ws://{HOST}:3030/websocket"
CMD_GET_STATUS = 0
CMD_GET_ATTR = 1
CMD_START_PRINT = 128
CMD_STOP_PRINT = 130
CMD_GET_FILE_LIST = 258
CMD_GET_FILE_DETAIL = 260
CMD_GET_HISTORY_ID = 320
CMD_GET_TASK_DETAIL = 321
CMD_CAMERA_STREAM_LIKELY = 324

COMMAND_NAMES = {
    CMD_START_PRINT: "SEND_PRINTER_START_PRINT",
    CMD_STOP_PRINT: "SEND_PRINTER_STOP_PRINT",
    CMD_GET_FILE_LIST: "GET_PRINTER_FILE_LIST",
    CMD_GET_FILE_DETAIL: "GET_PRINTER_FILE_DETAIL",
    CMD_GET_HISTORY_ID: "GET_PRINTER_HISTORY_ID",
    CMD_GET_TASK_DETAIL: "GET_PRINTER_TASK_DETAIL",
    CMD_CAMERA_STREAM_LIKELY: "Likely camera stream request (inferred from pcap)",
}


def make_msg(cmd: int, data: dict | None = None, mainboard_id: str = "") -> str:
    ts = int(time.time() * 1000)
    return json.dumps(
        {
            "Id": "",
            "Data": {
                "Cmd": cmd,
                "Data": data or {},
                "RequestID": f"py{ts}",
                "MainboardID": mainboard_id,
                "TimeStamp": ts,
                "From": 1,
            },
        }
    )


def extract_mainboard_id(*messages: dict) -> str:
    for message in messages:
        if not isinstance(message, dict):
            continue
        direct = message.get("MainboardID")
        if direct:
            return direct
        nested = message.get("Data", {}).get("MainboardID")
        if nested:
            return nested
    return ""


def build_start_payload(printer_path: str) -> dict:
    return {
        "Filename": printer_path,
        "StartLayer": 0,
        "Calibration_switch": 0,
        "PrintPlatformType": 0,
        "Tlp_Switch": 0,
        "slot_map": [],
    }


def print_command_map() -> None:
    print("Observed Centauri command IDs:")
    for command_id, name in sorted(COMMAND_NAMES.items()):
        print(f"  {command_id}: {name}")


async def recv_until(ws, predicate, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        msg = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
        obj = json.loads(msg)
        if predicate(obj):
            return obj
    raise TimeoutError("Timed out waiting for matching response")


async def probe(file_name: str | None = None, start: bool = False):
    async with websockets.connect(WS_URL, max_size=None, open_timeout=10) as ws:
        await ws.send(make_msg(CMD_GET_ATTR))
        attrs = await recv_until(ws, lambda o: o.get("Topic", "").startswith("sdcp/attributes/"), timeout=5)
        print("ATTRS:")
        print(json.dumps(attrs, indent=2))

        await ws.send(make_msg(CMD_GET_STATUS))
        status = await recv_until(ws, lambda o: o.get("Topic", "").startswith("sdcp/status/"), timeout=5)
        print("STATUS:")
        print(json.dumps(status, indent=2))

        mainboard_id = extract_mainboard_id(attrs, status)
        print(f"MAINBOARD_ID: {mainboard_id or '<missing>'}")

        await ws.send(make_msg(CMD_GET_FILE_LIST, {"Url": "/local"}, mainboard_id=mainboard_id))
        files = await recv_until(
            ws,
            lambda o: o.get("Data", {}).get("Cmd") == CMD_GET_FILE_LIST,
            timeout=8,
        )
        print("FILES:")
        print(json.dumps(files, indent=2)[:12000])

        if file_name:
            print(f"\nRequested file: {file_name}")
            names = [f["name"] for f in files["Data"]["Data"].get("FileList", [])]
            matched = [n for n in names if Path(n).name.lower() == file_name.lower()]
            if not matched:
                print("No exact printer-side filename match found.")
                return
            printer_path = matched[0]
            print(f"Matched printer path: {printer_path}")

            await ws.send(
                make_msg(
                    CMD_GET_FILE_DETAIL,
                    {"Url": printer_path},
                    mainboard_id=mainboard_id,
                )
            )
            try:
                detail = await recv_until(
                    ws,
                    lambda o: o.get("Data", {}).get("Cmd") == CMD_GET_FILE_DETAIL,
                    timeout=5,
                )
                print("FILE DETAIL:")
                print(json.dumps(detail, indent=2)[:12000])
            except Exception as exc:
                print(f"No file detail response observed: {exc}")

            if start:
                payload = build_start_payload(printer_path)
                print("\nSTART PROBE SEND:")
                print(json.dumps(payload, indent=2))
                await ws.send(make_msg(CMD_START_PRINT, payload, mainboard_id=mainboard_id))
                try:
                    response = await recv_until(
                        ws,
                        lambda o: (
                            o.get("Data", {}).get("Cmd") == CMD_START_PRINT
                            or o.get("Topic", "").startswith("sdcp/status/")
                            and Path(
                                o.get("Status", {}).get("PrintInfo", {}).get("Filename", "")
                            ).name.lower()
                            == Path(printer_path).name.lower()
                        ),
                        timeout=8,
                    )
                    print("START RESULT:")
                    print(json.dumps(response, indent=2))
                except Exception as exc:
                    print(f"No direct start response observed: {exc}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Printer-side file name to look for")
    parser.add_argument("--start", action="store_true", help="Attempt start command")
    parser.add_argument(
        "--show-command-map",
        action="store_true",
        help="Print the observed command-ID map and exit",
    )
    args = parser.parse_args()
    if args.show_command_map:
        print_command_map()
    else:
        asyncio.run(probe(args.file, args.start))
