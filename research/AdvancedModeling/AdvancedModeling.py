from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
import subprocess
import hashlib

# Reuse existing replicator utilities
from replicator_config import CONFIG_PATH, load_config, project_dirs
from replicator_generation import (
    build_generation_prompt,
    request_scad_from_openai,
    extract_scad_code,
    maybe_postprocess_scad,
    request_scad_syntax_fix,
    request_scad_printability_fix,
    rate_images_with_vision,
    resolve_api_key,
    slugify,
)


@dataclass
class Constraints:
    max_dim_mm: float = 120.0
    min_wall_mm: float = 1.2
    flat_base: bool = True
    no_supports: bool = True


@dataclass
class Budget:
    max_iters: int = 1
    candidates_per_iter: int = 2
    timeout_s: int = 600
    target_score: float = 70.0
    beam_width: int = 2


@dataclass
class Engines:
    model_gen: List[str] = None  # ["openscad"] for now
    vlm: str = "none"  # placeholder
    embedder: str = "none"

    def __post_init__(self):
        if self.model_gen is None:
            self.model_gen = ["openscad"]


@dataclass
class Metrics:
    vision: float = 50.0
    geo: float = 0.0
    spec: float = 0.0
    penalties: Dict[str, float] = None

    def __post_init__(self):
        if self.penalties is None:
            self.penalties = {}


@dataclass
class Candidate:
    id: str
    engine: str
    source_path: Path
    stl_path: Optional[Path]
    views: List[Path]
    metrics: Metrics
    score: float
    notes: str = ""


@dataclass
class Result:
    best: Candidate
    ranked: List[Candidate]
    history: List[Dict[str, Any]]
    work_dir: Path


