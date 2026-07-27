# AdvancedModeling — Vision‑Ranked, Iterative 3D Model Generation (Spec)

This document specifies a Python module and workflow that generates higher‑quality, more complex 3D models from an English description and optional reference images, ranks the results with an AI vision model plus geometric checks, and iterates until a good result is achieved. It builds on lessons from the current Replicator flow, but adds multi‑candidate search, structured evaluation, and learning across runs.

---

## Goals
- Accept an English description and optional reference images to generate printable 3D models.
- Produce multiple candidate models per iteration and rank them with vision + geometry criteria.
- Iterate automatically using model‑guided edits, syntax/print fixes, and param tweaks until a target score is met or a budget is exhausted.
- Learn across runs via an experience store (retrieval hints, operator priors, prompt scaffolds).
- Integrate cleanly with existing Replicator assets (OpenSCAD, slicer paths, project outputs).

## Non‑Goals (Initial)
- Full Blender/CAD authoring; focus first on OpenSCAD and CadQuery backends.
- End‑to‑end printing automation; use Replicator’s existing print pipeline for that.
- Training/fine‑tuning foundation models; rely on provider APIs and prompt‑level learning initially.

---

## Lessons Incorporated from Replicator
- Always return complete source, not fenced code blocks; JSON wrappers are parsed to extract `scad_code`.
- Prefer one manifold solid, base on z=0; avoid tiny/thin features and extreme overhangs.
- Avoid OpenSCAD pitfalls: no assigning geometry to variables; avoid external font dependencies.
- Automatic syntax/printability repair loops are effective after slicer or parser errors.
- Name‑plate and token scaffolds improve reliability; generalized, prompt‑conditioned scaffolds are valuable.

---

## High‑Level Architecture

```mermaid
flowchart LR
    A[Text Prompt\n+ Optional Images] --> B[Prompt Builder\n + Retrieval Hints]
    B --> C[Candidate Generator\n(LLM → OpenSCAD/CadQuery)]
    C --> D[Render/Export\n(OpenSCAD/CQ → STL + PNG views)]
    D --> E[Geometry Validator\n(trimesh checks)]
    D --> F[Vision Rater\n(LLM with vision)]
    E --> G[Score & Rank]
    F --> G
    G -->|Target met?| H{Stop}
    G -->|No| I[Iterative Improver\n(mutate, repair, re‑prompt)]
    I --> C
    G --> J[Experience Store\n(SQLite + Artifacts)]
```

Components:
- Prompt Builder: Adds task‑specific scaffolds, constraints, and retrieved tips from past successes.
- Candidate Generator: Produces N candidates via LLM‑to‑code using backends:
  - OpenSCAD (structured CSG)
  - CadQuery (for lofts, sweeps, revolves; better for organic/curved forms like chess pieces)
- Renderer/Exporter: Produces STL and multi‑view PNGs from each candidate.
- Geometry Validator: Manifoldness, thin walls, overhang heuristic, size/volume sanity.
- Vision Rater: VLM grades multi‑view images vs. the prompt and reference images.
- Score & Rank: Combines numeric metrics into a single score; selects survivors.
- Iterative Improver: Mutates parameters or requests targeted code edits based on critique.
- Experience Store: Records prompt → artifacts → scores → final choice to enable learning.

---

## Inputs and Outputs
- Inputs:
  - `description: str` — English description of the desired model
  - `ref_images: list[Path]` — 0–3 reference images (optional)
  - `constraints: Constraints` — size limits, unit mm, printability hints
  - `budget: Budget` — max iters, max candidates/iter, API cost/time caps
  - `engines: Engines` — which backends/providers to enable (OpenSCAD, CadQuery, which VLM)
- Outputs:
  - `Result` containing:
    - `best: Candidate` with `scad_path|py_path`, `stl_path`, view PNGs, `score_breakdown`
    - `ranked: list[Candidate]` top‑K with paths + scores
    - `history: list[IterationLog]` decisions, critiques, edits
    - `work_dir: Path` artifact folder (per prompt slug)

---

## Public Python API (to be implemented in AdvancedModeling.py)

- `AdvancedModeler(config: Config)`
- `generate(
    description: str,
    ref_images: list[Path] | None = None,
    constraints: Constraints | None = None,
    budget: Budget | None = None,
    engines: Engines | None = None,
    callbacks: list[Callback] | None = None,
) -> Result`

Data contracts (sketch):
- `Constraints`: `max_dim_mm: float`, `min_wall_mm: float`, `flat_base: bool`, `no_supports: bool`
- `Budget`: `max_iters: int`, `candidates_per_iter: int`, `timeout_s: int`, `target_score: float`
- `Engines`: `model_gen: list["openscad","cadquery"]`, `vlm: str`, `embedder: str`
- `Candidate`: `id: str`, `engine: str`, `source_path: Path`, `stl_path: Path`, `views: list[Path]`, `metrics: Metrics`, `score: float`, `notes: str`
- `Metrics`: `vision: float`, `geo: float`, `spec: float`, `penalties: dict[str,float]`
- `Result`: `best: Candidate`, `ranked: list[Candidate]`, `history: list[IterationLog]`, `work_dir: Path`

