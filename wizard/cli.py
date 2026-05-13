"""
Scaffold generator for new STT models.

Creates:
  - models/<name>/model.yaml      (config from template)
  - model_repository/adapters/<adapter>.py  (adapter skeleton)

Usage:
    # Interactive wizard (recommended):
    python -m wizard.cli
    or
    python -m wizard.cli --lang ru

    # Non-interactive:
    python -m wizard.cli --name whisper-large --adapter whisper
"""

import argparse
from dataclasses import dataclass
import locale
import os
import re
import sys
import logging
import structlog
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.rule import Rule
from rich import box
from rich.text import Text
from rich.tree import Tree

from .i18n import I18N
import yaml


ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
ADAPTERS_DIR = ROOT / "model_repository" / "adapters"
TEMPLATES_DIR = ROOT / "wizard" / "templates"
LANGUAGE_CHOICES = {
    "ru": "Русский",
    "en": "English",
}
CURRENT_LANG = "en"

console = Console()

LOGGING_LEVEL = logging.INFO
log = structlog.get_logger()


@dataclass
class ModelParameters:
    name: str
    adapter: str
    id: str
    description: str
    language_code: str
    sample_rate: int


@dataclass
class ScaffoldResult:
    model_params: ModelParameters
    model_dir_created: bool = False
    yaml_created: bool = False
    weights_dir_created: bool = False
    requirements_lock_created: bool = False
    system_packages_created: bool = False
    adapter_created: bool = False


def _setup_logging() -> None:
    """
    Configures structlog to output to stdout.
    """
    # -- Shared processors (run for every log event, before formatting) -------
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.format_exc_info,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.UnicodeDecoder(),
    ]

    root_logger = logging.getLogger("wizard")
    root_logger.handlers.clear()
    root_logger.setLevel(LOGGING_LEVEL)

    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(),
        foreign_pre_chain=shared_processors,
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def _t(key: str, **kwargs) -> str:
    fallback_language = "en"
    if CURRENT_LANG not in I18N:
        log.warning(
            f"Language '{CURRENT_LANG}' not found in I18N, falling back to {LANGUAGE_CHOICES[fallback_language]}"
        )
    lang_dict = I18N.get(CURRENT_LANG, I18N[fallback_language])
    if key not in lang_dict:
        log.warning(f"Key '{key}' not found in language '{CURRENT_LANG}'")
    template = lang_dict.get(key, key)

    return template.format(**kwargs)


def _detect_system_language() -> str:
    candidates = [
        locale.getlocale()[0],
        os.environ.get("LC_ALL"),
        os.environ.get("LANG"),
        os.environ.get("LANGUAGE"),
    ]
    for candidate in candidates:
        for lang_code in LANGUAGE_CHOICES.keys():
            if candidate and candidate.lower().strip().startswith(lang_code):
                log.info(f"Detected system language: {candidate}")
                return lang_code
    log.warning("Detected system language not found in environment")
    return "en"


def _load_template(name: str) -> str:
    path = TEMPLATES_DIR / name
    if not path.exists():
        log.error("Template not found", path=path)
        sys.exit(1)
    log.debug("Loaded template", path=path)
    return path.read_text(encoding="utf-8")


def _discover_existing_adapters() -> list[str]:
    adapters = []
    if ADAPTERS_DIR.exists():
        for f in sorted(ADAPTERS_DIR.iterdir()):
            if f.suffix == ".py" and f.stem not in ("__init__",):
                adapters.append(f.stem)
    return adapters


def _read_model_parameters(yaml_path: Path) -> ModelParameters:
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid model config: {yaml_path}")
    model = ModelParameters(
        name=yaml_path.parent.name,
        adapter=str(raw.get("adapter", "")),
        id=str(raw.get("id", "")),
        description=str(raw.get("description", "")),
        language_code=str(raw.get("language_code", "")),
        sample_rate=int(raw.get("sample_rate", "")),
    )
    return model