class AdvancedModeler:
    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = cfg or load_config(CONFIG_PATH)
        self.api_key = resolve_api_key(self.cfg)
        self.api_base = str(self.cfg["generation"].get("api_base"))
        self.model = str(self.cfg["generation"].get("model"))
        self.base_temp = float(self.cfg["generation"].get("temperature", 0.2))
        self.max_tokens = int(self.cfg["generation"].get("max_tokens", 2500))
        self._ref_images: List[Path] = []

    # ------------------------ Public API ------------------------
    def generate(
        self,
        description: str,
        ref_images: Optional[List[Path]] = None,
        constraints: Optional[Constraints] = None,
        budget: Optional[Budget] = None,
        engines: Optional[Engines] = None,
        callbacks: Optional[List] = None,
    ) -> Result:
        constraints = constraints or Constraints()
        budget = budget or Budget()
        engines = engines or Engines()
        callbacks = callbacks or []
        self._ref_images = list(ref_images or [])

        # Workspace paths
        pd = project_dirs(self.cfg)
        base = Path(pd["base"]).resolve()
        adv_root = base / "generated_advanced"
        adv_root.mkdir(parents=True, exist_ok=True)
        slug = self._safe_slug(description)
        work_dir = adv_root / slug
        work_dir.mkdir(parents=True, exist_ok=True)

        history: List[Dict[str, Any]] = []
        population: List[Candidate] = []

        for it in range(budget.max_iters):
            iter_dir = work_dir / f"iter_{it:03d}"
            iter_dir.mkdir(parents=True, exist_ok=True)

            # 1) Generate candidates (OpenSCAD only for now)
            new_candidates = self._generate_openscad_candidates(
                description, constraints, budget.candidates_per_iter, iter_dir
            )

            # 2) Render/export for each candidate (non-fatal per-candidate)
            for cand in new_candidates:
                try:
                    self._export_openscad(cand)
                except Exception:
                    cand.stl_path = None
                    cand.views = []

            # 3) Evaluate (geometry + placeholder vision). If poor geometry, attempt one repair pass.
            for cand in new_candidates:
                self._evaluate_candidate(cand, description)
                needs_fix = (cand.metrics.geo < 0.8) or any(
                    k in (cand.metrics.penalties or {}) for k in [
                        "empty_mesh", "zero_volume", "degenerate_bbox", "disconnected_bodies"
                    ]
                )
                if needs_fix:
                    try:
                        self._one_shot_printability_fix(cand, description)
                    except Exception:
                        pass

            # 4) Rank & select
            population.extend(new_candidates)
            population.sort(key=lambda c: c.score, reverse=True)
            population = population[: max(budget.beam_width, len(new_candidates))]

            # Callbacks for UI/progress
            for cb in callbacks:
                try:
                    cb(it, list(population), {"work_dir": str(work_dir)})
                except Exception:
                    pass

            # Save iteration log
            history.append(
                {
                    "iteration": it,
                    "candidates": [self._cand_to_json(c) for c in new_candidates],
                    "population": [self._cand_to_json(c) for c in population],
                }
            )

            if population and population[0].score >= budget.target_score:
                break

        if not population:
            raise RuntimeError("No candidates generated")

        best = population[0]
        result = Result(best=best, ranked=list(population), history=history, work_dir=work_dir)

        # Mirror best artifacts into standard Replicator folders
        self._mirror_best(best)

        # Persist result.json in work_dir
        (work_dir / "result.json").write_text(self._result_to_json(result), encoding="utf-8")

        # Also persist context.json
        context = {
            "description": description,
            "constraints": asdict(constraints),
            "budget": asdict(budget),
            "engines": asdict(engines),
            "config_model": self.model,
            "api_base": self.api_base,
        }
        (work_dir / "context.json").write_text(json.dumps(context, indent=2), encoding="utf-8")

        return result

    def _safe_slug(self, text: str, max_len: int = 40) -> str:
        base = slugify(text)
        if len(base) <= max_len:
            return base
        h = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:8]
        trimmed = base[: max_len - 9].rstrip("-_")
        return f"{trimmed}-{h}"

    # ------------------------ Internal helpers ------------------------
    def _generate_openscad_candidates(
        self, description: str, constraints: Constraints, n: int, iter_dir: Path
    ) -> List[Candidate]:
        candidates: List[Candidate] = []
        prompt = self._build_advanced_prompt(description, constraints)

        for i in range(n):
            # Small temperature jitter per candidate
            temp = max(0.0, min(1.0, self.base_temp + (i - (n - 1) / 2) * 0.15))
            payload = request_scad_from_openai(
                prompt=build_generation_prompt(prompt),
                api_key=self.api_key,
                api_base=self.api_base,
                model=self.model,
                temperature=temp,
                max_tokens=self.max_tokens,
            )
            title, description_text, scad_code = extract_scad_code(payload)
            scad_code = maybe_postprocess_scad(description, scad_code)

            cand_dir = iter_dir / f"cand_{i+1:04d}"
            cand_dir.mkdir(parents=True, exist_ok=True)
            scad_path = cand_dir / "model.scad"
            scad_path.write_text(scad_code, encoding="utf-8", newline="\n")

            candidates.append(
                Candidate(
                    id=f"cand_{i+1:04d}",
                    engine="openscad",
                    source_path=scad_path,
                    stl_path=None,
                    views=[],
                    metrics=Metrics(),
                    score=0.0,
                    notes=description_text,
                )
            )
        return candidates

    def _export_openscad(self, cand: Candidate) -> None:
        # Export STL
        pd = project_dirs(self.cfg)
        openscad_exe = Path(str(self.cfg["paths"].get("openscad_exe", "")))
        if not openscad_exe.exists():
            raise FileNotFoundError(f"OpenSCAD executable not found: {openscad_exe}")

        stl_out = cand.source_path.with_suffix(".stl")
        img_iso = cand.source_path.with_name("view_iso.png")

        # STL export
        cmd_stl = [str(openscad_exe), "-o", str(stl_out), str(cand.source_path)]
        res = subprocess.run(cmd_stl, capture_output=True, text=True)
        if res.returncode != 0:
            # Attempt one syntax-fix retry using compiler errors
            try:
                fixed_code = request_scad_syntax_fix(
                    scad_code=cand.source_path.read_text(encoding="utf-8", errors="replace"),
                    error_text=(res.stdout or "") + "\n" + (res.stderr or ""),
                    api_key=self.api_key,
                    api_base=self.api_base,
                    model=self.model,
                )
                cand.source_path.write_text(fixed_code, encoding="utf-8", newline="\n")
                res2 = subprocess.run(cmd_stl, capture_output=True, text=True)
                if res2.returncode != 0:
                    raise RuntimeError(f"OpenSCAD export failed after syntax-fix: {res2.stderr or res2.stdout}")
            except Exception as exc:
                raise RuntimeError(f"OpenSCAD export failed: {res.stderr or res.stdout}\nSyntax-fix error: {exc}")

        # PNG ISO view
        cmd_img_iso = [
            str(openscad_exe),
            "-o",
            str(img_iso),
            "--imgsize=768,768",
            "--viewall",
            "--autocenter",
            "--projection=p",
            str(cand.source_path),
        ]
        self._run_cmd(cmd_img_iso)

        cand.stl_path = stl_out
        cand.views = [p for p in [img_iso] if p.exists()]

    def _evaluate_candidate(self, cand: Candidate, description: str) -> None:
        # Geometry metrics via trimesh (optional)
        geo_score, penalties = self._geometry_metrics(cand.stl_path)
        # Vision rating if we have at least one view
        vision_score = 50.0
        vlm_details: Dict[str, Any] = {}
        if cand.views:
            try:
                rating = rate_images_with_vision(
                    prompt=description,
                    image_paths=cand.views,
                    api_key=self.api_key,
                    api_base=self.api_base,
                    model=self.model,
                    max_tokens=600,
                    ref_image_paths=self._ref_images if self._ref_images else None,
                )
                vision_score = float(rating.get("score", 50.0))
                vlm_details = rating
            except Exception:
                vision_score = 50.0
        spec_score = 0.0
        metrics = Metrics(vision=vision_score, geo=geo_score, spec=spec_score, penalties=penalties)
        cand.metrics = metrics
        # Weighted score (kept simple for MVP)
        cand.score = 0.6 * metrics.vision + 0.4 * (metrics.geo * 100.0) - sum(penalties.values())

        # Save metrics.json alongside candidate
        mpath = cand.source_path.with_name("metrics.json")
        mpath.write_text(json.dumps(self._metrics_to_json(metrics), indent=2), encoding="utf-8")
        if vlm_details:
            (cand.source_path.with_name("vlm_rating.json")).write_text(json.dumps(vlm_details, indent=2), encoding="utf-8")

        # Save a readable textual critique with suggestions
        critique = self._make_english_critique(description, cand, metrics)
        (cand.source_path.with_name("critique.txt")).write_text(critique, encoding="utf-8")

    def _one_shot_printability_fix(self, cand: Candidate, description: str) -> None:
        # Use the existing printability-fix helper with a synthetic error summary, then re-export and re-evaluate once
        scad_code = cand.source_path.read_text(encoding="utf-8", errors="replace")
        error_summary = "Geometry analysis indicates non-printable aspects (e.g., disconnected bodies, zero volume, or poor manifoldness)."
        fixed_code = request_scad_printability_fix(
            scad_code=scad_code,
            error_text=error_summary,
            api_key=self.api_key,
            api_base=self.api_base,
            model=self.model,
        )
        cand.source_path.write_text(fixed_code, encoding="utf-8", newline="\n")
        # Re-export and re-evaluate
        self._export_openscad(cand)
        self._evaluate_candidate(cand, description)

    def _geometry_metrics(self, stl_path: Optional[Path]) -> Tuple[float, Dict[str, float]]:
        penalties: Dict[str, float] = {}
        if not stl_path or not stl_path.exists():
            return 0.0, {"missing_stl": 30.0}

        try:
            import trimesh  # type: ignore
        except Exception:
            # If trimesh is missing, give a neutral geo score but penalize
            return 0.5, {"no_trimesh": 10.0}

        try:
            mesh = trimesh.load(str(stl_path), force="mesh")
            if mesh.is_empty:
                return 0.0, {"empty_mesh": 50.0}
            bbox = mesh.bounding_box.extents
            if any(e <= 0 for e in bbox):
                penalties["degenerate_bbox"] = 40.0
            # Normalize a crude geometry score: manifoldness + size sanity
            watertight = float(mesh.is_watertight)
            vol = float(mesh.volume) if hasattr(mesh, "volume") else 0.0
            if vol <= 0.0:
                penalties["zero_volume"] = 40.0
            # z-min near zero
            zmin = float(mesh.bounds[0][2])
            if abs(zmin) > 0.2:
                penalties["base_not_on_z0"] = 5.0
            # Penalize disconnected floating parts (multiple components)
            try:
                comps = mesh.split(only_watertight=False)
                if isinstance(comps, (list, tuple)) and len(comps) > 1:
                    penalties["disconnected_bodies"] = min(30.0, 10.0 * (len(comps) - 1))
            except Exception:
                pass
            # Simple combination
            base_geo = 0.4 * watertight + 0.6 * (1.0 if vol > 0 else 0.0)
            # Clip to [0,1]
            base_geo = max(0.0, min(1.0, base_geo))
            return base_geo, penalties
        except Exception:
            return 0.0, {"trimesh_error": 30.0}

    def _mirror_best(self, best: Candidate) -> None:
        pd = project_dirs(self.cfg)
        gen_dir = Path(pd["generated"]).resolve()
        stl_dir = Path(pd["stl"]).resolve()
        gen_dir.mkdir(parents=True, exist_ok=True)
        stl_dir.mkdir(parents=True, exist_ok=True)

        # Mirror using the overall description slug as name
        # best.source_path = .../generated_advanced/<slug>/iter_xxx/cand_xxxx/model.scad
        # parents[0]=cand_xxxx, [1]=iter_xxx, [2]=<slug>
        slug = best.source_path.parents[2].name
        src_scad = best.source_path
        dst_scad = gen_dir / f"{slug}.scad"
        try:
            dst_scad.write_text(src_scad.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        if best.stl_path and best.stl_path.exists():
            dst_stl = stl_dir / f"{slug}.stl"
            try:
                dst_stl.write_bytes(best.stl_path.read_bytes())
            except Exception:
                pass

    def _build_advanced_prompt(self, description: str, constraints: Constraints) -> str:
        lines = [description.strip(), "\nConstraints:"]
        lines.append(f"- Flat base on z=0: {'yes' if constraints.flat_base else 'no'}")
        lines.append(f"- Avoid supports: {'yes' if constraints.no_supports else 'no'}")
        lines.append(f"- Minimum wall thickness: {constraints.min_wall_mm} mm")
        lines.append(f"- Maximum overall dimension: {constraints.max_dim_mm} mm")
        # Domain guidance for chess pieces without switching to CadQuery
        desc_low = description.lower()
        if any(w in desc_low for w in ["chess", "rook", "bishop", "knight", "queen", "king", "pawn"]):
            lines.append("")
            lines.append("Guidance:")
            lines.append("- Prefer rotate_extrude() of a 2D profile for the main body.")
            lines.append("- Keep one contiguous manifold solid; avoid floating/disconnected details.")
            lines.append("- Use chunky crenellations for a rook via difference() with sufficient thickness.")
        return "\n".join(lines)

    def _run_cmd(self, cmd: List[str]) -> None:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed: {subprocess.list2cmdline(cmd)} (rc={proc.returncode})")

    def _cand_to_json(self, c: Candidate) -> Dict[str, Any]:
        return {
            "id": c.id,
            "engine": c.engine,
            "source_path": str(c.source_path),
            "stl_path": str(c.stl_path) if c.stl_path else None,
            "views": [str(v) for v in c.views],
            "metrics": self._metrics_to_json(c.metrics),
            "score": c.score,
            "notes": c.notes,
        }

    def _metrics_to_json(self, m: Metrics) -> Dict[str, Any]:
        return {
            "vision": m.vision,
            "geo": m.geo,
            "spec": m.spec,
            "penalties": m.penalties,
        }

    def _result_to_json(self, r: Result) -> str:
        payload = {
            "best": self._cand_to_json(r.best),
            "ranked": [self._cand_to_json(c) for c in r.ranked],
            "history": r.history,
            "work_dir": str(r.work_dir),
        }
        return json.dumps(payload, indent=2)

    def _make_english_critique(self, description: str, cand: Candidate, metrics: Metrics) -> str:
        lines: List[str] = []
        lines.append(f"Description: {description}")
        lines.append(f"Candidate: {cand.id} ({cand.engine})")
        lines.append("")
        lines.append("Scores:")
        lines.append(f"- Vision (placeholder): {metrics.vision:.1f} / 100")
        lines.append(f"- Geometry: {metrics.geo*100.0:.1f} / 100")
        total = 0.6 * metrics.vision + 0.4 * (metrics.geo * 100.0) - sum(metrics.penalties.values())
        lines.append(f"- Total: {total:.1f}")
        lines.append("")

        # Issues detected from penalties and low geometry score
        issues: List[str] = []
        pen = metrics.penalties or {}
        msg_map = {
            "missing_stl": "Exported STL missing; OpenSCAD export may have failed.",
            "no_trimesh": "Geometry checks unavailable (trimesh not installed).",
            "empty_mesh": "Mesh is empty; the model likely produced no solid geometry.",
            "degenerate_bbox": "Degenerate bounding box; dimensions may be zero or invalid unions.",
            "zero_volume": "Zero volume mesh; ensure solids have thickness and are unioned.",
            "base_not_on_z0": "Base not on z=0; the part may not sit flat on the build plate.",
            "trimesh_error": "Geometry analysis failed; STL may be corrupt or incompatible.",
            "disconnected_bodies": "Multiple disconnected parts detected; ensure everything is one printable solid.",
        }
        for k, v in pen.items():
            issues.append(f"{msg_map.get(k, k)} (penalty {v:.1f})")
        if metrics.geo < 0.6 and not pen:
            issues.append("Geometry score is low; model may not be watertight or has invalid volume.")

        if issues:
            lines.append("Issues:")
            for it in issues:
                lines.append(f"- {it}")
            lines.append("")

        # Suggestions based on issues and common printability best practices
        suggestions: List[str] = []
        if "base_not_on_z0" in pen or metrics.geo < 0.7:
            suggestions.append("Ensure the base sits on z=0 (flat build surface).")
        if "zero_volume" in pen or "empty_mesh" in pen or metrics.geo < 0.6:
            suggestions.append("Union all parts into a single watertight solid; avoid zero‑thickness features.")
        if "degenerate_bbox" in pen:
            suggestions.append("Check dimensional parameters; avoid zeros where size is required.")
        if not suggestions:
            suggestions.append("Consider thickening fragile features and adding small fillets/chamfers for strength.")

        lines.append("Suggestions:")
        for s in suggestions:
            lines.append(f"- {s}")

        # Reference to views for manual inspection
        if cand.views:
            lines.append("")
            lines.append("Views:")
            for v in cand.views:
                lines.append(f"- {v}")

        return "\n".join(lines) + "\n"


# Optional: small CLI to exercise the generator directly
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AdvancedModeling quick driver")
    ap.add_argument("--desc", required=True, help="Model description")
    ap.add_argument("--max-iters", type=int, default=1)
    ap.add_argument("--candidates", type=int, default=2)
    ap.add_argument("--target", type=float, default=70.0)
    args = ap.parse_args()

    cfg = load_config(CONFIG_PATH)
    am = AdvancedModeler(cfg)
    res = am.generate(
        description=args.desc,
        constraints=Constraints(),
        budget=Budget(max_iters=args.max_iters, candidates_per_iter=args.candidates, target_score=args.target),
        engines=Engines(),
    )
    print(json.dumps(json.loads(am._result_to_json(res)), indent=2))