Callbacks receive `(iteration_index, candidates: list[Candidate], context: RunContext)` for UI/progress.

---

## Directory Layout and Files
By default, artifacts live under the current Replicator project, with a per‑prompt workspace. Example:

```
projects/<name>/generated_advanced/<slug>/
  iter_000/
    cand_0001/
      model.scad | model.py
      model.stl
      view_iso.png
      view_front.png
      metrics.json
      critique.txt
  iter_001/
    ...
  result.json           # full Result serialization
  context.json          # prompt, constraints, engine/config versions
  experience.sqlite     # (optional) local store when using per‑prompt DB
```

A flat mirror of top artifacts will also be written alongside existing Replicator outputs for easy reuse:
- `projects/<name>/generated/<slug>.scad` or `.py` (CadQuery)
- `projects/<name>/stl/<slug>.stl`

---

## Candidate Generation Strategies
1) OpenSCAD (CSG) via LLM prompt‑to‑code
   - Strong for mechanical/parametric solids.
   - Use Replicator’s proven system prompts; extend with constraints (flat base, wall thickness hints).
2) CadQuery (Python CAD kernel)
   - Strong for profiles, lofts, sweeps, revolves—suited for chess pieces.
   - LLM emits Python using CadQuery API guarded by a small template and unit defaults.

Seeding: generate `candidates_per_iter` across engines (e.g., 2 OpenSCAD + 2 CadQuery) with varied temperatures and parameterized scaffolds.

---

## Rendering and Export
- OpenSCAD: use existing `openscad.exe` path to export STL and PNG view(s). Multiple views via `--camera` or by rotating the model and snapshotting; at minimum, iso + front.
- CadQuery: script runs headless to export STL; PNGs via a tiny CQ renderer or `trimesh` scene.
- Views: standardize filenames (`view_iso.png`, `view_front.png`, `view_side.png`), fixed size (e.g., 768×768).

---

## Geometry Validation (trimesh‑based)
Checks per STL:
- Manifoldness: watertight, no self‑intersections (approx).
- Dimensions: within `max_dim_mm`; non‑zero volume/surface.
- Thin walls: sample local thickness via ray pairs/voxelization heuristic; penalize < `min_wall_mm`.
- Overhang heuristic: surface normals vs. build Z to estimate support likelihood; penalize steep areas if `no_supports`.
- Base flatness: sufficient coplanar faces on z=0 within epsilon.

Outputs normalized to 0–1 `geo` score plus named penalties.

---

## Vision Rating (VLM)
- Input: prompt text, reference images (0–3), and the candidate’s view PNGs.
- Rubric (0–100):
  - Shape fidelity to description/reference (0–45)
  - Proportion/structure plausibility (0–25)
  - Printability cues visible (flat base, sturdy features) (0–15)
  - Aesthetics/cleanliness (0–15)
- Response: `{score: float, pros: [..], cons: [..], edit_suggestions: [..]}`
- Provider: pluggable “vision model” adapter (OpenAI‑compatible or local). Use JSON mode and deterministic temperature for rating.

---

## Scoring and Selection
Combine into a single score `S` per candidate:

- Normalize to 0–100:
  - `V = vision_score` (already 0–100)
  - `G = 100 * geo` (0–100)
  - `P = 100 - penalty_sum_clipped`
  - `Spec = spec_match` (optional text‑similarity 0–100 via embeddings)
- Weighted sum (tunable): `S = 0.6*V + 0.25*G + 0.15*Spec - penalty_boost`
- Rank descending; keep top‑K for mutation (`beam_width`).

Stop when `max_iters` reached or `best.S >= target_score`.

---

## Iterative Improvement Loop
Pseudocode:

```python
def iterate(ctx):
    pop = seed_candidates(ctx)
    for t in range(ctx.budget.max_iters):
        render_export(pop)
        evaluate(pop)    # geometry + VLM rating
        rank(pop)
        if pop[0].score >= ctx.budget.target_score:
            break
        # Build edits from critiques + heuristics
        edits = make_edits_from_critiques(pop[:ctx.beam_width])
        pop = mutate_and_regenerate(pop[:ctx.beam_width], edits, ctx)
    return best_of(pop)
```

Mutation operators (examples):
- Parameter nudges (height/width/radius/chamfer/fillet amounts).
- Profile adjustments (for CadQuery: spline control points, revolve profiles).
- Morphological ops (thicken, fillet/chamfer, smooth hull where printable).
- Targeted LLM “edit this code to …” using `edit_suggestions` from the rater.
- Automatic fix passes: syntax repair and printability repair (reuse Replicator helpers).

Operator selection is weighted by past success (learned priors).

---

## Learning and Experience Store
- Storage: SQLite DB + artifact paths; or a centralized DB later.
- Contents: prompt text, slug, constraints, engine, code snapshots, metrics, critiques, final result.
- Retrieval: embed prompt text and short descriptors; on new runs, fetch nearest neighbors to prime scaffolds and operator priors.
- Learning signals:
  - Which mutations increased score (per category)?
  - Which scaffolds (e.g., “revolve with S‑curve profile”) correlate with high scores for similar tasks?
  - Persist “critic → action” pairs to improve edit prompts.

