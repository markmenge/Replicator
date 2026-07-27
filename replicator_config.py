from __future__ import annotations

import json
from pathlib import Path


CONFIG_PATH = Path(__file__).with_name("replicator.json")
DEFAULT_OPENSCAD = Path(r"C:\Program Files\OpenSCAD\openscad.exe")
DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_PREVIEW_SIZE = 768


def default_config() -> dict:
    return {
        "version": 2,
        "printer": {
            "host": "192.168.1.156",
            "auto_level": False,
            "timelapse": False,
            "skip_confirmation": True,
        },
        "generation": {
            "api_key": "",
            "api_key_env": "OPENAI_API_KEY",
            "api_base": DEFAULT_API_BASE,
            "model": DEFAULT_MODEL,
            "temperature": 0.2,
            "max_tokens": 2500,
            "preview_size": DEFAULT_PREVIEW_SIZE,
            "name": "",
            "offline_nameplate": False,
            "dry_run": False,
            "whisper_model": "base",
            "voice_seconds": 8,
        },
        "paths": {
            "openscad_exe": str(DEFAULT_OPENSCAD),
            "orca_exe": r"D:/Program Files/OrcaSlicer/orca-slicer.exe",
            "orca_conf": r"C:/Users/Mark/AppData/Roaming/OrcaSlicer/OrcaSlicer.conf",
            "orca_user_dir": r"C:/Users/Mark/AppData/Roaming/OrcaSlicer/user/default",
            "orca_system_dir": r"C:/Users/Mark/AppData/Roaming/OrcaSlicer/system",
        },
        "projects": {
            "root_dir": str((Path(__file__).with_name("projects")).resolve()),
            "name": "default",
        },
        "slicing": {
            "filament_preset": "",
            "allow_missing_thumbnail": False,
            "thumbnail_size": 144,
            "ensure_heat_order": False,
            "ensure_prime_strip": False,
            "ws_port": 3030,
            "upload_url": "",
            "sim_stub_slicer": False,
            "sim": False,
        },
        "ui": {
            "print_enabled": False,
            "show_preview": True,
            "visualize_before_print": False,
            "last_prompt": "",
            "advanced_modeling": False,
            "ref_image": "",
        },
    }


def merge_dict(dst: dict, src: dict) -> dict:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            merge_dict(dst[key], value)
        else:
            dst[key] = value
    return dst


def load_config(path: Path) -> dict:
    cfg = default_config()
    if not path.exists():
        save_config(path, cfg)
        return cfg

    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, dict):
            merge_dict(cfg, current)
    except json.JSONDecodeError:
        pass
    return cfg


def save_config(path: Path, cfg: dict) -> None:
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def normalize_optional_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"none", "null"}:
        return ""
    return text


def project_dirs(cfg: dict) -> dict:
    root = Path(str(cfg.get("projects", {}).get("root_dir", Path(__file__).with_name("projects").resolve()))).resolve()
    name = str(cfg.get("projects", {}).get("name", "default")).strip() or "default"
    base = root / name
    gen = base / "generated"
    stl = base / "stl"
    gcode = base / "gcode"
    return {"root": root, "name": name, "base": base, "generated": gen, "stl": stl, "gcode": gcode}
