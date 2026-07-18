from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


SYSTEM_PROMPT = """You generate valid OpenSCAD models from plain-English requests.

Return a JSON object with these keys:
- title: short filesystem-safe style name in plain ASCII words
- description: one-sentence summary of the object
- scad_code: complete OpenSCAD source code only

Requirements for scad_code:
- Produce a single printable solid when possible
- Use standard OpenSCAD syntax only
- Do not use import(), surface(), text() with external fonts, or external files
- Keep dimensions reasonable for FDM printing unless the user specifies otherwise
- Prefer simple parameterized code with top-level variables
- Use ASCII only
- Do not wrap the code in markdown fences
- Do not assign geometry nodes to variables (for example `shape = cube(...)` is invalid in OpenSCAD)
"""

SCAD_FIX_SYSTEM_PROMPT = """You repair OpenSCAD source code.

Return a JSON object with key:
- scad_code: complete corrected OpenSCAD source code

Rules:
- Keep the same model intent and dimensions unless required to fix syntax
- Fix parser/syntax issues and keep code printable
- Use valid OpenSCAD syntax only
- Do not wrap output in markdown fences
"""

SCAD_PRINT_FIX_SYSTEM_PROMPT = """You repair OpenSCAD so slicers can export and slice it reliably.

Return a JSON object with key:
- scad_code: complete corrected OpenSCAD source code

Rules:
- Keep the same model intent and approximate dimensions
- Produce one contiguous manifold solid suitable for FDM slicing
- Avoid floating/disconnected bodies, self-intersections, and zero-thickness features
- Keep a flat base on z=0 when reasonable
- Use valid OpenSCAD syntax only
- Do not wrap output in markdown fences
"""


def looks_like_name_plate_prompt(prompt: str) -> bool:
    normalized = prompt.lower()
    keywords = (
        "name plate",
        "nameplate",
        "name badge",
        "badge",
        "desk plate",
        "desk sign",
        "name tag",
        "plaque",
    )
    return any(keyword in normalized for keyword in keywords)


def looks_like_token_prompt(prompt: str) -> bool:
    normalized = prompt.lower()
    keywords = (
        "token",
        "game piece",
        "board game piece",
        "monopoly",
        "meeple",
        "pawn",
        "figurine",
    )
    return any(keyword in normalized for keyword in keywords)


def extract_requested_name(prompt: str) -> str | None:
    quoted = re.search(r'"([A-Za-z0-9 _.-]{1,40})"', prompt)
    if quoted:
        return quoted.group(1).strip()

    named = re.search(r"\bname\s+([A-Za-z][A-Za-z0-9_-]{0,39})\b", prompt, flags=re.IGNORECASE)
    if named:
        return named.group(1).strip()

    says = re.search(r"\bsays\s+([A-Za-z][A-Za-z0-9_-]{0,39})\b", prompt, flags=re.IGNORECASE)
    if says:
        return says.group(1).strip()

    return None


def build_generation_prompt(prompt: str) -> str:
    if looks_like_name_plate_prompt(prompt):
        requested_name = extract_requested_name(prompt) or "NAME"
        scaffold = f"""

Name-plate guidance:
- Prefer a simple rectangular plate with a solid base.
- Use OpenSCAD text() for lettering, not hand-built block letters.
- Use linear_extrude() to create raised letters above the base.
- Center the text on the plate with halign=\"center\" and valign=\"center\".
- Use a bold sans-serif font string that is commonly available, such as Liberation Sans:style=Bold.
- Default to a printable plate around 100mm x 30mm x 3mm unless the user requests other dimensions.
- Default to raised text around 12mm to 18mm tall and about 2mm to 3mm above the base.
- Keep the base plate resting on the build plane with its bottom at z=0.
- Do not use center=true for the base plate cube.
- Place raised text on top of the base, typically by translating it to [plate_length/2, plate_width/2, plate_thickness].
- The text should read exactly: {requested_name}
- Keep the result as one printable solid.
"""
        return prompt + scaffold

    if looks_like_token_prompt(prompt):
        scaffold = """

Game-token guidance:
- Design a single solid board-game token that is easy to print on FDM.
- Keep the base flat on z=0 with no supports required.
- Default size target: 18mm to 24mm wide and 2.5mm to 5mm thick unless user requests otherwise.
- Prefer rounded edges/chamfers and avoid tiny fragile protrusions.
- If adding icon details, emboss or deboss by 0.6mm to 1.2mm and keep strokes thick.
- Avoid thin walls under 1.2mm and avoid overhangs steeper than about 55 degrees.
- Keep geometry manifold and printable as one piece.
- Use simple primitive operations (union/difference/hull/minkowski) and top-level parameters.
"""
        return prompt + scaffold

    return prompt


