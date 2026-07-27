from argparse import ArgumentParser
from pathlib import Path
import os
import sys

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "app"))

from config import Settings


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = ArgumentParser(
        description="Download the Docling Standard PDF pipeline models.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_default_output_dir(),
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    settings = Settings()

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _configure_runtime_paths(output_dir)

    from docling.datamodel.pipeline_options import LayoutOptions
    from docling.models.stages.layout.layout_model import LayoutModel
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel
    from docling.models.stages.table_structure.table_structure_model import (
        TableStructureModel,
    )
    from huggingface_hub import snapshot_download

    layout_options = LayoutOptions()
    layout_options.model_spec = layout_options.model_spec.model_copy(
        update={"revision": settings.docling_layout_revision},
    )
    LayoutModel.download_models(
        local_dir=output_dir / layout_options.model_spec.model_repo_folder,
        force=args.force,
        progress=True,
        layout_model_config=layout_options.model_spec,
    )
    snapshot_download(
        repo_id="docling-project/docling-models",
        revision=settings.docling_table_revision,
        local_dir=output_dir / TableStructureModel._model_repo_folder,
        allow_patterns=["model_artifacts/tableformer/accurate/*"],
        force_download=args.force,
    )
    RapidOcrModel.download_models(
        backend="torch",
        lang="chinese",
        local_dir=output_dir / RapidOcrModel._model_repo_folder,
        force=args.force,
        progress=True,
    )
    from parsers.docling_parser import _validated_docling_artifacts_path

    _validated_docling_artifacts_path(str(output_dir))
    print(f"Docling models ready: {output_dir}")
    return 0


def _default_output_dir() -> Path:
    configured = os.getenv("REVIEW_AGENT_DOCLING_ARTIFACTS_PATH", "").strip()
    if configured:
        return Path(configured)
    runtime_root = os.getenv("REVIEW_AGENT_RUNTIME_ROOT", "").strip()
    if runtime_root:
        return Path(runtime_root) / "models" / "docling"
    raise RuntimeError(
        "Set REVIEW_AGENT_RUNTIME_ROOT, REVIEW_AGENT_DOCLING_ARTIFACTS_PATH, "
        "or pass --output-dir."
    )


def _configure_runtime_paths(output_dir: Path) -> None:
    configured_root = os.getenv("REVIEW_AGENT_RUNTIME_ROOT", "").strip()
    runtime_root = (
        Path(configured_root).expanduser().resolve()
        if configured_root
        else output_dir.parent.parent
    )
    cache_root = runtime_root / "cache" / "huggingface"
    temp_root = runtime_root / "temp"
    cache_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(cache_root))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(cache_root / "hub"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "600")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
    os.environ.setdefault("TEMP", str(temp_root))
    os.environ.setdefault("TMP", str(temp_root))


if __name__ == "__main__":
    raise SystemExit(main())