def _discover_existing_models() -> list[ModelParameters]:
    models = []
    if MODELS_DIR.exists():
        for d in sorted(MODELS_DIR.iterdir()):
            yaml_path = d / "model.yaml"
            if d.is_dir() and yaml_path.exists():
                try:
                    model = _read_model_parameters(yaml_path)
                except ValueError:
                    log.warning("Skipping invalid model config", path=yaml_path)
                    continue
                models.append(model)
    return models


def _is_valid_model_name(name: str) -> bool:
    """
    Valid names contain only lowercase letters, numbers, hyphens, and underscores.
    Must not be empty.
    """
    if not name:
        return False
    return bool(re.match(r"^[a-z0-9_-]+$", name))


def _to_class_name(adapter: str) -> str:
    """whisper_large → WhisperLargeAdapter"""
    parts = adapter.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts) + "Adapter"


def _interactive_wizard() -> ModelParameters:
    console.print()
    console.print(
        Panel(
            f"{_t('wizard_title')}\n{_t('wizard_subtitle')}",
            border_style="bright_cyan",
            padding=(1, 2),
        )
    )
    console.print()
    # ── Show existing models/adapters ────────────────────────────────
    existing_models = _discover_existing_models()
    existing_adapters = _discover_existing_adapters()

    if existing_models:
        table = Table(
            title=_t("existing_models_title"),
            box=box.SIMPLE,
            show_edge=False,
            title_style="dim",
            header_style="dim bold",
            padding=(0, 1),
        )
        table.add_column(_t("model_col"), style="cyan")
        table.add_column(_t("model_dir_col"), style="dim")
        table.add_column(_t("adapter_col"), style="dim")
        for model in existing_models:
            adapter = (
                model.adapter if model.adapter in existing_adapters else _t("missing")
            )
            table.add_row(model.id, f"models/{model.name}/", adapter)
        console.print(table)
        console.print()

    # ── Model name ───────────────────────────────────────────────────
    while True:
        model_name = Prompt.ask(_t("prompt_model_name")).strip()
        if not model_name:
            console.print(_t("model_required"))
            continue
        if not _is_valid_model_name(model_name):
            console.print(_t("model_invalid"))
            continue
        if model_name in [m.name for m in existing_models]:
            console.print(_t("model_exists", model=model_name))
            continue
        break

    # ── Adapter name ─────────────────────────────────────────────────
    default_adapter = model_name

    if existing_adapters:
        console.print()
        adapter_list = ", ".join(f"[cyan]{a}[/cyan]" for a in existing_adapters)
        console.print(_t("existing_adapters", adapters=adapter_list))

    # Set up tab-completion for adapters if readline is available (Linux/Mac/WSL)
    try:
        import readline

        def completer(text, state):
            options = [x for x in existing_adapters if x.startswith(text)]
            return options[state] if state < len(options) else None

        readline.set_completer(completer)
        if "libedit" in getattr(readline, "__doc__", ""):
            readline.parse_and_bind("bind ^I rl_complete")  # macOS
        else:
            readline.parse_and_bind("tab: complete")  # Linux/WSL
    except ImportError:
        log.info(
            "Tab-completion is not available on this platform", platform=sys.platform
        )

    while True:
        adapter_name = Prompt.ask(_t("prompt_adapter"), default=default_adapter).strip()
        if not adapter_name:
            console.print(_t("model_required"))
            continue
        if not _is_valid_model_name(adapter_name):
            console.print(_t("model_invalid"))
            continue
        break

    # Disable completion so it doesn't affect subsequent prompts
    try:
        import readline

        readline.set_completer(None)
    except ImportError:
        pass

    reuse_adapter = adapter_name in existing_adapters
    if reuse_adapter:
        console.print(_t("reuse_adapter", adapter=adapter_name))
    else:
        console.print(_t("will_create_adapter", adapter=adapter_name))

    # ── Model ID ─────────────────────────────────────────────────────
    while True:
        model_id = Prompt.ask(_t("prompt_model_id"), default=model_name).strip()
        if not model_id:
            console.print(_t("model_required"))
            continue
        if not _is_valid_model_name(model_id):
            console.print(_t("model_invalid"))
            continue
        if model_id in [m.id for m in existing_models]:
            console.print(_t("model_exists", model=model_id))
            continue
        break

    # ── Description ──────────────────────────────────────────────────
    description = Prompt.ask(
        _t("prompt_description"), default="TODO: describe your model"
    ).strip()

    # ── Language ─────────────────────────────────────────────────────
    language = Prompt.ask(_t("prompt_language"), default="en").strip()

    # ── Sample rate ──────────────────────────────────────────────────
    while True:
        try:
            sample_rate = int(
                Prompt.ask(_t("prompt_sample_rate"), default="16000").strip()
            )
            if sample_rate <= 0:
                raise ValueError()
            break
        except ValueError:
            console.print(_t("sample_rate_invalid"))
            continue

    # ── Confirmation ─────────────────────────────────────────────────
    console.print()
    console.print(Rule(_t("review"), style="bright_cyan"))
    console.print()
    console.print(_t("will_create_model", model=model_name))
    model_summary = Table(box=box.ROUNDED, border_style="bright_cyan", padding=(0, 1))
    model_summary.add_column(_t("param"), style="bold")
    model_summary.add_column(_t("value"), style="cyan")
    model_summary.add_row(_t("p_model_name"), model_name)
    model_summary.add_row(
        _t("p_adapter"),
        adapter_name
        + (
            f" [dim]({_t('existing')})[/dim]"
            if reuse_adapter
            else f" [dim]({_t('new')})[/dim]"
        ),
    )
    model_summary.add_row(_t("p_model_id"), model_id)
    model_summary.add_row(_t("p_description"), description)
    model_summary.add_row(
        _t("p_language"), f"{language} ({LANGUAGE_CHOICES.get(language, '?')})"
    )
    model_summary.add_row(_t("p_sample_rate"), f"{sample_rate} Hz")
    console.print(model_summary)
    console.print()
    if not reuse_adapter:
        console.print(_t("will_create_adapter", adapter=adapter_name))
        adapter_summary = Table(
            box=box.ROUNDED, border_style="bright_cyan", padding=(0, 1)
        )
        adapter_summary.add_column(_t("param"), style="bold")
        adapter_summary.add_column(_t("value"), style="cyan")
        adapter_summary.add_row(_t("p_adapter"), adapter_name)
        adapter_summary.add_row(_t("p_class"), _to_class_name(adapter_name))
        console.print(adapter_summary)
        console.print()

    if not Confirm.ask(_t("proceed"), default=True):
        console.print(_t("aborted"))
        sys.exit(0)

    return ModelParameters(
        name=model_name,
        adapter=adapter_name,
        id=model_id,
        description=description,
        language_code=language,
        sample_rate=sample_rate,
    )


