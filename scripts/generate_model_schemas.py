from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ascend_math_agent.schema_artifacts import generated_model_schemas  # noqa: E402


def main() -> None:
    schema_dir = ROOT / "resources" / "schemas"
    for filename, schema in generated_model_schemas().items():
        rendered = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (schema_dir / filename).write_text(rendered, encoding="utf-8")
    print("Generated strict model-output schemas.")


if __name__ == "__main__":
    main()
