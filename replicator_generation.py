from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
import base64


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
- Prefer lathe-style shapes with rotate_extrude() for revolved profiles when suitable (e.g., chess pieces)
- Keep the model resting on the build plane with its base at z=0 (avoid center=true on base solids)
- Avoid tiny disconnected ornaments; union parts into one contiguous manifold solid
"""


def _uses_max_completion_tokens(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith("gpt-5") or m.startswith("o4")


def _supports_temperature(model: str) -> bool:
    m = (model or "").lower()
    # GPT-5 family currently enforces default temperature only; omit or use 1
    return not (m.startswith("gpt-5") or m.startswith("o4"))


def _message_text_from_choice(choice: dict) -> str:
    try:
        msg = choice.get("message", {})
    except Exception:
        msg = {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    # Some models return content as a list of parts
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if "text" in part and isinstance(part["text"], str):
                    parts.append(part["text"])
                elif part.get("type") in ("text", "output_text") and isinstance(part.get("content"), str):
                    parts.append(str(part.get("content")))
        if parts:
            return "\n".join(parts)
    # Fallback empty
    return ""


def _responses_aggregate_text(data: dict) -> str:
    # Try common fields first
    if isinstance(data, dict):
        if isinstance(data.get("output_text"), str) and data.get("output_text").strip():
            return str(data.get("output_text"))
        # Some variants nest under 'response' or 'output'
        for key in ("response", "output", "outputs"):
            block = data.get(key)
            if isinstance(block, dict):
                # Single response object
                txt = _responses_aggregate_text(block)
                if txt:
                    return txt
            if isinstance(block, list):
                parts: list[str] = []
                for item in block:
                    if not isinstance(item, dict):
                        continue
                    # Item may have 'content' which is a list
                    content = item.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict):
                                if "text" in c and isinstance(c["text"], str):
                                    parts.append(c["text"])
                                elif c.get("type") in ("text", "output_text") and isinstance(c.get("content"), str):
                                    parts.append(str(c.get("content")))
                                elif isinstance(c.get("text"), dict) and isinstance(c["text"].get("value"), str):
                                    parts.append(c["text"]["value"])  # some SDKs use text.value
                if parts:
                    return "\n".join(parts)
    # Fallback: no obvious structured text
    return ""


def _openai_client(api_key: str, api_base: str):
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("OpenAI SDK not installed. Run: pip install openai") from exc
    # OpenAI SDK accepts base_url (root that includes /v1)
    client = OpenAI(api_key=api_key, base_url=api_base.rstrip("/"))
    return client


def _encode_image_to_data_uri(path: Path) -> str:
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def rate_images_with_vision(
    *,
    prompt: str,
    image_paths: list[Path],
    api_key: str,
    api_base: str,
    model: str,
    max_tokens: int = 600,
    ref_image_paths: list[Path] | None = None,
) -> dict:
    """Rate candidate images against the prompt using a JSON rubric.

    Returns a dict like {"score": float, "pros": [...], "cons": [...], "edit_suggestions": [...]}.
    """
    if not image_paths:
        return {"score": 50.0, "pros": [], "cons": ["No images provided"], "edit_suggestions": []}

    data_uris = []
    for p in image_paths:
        try:
            if p.exists():
                data_uris.append(_encode_image_to_data_uri(p))
        except Exception:
            continue
    if not data_uris:
        return {"score": 50.0, "pros": [], "cons": ["Images missing"], "edit_suggestions": []}

    rubric = (
        "Grade 0–100 with this rubric: "
        "Shape fidelity to description (0–45), Proportion/structure plausibility (0–25), "
        "Printability cues visible (flat base, sturdy features) (0–15), Aesthetics/cleanliness (0–15). "
        "If one or more reference images are provided, compare the candidate images to the reference and "
        "favor closer visual similarity in silhouette and proportions. "
        "Return a JSON object only with keys: score (number), pros (array of strings), "
        "cons (array of strings), edit_suggestions (array of strings)."
    )

    is_gpt5 = _uses_max_completion_tokens(model)
    if is_gpt5:
        client = _openai_client(api_key, api_base)
        content_blocks = [{"type": "input_text", "text": rubric + "\n\nPrompt: " + prompt}]
        # Attach reference images first (if any), followed by candidate images
        if ref_image_paths:
            for p in ref_image_paths:
                try:
                    if p.exists():
                        content_blocks.append({"type": "input_image", "image_url": _encode_image_to_data_uri(p)})
                except Exception:
                    pass
        for uri in data_uris:
            content_blocks.append({"type": "input_image", "image_url": uri})
        messages = [
            {"role": "system", "content": "You are a strict JSON grader. Output only JSON."},
            {"role": "user", "content": content_blocks},
        ]
        kwargs = {
            "model": model,
            "input": messages,
            "max_output_tokens": max_tokens + 200,
            "reasoning": {"effort": "low"},
        }
        try:
            resp = client.responses.create(**kwargs)  # type: ignore[arg-type]
        except Exception as exc:
            raise RuntimeError(f"Vision rating failed: {exc}") from exc
        try:
            content = getattr(resp, "output_text", None) or _responses_aggregate_text(resp.to_dict())  # type: ignore[attr-defined]
        except Exception:
            content = ""
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Vision model did not return content")
        return extract_json_object(content)
    else:
        # Chat Completions with vision content blocks
        url = api_base.rstrip("/") + "/chat/completions"
        user_content = [{"type": "text", "text": rubric + "\n\nPrompt: " + prompt}]
        if ref_image_paths:
            for p in ref_image_paths:
                try:
                    if p.exists():
                        user_content.append({"type": "image_url", "image_url": {"url": _encode_image_to_data_uri(p)}})
                except Exception:
                    pass
        for uri in data_uris:
            user_content.append({"type": "image_url", "image_url": {"url": uri}})
        payload = {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "You are a strict JSON grader. Output only JSON."},
                {"role": "user", "content": user_content},
            ],
        }
        if _supports_temperature(model):
            payload["temperature"] = 0
        if _uses_max_completion_tokens(model):
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens

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
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
        choice0 = data["choices"][0]
        content = _message_text_from_choice(choice0)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Vision model did not return content")
        return extract_json_object(content)

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


def looks_like_chess_piece_prompt(prompt: str) -> bool:
    normalized = prompt.lower()
    keywords = (
        "chess",
        "rook",
        "bishop",
        "knight",
        "queen",
        "king",
        "pawn",
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
    if looks_like_chess_piece_prompt(prompt):
        scaffold = """