---

## Configuration
- Reuse `replicator.json` where possible:
  - Paths: `openscad_exe`, Orca, project folders (via `project_dirs()`)
  - Generation: `api_base`, `model`, token/temperature defaults
- AdvancedModeling extras (to add):
  - `advanced.candidates_per_iter`, `advanced.max_iters`, `advanced.target_score`
  - `advanced.vlm_model`, `advanced.embed_model`
  - `advanced.beam_width`, `advanced.timeout_s`

---

## Test Script Contract (research/AdvancedModeling/Test_AdvancedModeling.py)
The test harness will:
- Accept a description and optional `--image path` repeated.
- Instantiate `AdvancedModeler` with config derived from `replicator.json`.
- Run `generate(...)` for a small budget (e.g., 2 iters × 3 candidates).
- Print a concise summary and write `result.json` in the work dir.

Example usage:

```bash
python research/AdvancedModeling/Test_AdvancedModeling.py \
  --desc "Chess knight, printable, 40mm tall, flat base" \
  --image research/AdvancedModeling/refs/knight_ref.png \
  --max-iters 2 --candidates 3 --target 78
```

Expected output artifacts are described in “Directory Layout”.

---

## Minimal Provider Interfaces
- `VisionRater.rate(prompt, views, ref_images) -> RatingJSON`
- `Generator.generate(prompt, scaffold, engine) -> CandidateSource` (OpenSCAD or CadQuery)
- `Renderer.export(source) -> STL + PNG views`
- `Validator.check(stl) -> Metrics`
- `Improver.edit(candidate, critique) -> CandidateSource`

All providers must be pure‑function style where possible and return explicit artifacts/paths.

---

## Dependencies (initial set)
- `trimesh` — mesh validation and simple rendering
- `numpy` — numerics
- `Pillow` — image utilities
- `cadquery` — optional generator backend for complex/curvy forms
- OpenSCAD — CLI exporter for SCAD → STL/PNG
- An OpenAI‑compatible client (reuse simple `urllib` flow from Replicator or `requests`)

These will be added incrementally; the test harness guards optional pieces.

---

## Acceptance Criteria (initial)
- On prompts like “Chess rook/bishop/knight, printable, 40–50mm, flat base”:
  - Produces at least one manifold STL within 2 iterations (≤ 6 candidates total).
  - Best candidate reaches `target_score ≥ 75` under the rubric.
  - Artifacts and `result.json` are written as specified.
- Repairs: If a slicer/validator error occurs, an automatic repair pass is attempted exactly once before giving up the iteration.

---

## Risks and Mitigations
- Vision subjectivity: use multi‑criteria rubric and combine with geometry metrics.
- OpenSCAD limits for organic shapes: add CadQuery backend and favor it for curvy tasks.
- API variability/cost: cap budgets, stream logs, cache intermediate renders.
- Performance: parallelize candidate rendering/evaluation per iteration with a process pool.

---

## Next Steps (Agent Plan to Implement)
1) Scaffolding
   - Create `AdvancedModeling.py` with API skeleton, data classes, and config adapter.
   - Create `Test_AdvancedModeling.py` CLI harness and `out/` workspace management.
2) Providers
   - Implement OpenSCAD generator (LLM → SCAD) reusing Replicator prompts.
   - Implement CadQuery generator (LLM → CQ Python) with safe template.
   - Implement renderer/exporter for both backends; write multi‑view PNGs.
3) Evaluation
   - Add `trimesh` validations and normalized `geo` score.
   - Implement `VisionRater` adapter with JSON rubric; support OpenAI‑compatible API.
   - Combine into final score; serialize `metrics.json` per candidate.
4) Iteration
   - Implement mutation operators and critique‑driven edit prompts.
   - Add syntax/printability repair passes using existing Replicator helpers.
   - Add beam search loop with configurable `beam_width`.
5) Learning
   - Introduce SQLite experience store and simple retrieval for prompt scaffolds.
   - Track operator win‑rates and update priors across runs.
6) Integration
   - Mirror best artifacts into Replicator’s `generated/` and `stl/` for easy printing.
   - Optional: add a new Replicator UI button to invoke AdvancedModeling.

---

## Open Questions
- Preferred VLM provider and model for rating? (JSON reliability, latency, cost)
- Do we standardize on CadQuery for chess‑like/organic forms by default?
- How strict should the geometry penalties be for early exploration vs. late refinement?
- What are safe default budgets for a good developer experience?

---

## Runbook (for the test harness once implemented)
- Ensure `OPENAI_API_KEY` (or configured env var) is set.
- Confirm `openscad.exe` path in `replicator.json` Settings → Paths or edit file.
- Run the test harness with a small budget and inspect the `out/` directory.
- Open the mirrored `stl/` in OrcaSlicer if desired to validate printability.

End of spec.