def postprocess_name_plate_scad(scad_code: str) -> str:
    updated = scad_code

    updated = re.sub(
        r"cube\(\s*\[\s*plate_length\s*,\s*plate_(?:width|height)\s*,\s*plate_thickness\s*\]\s*,\s*center\s*=\s*true\s*\)",
        "cube([plate_length, plate_width, plate_thickness], center=false)",
        updated,
    )
    updated = re.sub(
        r"cube\(\s*\[\s*plate_length\s*,\s*plate_height\s*,\s*plate_thickness\s*\]\s*,\s*center\s*=\s*false\s*\)",
        "cube([plate_length, plate_width, plate_thickness], center=false)",
        updated,
    )
    updated = re.sub(
        r"cube\(\s*\[\s*plate_length\s*,\s*plate_height\s*,\s*plate_thickness\s*\]\s*,\s*center\s*=\s*true\s*\)",
        "cube([plate_length, plate_width, plate_thickness], center=false)",
        updated,
    )

    updated = re.sub(
        r"\bplate_height\b",
        "plate_width",
        updated,
    )

    updated = re.sub(
        r"translate\(\s*\[\s*0\s*,\s*0\s*,\s*plate_thickness\s*\]\s*\)\s*\n\s*linear_extrude",
        "translate([plate_length / 2, plate_width / 2, plate_thickness])\n        linear_extrude",
        updated,
    )

    return updated


def maybe_postprocess_scad(prompt: str, scad_code: str) -> str:
    if looks_like_name_plate_prompt(prompt):
        return postprocess_name_plate_scad(scad_code)
    return scad_code


def slugify(value: str) -> str:
    lowered = value.lower().strip()
    lowered = re.sub(r"[^a-z0-9]+", "_", lowered)
    lowered = lowered.strip("_")
    return lowered or "generated_model"


def extract_json_object(text: str) -> dict:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("Model response was not valid JSON")

    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model response could not be parsed as JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Model response JSON was not an object")
    return data


def extract_scad_code(payload: dict) -> tuple[str, str, str]:
    title = str(payload.get("title", "generated_model")).strip() or "generated_model"
    description = str(payload.get("description", "")).strip()
    scad_code = payload.get("scad_code")
    if not isinstance(scad_code, str) or not scad_code.strip():
        raise RuntimeError("Model response did not include scad_code")

    cleaned = scad_code.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_+-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    return title, description, cleaned + "\n"


def resolve_api_key(cfg: dict) -> str:
    explicit_key = str(cfg["generation"].get("api_key", "")).strip()
    env_name = str(cfg["generation"].get("api_key_env", "OPENAI_API_KEY")).strip() or "OPENAI_API_KEY"
    if explicit_key:
        return explicit_key
    value = os.environ.get(env_name)
    if value:
        return value
    raise RuntimeError(
        f"No API key provided. Set {env_name} or fill API Key in Settings."
    )


def request_scad_from_openai(
    *,
    prompt: str,
    api_key: str,
    api_base: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> dict:
    url = api_base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed: HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI API request failed: {exc}") from exc

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("OpenAI API response did not match expected shape") from exc

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("OpenAI API response did not include text content")
    return extract_json_object(content)


def write_metadata(metadata_path: Path, *, prompt: str, model: str, title: str, description: str) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "prompt": prompt,
        "model": model,
        "title": title,
        "description": description,
    }
    metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def request_scad_syntax_fix(
    *,
    scad_code: str,
    error_text: str,
    api_key: str,
    api_base: str,
    model: str,
) -> str:
    url = api_base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 3500,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SCAD_FIX_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Fix this OpenSCAD parser error while preserving model intent.\n\n"
                    f"Error:\n{error_text}\n\n"
                    f"SCAD source:\n{scad_code}"
                ),
            },
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI syntax-fix request failed: HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI syntax-fix request failed: {exc}") from exc

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Syntax-fix response did not match expected shape") from exc

    fixed_payload = extract_json_object(str(content))
    fixed_code = fixed_payload.get("scad_code")
    if not isinstance(fixed_code, str) or not fixed_code.strip():
        raise RuntimeError("Syntax-fix response did not include scad_code")

    cleaned = fixed_code.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_+-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    return cleaned + "\n"


def request_scad_printability_fix(
    *,
    scad_code: str,
    error_text: str,
    api_key: str,
    api_base: str,
    model: str,
) -> str:
    url = api_base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 3500,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SCAD_PRINT_FIX_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Fix this OpenSCAD model so slicers can export/slice it.\n\n"
                    f"Slicer error:\n{error_text}\n\n"
                    f"SCAD source:\n{scad_code}"
                ),
            },
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI printability-fix request failed: HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI printability-fix request failed: {exc}") from exc

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Printability-fix response did not match expected shape") from exc

    fixed_payload = extract_json_object(str(content))
    fixed_code = fixed_payload.get("scad_code")
    if not isinstance(fixed_code, str) or not fixed_code.strip():
        raise RuntimeError("Printability-fix response did not include scad_code")

    cleaned = fixed_code.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_+-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    return cleaned + "\n"