Chess-piece guidance:
- Use rotate_extrude() over a 2D profile to form the primary body; keep the base flat on z=0.
- Default total height: 40–50 mm unless specified; choose proportional radii.
- Maintain minimum wall/feature thickness ≥ 1.2 mm; avoid fragile overhangs.
- Produce one contiguous manifold solid. Use union() and avoid floating parts.
- For a rook: cylindrical tower with thicker base, debossed or difference() crenellations kept chunky.
- For knight/bishop/queen/king: favor simple printable silhouettes; avoid thin spikes.
- Keep parameters at top for height, radii, and feature sizes.
"""
        return prompt + scaffold
    elif looks_like_name_plate_prompt(prompt):
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
    elif looks_like_token_prompt(prompt):
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
    else:
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

    raw_obj = text[start:end + 1]
    try:
        data = json.loads(raw_obj)
    except json.JSONDecodeError:
        # Heuristic fix for invalid backslash escapes inside large string fields like scad_code
        def _fix_invalid_escapes(s: str) -> str:
            import re
            # Replace backslashes that are not part of a valid JSON escape sequence with escaped backslashes
            # Valid escapes: \\ \" \/ \b \f \n \r \t \uXXXX
            def repl(m: re.Match[str]) -> str:
                bs, nxt = m.group(1), m.group(2)
                if nxt in ['\\', '"', '/', 'b', 'f', 'n', 'r', 't']:
                    return bs + '\\' + nxt  # already valid pattern matched once; keep as-is by doubling? no — don't change
                if nxt == 'u':
                    return bs + '\\u'
                # otherwise, escape it
                return bs + '\\' + nxt
            # Use a regex that finds a single backslash not followed by a valid escape char
            fixed = re.sub(r"(\\)([^\\\"/bfnrtu])", repl, s)
            return fixed

        fixed_obj = _fix_invalid_escapes(raw_obj)
        try:
            data = json.loads(fixed_obj)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Model response could not be parsed as JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Model response JSON was not an object")
    return data


def _extract_scad_fallback(text: str) -> dict | None:
    """Best-effort SCAD extractor when the model didn't return valid JSON.

    Looks for a fenced code block and returns a minimal payload if found.
    """
    import re as _re
    m = _re.search(r"```(?:openscad|scad)?\s*([\s\S]*?)```", text, flags=_re.IGNORECASE)
    if m:
        code = m.group(1).strip()
        if code:
            return {"title": "generated_model", "description": "", "scad_code": code}
    # Heuristic: grab long blocks containing common OpenSCAD tokens if no fences
    tokens = ("rotate_extrude", "linear_extrude", "union(", "difference(", "cube(", "cylinder(", "sphere(")
    if any(t in text for t in tokens):
        # Extract lines between first and last semicolon burst as a crude block
        try:
            lines = text.splitlines()
            idxs = [i for i, ln in enumerate(lines) if any(t in ln for t in tokens)]
            if idxs:
                i0 = max(0, idxs[0] - 3)
                i1 = min(len(lines), idxs[-1] + 50)
                snippet = "\n".join(lines[i0:i1]).strip()
                if snippet:
                    return {"title": "generated_model", "description": "", "scad_code": snippet}
        except Exception:
            pass
    return None


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


from typing import Callable, Optional


def request_scad_from_openai(
    *,
    prompt: str,
    api_key: str,
    api_base: str,
    model: str,
    temperature: float,
    max_tokens: int,
    on_debug: Optional[Callable[[str], None]] = None,
) -> dict:
    def _dbg(msg: str) -> None:
        if on_debug:
            try:
                on_debug(msg)
            except Exception:
                pass
    # Prefer Chat Completions for non-GPT-5; use SDK Responses API for GPT-5 family
    is_gpt5 = _uses_max_completion_tokens(model)
    _dbg(f"API: request model={model} is_gpt5={is_gpt5} max_tokens={max_tokens}")
    if is_gpt5:
        # SDK path
        client = _openai_client(api_key, api_base)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt + "\n\nRespond with a single JSON object only."},
        ]
        kwargs = {"model": model, "input": messages}
        # Token limits for Responses API (pad to reduce truncation from reasoning)
        kwargs["max_output_tokens"] = max_tokens + 1000
        # Hint to minimize reasoning verbosity
        kwargs["reasoning"] = {"effort": "low"}
        # Some deployments reject non-default temperature for GPT-5; omit to use default
        if _supports_temperature(model):
            kwargs["temperature"] = temperature

        try:
            resp = client.responses.create(**kwargs)  # type: ignore[arg-type]
            _dbg("API: responses.create returned")
        except Exception as exc:
            raise RuntimeError(f"OpenAI API (responses) request failed: {exc}") from exc

        try:
            # SDK convenience property
            content = getattr(resp, "output_text", None) or ""
            if not content:
                # Fallback to dict walk
                d = resp.to_dict()  # type: ignore[attr-defined]
                _dbg(f"API: responses.to_dict keys={list(d.keys()) if isinstance(d, dict) else type(d)}")
                content = _responses_aggregate_text(d)
                if not content:
                    # Write debug payload to inspect response shape
                    try:
                        Path("replicator_api_debug.json").write_text(json.dumps(d, indent=2), encoding="utf-8")
                        _dbg("API: wrote replicator_api_debug.json for inspection")
                    except Exception:
                        pass
        except Exception:
            content = ""
        if not isinstance(content, str) or not content.strip():
            _dbg("API: empty content from responses path")
            raise RuntimeError("OpenAI API response did not include text content")
        try:
            return extract_json_object(content)
        except Exception:
            try:
                Path("replicator_api_raw.txt").write_text(str(content), encoding="utf-8")
                _dbg("API: wrote replicator_api_raw.txt fallback")
            except Exception:
                pass
            # Fallback: try to salvage SCAD from fences
            fallback = _extract_scad_fallback(str(content))
            if fallback:
                _dbg("API: salvaged SCAD from non-JSON content")
                return fallback
            raise

    # Legacy Chat Completions path for non-GPT-5
    url = api_base.rstrip("/") + "/chat/completions"
    base_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    if is_gpt5:
        payload = {
            "model": model,
            "input": base_messages,
        }
        # Responses API token field
        payload["max_output_tokens"] = max_tokens
        if _supports_temperature(model):
            payload["temperature"] = temperature
    else:
        payload = {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": base_messages,
        }
        if _supports_temperature(model):
            payload["temperature"] = temperature
        if _uses_max_completion_tokens(model):
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _do_request(pl: dict) -> dict:
        body = json.dumps(pl).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        _dbg("API: POST chat.completions")
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            _dbg(f"API: HTTP {getattr(resp, 'status', '?')} bytes={len(raw)}")
            return json.loads(raw)

    # First attempt: JSON mode
    try:
        data = _do_request(payload)
        if is_gpt5:
            content = _responses_aggregate_text(data)
            if content.strip():
                return extract_json_object(content)
        else:
            choice0 = data["choices"][0]
            content = _message_text_from_choice(choice0)
            if isinstance(content, str) and content.strip():
                return extract_json_object(content)
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API request failed: HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI API request failed: {exc}") from exc
    except Exception:
        # Fall through to retry
        pass

    # Fallback: remove response_format and insist on JSON in the user message
    if is_gpt5:
        fallback_payload = {
            "model": model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt + "\n\nRespond with a single JSON object only."},
            ],
        }
        fallback_payload["max_output_tokens"] = max_tokens
        if _supports_temperature(model):
            fallback_payload["temperature"] = temperature
    else:
        fallback_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt + "\n\nRespond with a single JSON object only."},
            ],
        }
        if _supports_temperature(model):
            fallback_payload["temperature"] = temperature
        if _uses_max_completion_tokens(model):
            fallback_payload["max_completion_tokens"] = max_tokens
        else:
            fallback_payload["max_tokens"] = max_tokens

    data = _do_request(fallback_payload)
    if is_gpt5:
        content = _responses_aggregate_text(data)
    else:
        choice0 = data["choices"][0]
        content = _message_text_from_choice(choice0)
    if not isinstance(content, str) or not content.strip():
        _dbg("API: empty content from fallback path")
        raise RuntimeError("OpenAI API response did not include text content")
    try:
        return extract_json_object(content)
    except Exception:
        try:
            Path("replicator_api_raw.txt").write_text(str(content), encoding="utf-8")
            _dbg("API: wrote replicator_api_raw.txt fallback (chat path)")
        except Exception:
            pass
        fallback = _extract_scad_fallback(str(content))
        if fallback:
            _dbg("API: salvaged SCAD from non-JSON content (chat path)")
            return fallback
        raise


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
    is_gpt5 = _uses_max_completion_tokens(model)
    if is_gpt5:
        client = _openai_client(api_key, api_base)
        messages = [
            {"role": "system", "content": SCAD_FIX_SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Fix this OpenSCAD parser error while preserving model intent.\n\n"
                f"Error:\n{error_text}\n\n"
                f"SCAD source:\n{scad_code}"
            )},
        ]
        kwargs = {"model": model, "input": messages, "max_output_tokens": 4500, "reasoning": {"effort": "low"}}
        if _supports_temperature(model):
            kwargs["temperature"] = 0
        try:
            resp = client.responses.create(**kwargs)  # type: ignore[arg-type]
        except Exception as exc:
            raise RuntimeError(f"OpenAI syntax-fix (responses) failed: {exc}") from exc
        try:
            content = getattr(resp, "output_text", None) or _responses_aggregate_text(resp.to_dict())  # type: ignore[attr-defined]
        except Exception:
            content = ""
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
    else:
        url = api_base.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
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
        if _supports_temperature(model):
            payload["temperature"] = 0
        if _uses_max_completion_tokens(model):
            payload["max_completion_tokens"] = 3500
        else:
            payload["max_tokens"] = 3500
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
        if is_gpt5:
            content = _responses_aggregate_text(data)
        else:
            choice0 = data["choices"][0]
            content = _message_text_from_choice(choice0)
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
    is_gpt5 = _uses_max_completion_tokens(model)
    if is_gpt5:
        client = _openai_client(api_key, api_base)
        messages = [
            {"role": "system", "content": SCAD_PRINT_FIX_SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Fix this OpenSCAD model so slicers can export/slice it.\n\n"
                f"Slicer error:\n{error_text}\n\n"
                f"SCAD source:\n{scad_code}"
            )},
        ]
        kwargs = {"model": model, "input": messages, "max_output_tokens": 4500, "reasoning": {"effort": "low"}}
        if _supports_temperature(model):
            kwargs["temperature"] = 0
        try:
            resp = client.responses.create(**kwargs)  # type: ignore[arg-type]
        except Exception as exc:
            raise RuntimeError(f"OpenAI printability-fix (responses) failed: {exc}") from exc
        try:
            content = getattr(resp, "output_text", None) or _responses_aggregate_text(resp.to_dict())  # type: ignore[attr-defined]
        except Exception:
            content = ""
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
    else:
        url = api_base.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
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
        if _supports_temperature(model):
            payload["temperature"] = 0
        if _uses_max_completion_tokens(model):
            payload["max_completion_tokens"] = 3500
        else:
            payload["max_tokens"] = 3500
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
        if is_gpt5:
            content = _responses_aggregate_text(data)
        else:
            choice0 = data["choices"][0]
            content = _message_text_from_choice(choice0)
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