def _scaffold(params: ModelParameters) -> ScaffoldResult:
    class_name = _to_class_name(params.adapter)
    model_dir = MODELS_DIR / params.name
    adapter_file = ADAPTERS_DIR / f"{params.adapter}.py"
    adapters_init = ADAPTERS_DIR / "__init__.py"
    yaml_path = model_dir / "model.yaml"
    requirements_lock_path = model_dir / "requirements.lock"
    system_packages_path = model_dir / "system-packages.txt"

    results = ScaffoldResult(model_params=params)

    if not model_dir.exists():
        model_dir.mkdir(parents=True)
        results.model_dir_created = True

    ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    if not adapters_init.exists():
        adapters_init.write_text('"""Model adapters package."""\n', encoding="utf-8")

    if not (model_dir / "weights").exists():
        (model_dir / "weights").mkdir()
        results.weights_dir_created = True

    if not yaml_path.exists():
        template = _load_template("model.yaml.tpl")
        content = template.format(
            name=params.name,
            adapter=params.adapter,
            id=params.id,
            description=params.description,
            language_code=params.language_code,
            sample_rate=params.sample_rate,
        )
        yaml_path.write_text(content, encoding="utf-8")
        results.yaml_created = True
    else:
        try:
            results.model_params = _read_model_parameters(yaml_path)
        except ValueError:
            log.error("Existing model.yaml is invalid", path=yaml_path)
            sys.exit(1)

    if not requirements_lock_path.exists():
        requirements_lock_path.write_text(
            "# Pin model runtime dependencies here.\n"
            "# Example:\n"
            "# torch==2.8.0\n"
            "# transformers==4.56.2\n",
            encoding="utf-8",
        )
        results.requirements_lock_created = True

    if not system_packages_path.exists():
        system_packages_path.write_text(
            "# Optional OS packages, one per line.\n"
            "# Example:\n"
            "# ffmpeg\n"
            "# libsndfile1\n",
            encoding="utf-8",
        )
        results.system_packages_created = True

    if not adapter_file.exists():
        template = _load_template("adapter.py.tpl")
        content = template.format(
            adapter=params.adapter,
            class_name=class_name,
            model_id=params.id,
            sample_rate=params.sample_rate,
        )
        adapter_file.write_text(content, encoding="utf-8")
        results.adapter_created = True

    return results


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _print_result(result: ScaffoldResult) -> None:
    console.print()

    # ── File tree ────────────────────────────────────────────────────
    tree = Tree(
        f"[bold bright_cyan]📁 {_relative(ROOT)}[/bold bright_cyan]",
        guide_style="dim",
    )

    models_branch = tree.add("[bold]models/[/bold]")
    model_branch = models_branch.add(
        f"[bold cyan]{result.model_params.name}/[/bold cyan]"
    )

    if result.model_dir_created:
        model_branch.add(
            f"[green]✓[/green] [bold]model.yaml[/bold]  [dim]- {_t('config')}[/dim]"
        )
        model_branch.add(
            "[green]✓[/green] [bold]requirements.lock[/bold]  "
            f"[dim]- {_t('python_deps')}[/dim]"
        )
        model_branch.add(
            "[green]✓[/green] [bold]system-packages.txt[/bold]  "
            f"[dim]- {_t('system_deps')}[/dim]"
        )
        model_branch.add(
            f"[green]✓[/green] [dim]weights/[/dim]  [dim]- {_t('place_weights')}[/dim]"
        )
    else:
        if result.yaml_created:
            model_branch.add(
                f"[green]✓[/green] [bold]model.yaml[/bold]  [dim]- {_t('config')}[/dim]"
            )
        else:
            model_branch.add(
                f"[yellow]⚠[/yellow] [bold]model.yaml[/bold]  [dim]- {_t('already_exists')}[/dim]"
            )
        if result.requirements_lock_created:
            model_branch.add(
                "[green]✓[/green] [bold]requirements.lock[/bold]  "
                f"[dim]- {_t('python_deps')}[/dim]"
            )
        else:
            model_branch.add(
                "[yellow]⚠[/yellow] [bold]requirements.lock[/bold]  "
                f"[dim]- {_t('already_exists')}[/dim]"
            )
        if result.system_packages_created:
            model_branch.add(
                "[green]✓[/green] [bold]system-packages.txt[/bold]  "
                f"[dim]- {_t('system_deps')}[/dim]"
            )
        else:
            model_branch.add(
                "[yellow]⚠[/yellow] [bold]system-packages.txt[/bold]  "
                f"[dim]- {_t('already_exists')}[/dim]"
            )
        if result.weights_dir_created:
            model_branch.add(
                f"[green]✓[/green] [dim]weights/[/dim]  [dim]- {_t('place_weights')}[/dim]"
            )
        else:
            model_branch.add(
                f"[yellow]⚠[/yellow] [dim]weights/[/dim]  [dim]- {_t('already_exists')}[/dim]"
            )

    model_repo_branch = tree.add("[bold]model_repository/adapters/[/bold]")
    adapter_name = result.model_params.adapter
    adapter_class = _to_class_name(adapter_name)
    if result.adapter_created:
        model_repo_branch.add(
            f"[green]✓[/green] [bold]{adapter_name}[/bold]  "
            f"[dim]- {adapter_class}[/dim]"
        )
    else:
        model_repo_branch.add(
            f"[yellow]⚠[/yellow] [bold]{adapter_name}[/bold]  "
            f"[dim]- {_t('reusing')}[/dim]"
        )

    console.print(
        Panel(tree, title=_t("created_files"), border_style="green", padding=(1, 2))
    )

    # ── Preview ModelParameters ──────────────────────────────────────
    model_parameters = getattr(result, "model_parameters", result.model_params)
    console.print()
    params_table = Table(box=box.ROUNDED, border_style="bright_cyan", padding=(0, 1))
    params_table.add_column("field", style="bold")
    params_table.add_column("value", style="cyan")
    params_table.add_row("name", model_parameters.name)
    params_table.add_row("adapter", model_parameters.adapter)
    params_table.add_row("id", model_parameters.id)
    params_table.add_row("description", model_parameters.description)
    params_table.add_row("language_code", model_parameters.language_code)
    params_table.add_row("sample_rate", str(model_parameters.sample_rate))
    console.print(
        Panel(
            params_table,
            title="[bold]ModelParameters[/bold]",
            border_style="bright_cyan",
            padding=(1, 1),
        )
    )

    # ── Next steps ───────────────────────────────────────────────────
    console.print()
    model_dir = MODELS_DIR / result.model_params.name
    adapter_file = ADAPTERS_DIR / f"{adapter_name}.py"
    yaml_path = model_dir / "model.yaml"

    yaml_rel = _relative(yaml_path)
    adapter_rel = _relative(adapter_file)
    weights_rel = _relative(model_dir / "weights")

    steps = Text()
    steps.append("  1. ", style="bold cyan")
    steps.append(_t("step1", yaml_rel=yaml_rel), style="bold")
    steps.append(_t("step1_desc"), style="dim")
    steps.append("\n")

    steps.append("  2. ", style="bold cyan")
    steps.append(_t("step2", adapter_rel=adapter_rel), style="bold")
    steps.append(_t("step2_desc"), style="dim")
    steps.append("\n")

    steps.append("  3. ", style="bold cyan")
    steps.append(_t("step3", weights_rel=weights_rel), style="bold")
    steps.append("\n")

    steps.append("  4. ", style="bold cyan")
    steps.append(_t("step4", model_name=result.model_params.name), style="bold white")
    steps.append(_t("step4_desc", model_name=result.model_params.name), style="dim")
    steps.append("\n")

    steps.append("  5. ", style="bold cyan")
    steps.append(_t("step5", model_name=result.model_params.name), style="bold white")
    steps.append(_t("step5_desc", model_name=result.model_params.name), style="dim")

    console.print(
        Panel(
            steps,
            title=_t("next_steps"),
            border_style="bright_yellow",
            padding=(1, 2),
        )
    )
    console.print()


