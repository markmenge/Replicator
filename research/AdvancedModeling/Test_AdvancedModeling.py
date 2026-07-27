#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from replicator_config import CONFIG_PATH, load_config
from research.AdvancedModeling.AdvancedModeling import AdvancedModeler, Constraints, Budget, Engines


def main() -> int:
    ap = argparse.ArgumentParser(description="Test harness for AdvancedModeling")
    ap.add_argument("--desc", required=True, help="Model description to generate")
    ap.add_argument("--image", action="append", default=[], help="Reference image path (repeatable)")
    ap.add_argument("--max-iters", type=int, default=1)
    ap.add_argument("--candidates", type=int, default=2)
    ap.add_argument("--target", type=float, default=70.0)
    args = ap.parse_args()

    ref_images = [Path(p) for p in args.image]

    cfg = load_config(CONFIG_PATH)
    am = AdvancedModeler(cfg)

    result = am.generate(
        description=args.desc,
        ref_images=ref_images,
        constraints=Constraints(),
        budget=Budget(max_iters=args.max_iters, candidates_per_iter=args.candidates, target_score=args.target),
        engines=Engines(),
    )

    # Print concise summary and where artifacts live
    print("Best candidate:")
    print(json.dumps({
        "id": result.best.id,
        "score": result.best.score,
        "engine": result.best.engine,
        "source": str(result.best.source_path),
        "stl": str(result.best.stl_path) if result.best.stl_path else None,
        "views": [str(v) for v in result.best.views],
        "work_dir": str(result.work_dir),
    }, indent=2))

    print("\nArtifacts written under:", result.work_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