def main():
    global CURRENT_LANG

    _setup_logging()

    parser = argparse.ArgumentParser(
        description="Scaffold a new STT model",
        epilog="Run without --name argument for interactive wizard mode.",
    )
    parser.add_argument(
        "--name", default=None, help="Model directory name (e.g. whisper-large)"
    )
    parser.add_argument(
        "--adapter", default=None, help="Adapter module name (e.g. whisper)"
    )
    parser.add_argument(
        "--lang",
        default="auto",
        choices=["auto", "ru", "en"],
        help="UI language: auto (system), ru, en",
    )
    args = parser.parse_args()

    CURRENT_LANG = _detect_system_language() if args.lang == "auto" else args.lang
    if args.name:
        if not args.adapter:
            args.adapter = args.name.strip().lower().replace(" ", "_").replace("-", "_")
        params = ModelParameters(
            name=args.name,
            adapter=args.adapter,
            id=args.name,
            description="TODO: describe your model",
            language_code="en",
            sample_rate=16000,
        )
    else:
        params = _interactive_wizard()

    result = _scaffold(params)
    _print_result(result)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print()
        console.print(_t("aborted"))
        log.error("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        console.print()
        console.print(_t("aborted"))
        log.error("Unexpected error occurred", error=str(e), exc_info=True)
        sys.exit(1)
