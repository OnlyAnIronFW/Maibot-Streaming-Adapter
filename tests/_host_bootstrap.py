import atexit
import importlib.util
import os
import sys
import time

from pathlib import Path


_TESTS_DIR = Path(__file__).resolve().parent
_PLUGINS_DIR = _TESTS_DIR.parents[1]
_WORKSPACE_DIR = _TESTS_DIR.parents[3]
# 宿主核心仓库位置：优先取环境变量 MAIBOT_CORE_REPO，便于在非本地环境指向自己的 MaiBot 主仓。
_CORE_REPO_DIR = Path(os.environ.get("MAIBOT_CORE_REPO") or _WORKSPACE_DIR / "MaiBot-r-dev")
_CREATED_CONFIGS: list[Path] = []
_BOOTSTRAPPED = False
_PROCESS_STARTED_AT = time.time()


def ensure_host_test_environment() -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return

    for candidate in (_PLUGINS_DIR, _CORE_REPO_DIR):
        candidate_text = str(candidate)
        if candidate.exists() and candidate_text not in sys.path:
            sys.path.insert(0, candidate_text)

    _ensure_core_config_files()
    _BOOTSTRAPPED = True


def _ensure_core_config_files() -> None:
    bootstrap = _load_config_bootstrap_module()
    config_dir = _CORE_REPO_DIR / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    bot_config_path = config_dir / "bot_config.toml"
    if not bot_config_path.exists():
        bootstrap.generate_new_config_file(bootstrap.Config, bot_config_path, bootstrap.CONFIG_VERSION)
        _CREATED_CONFIGS.append(bot_config_path)

    model_config_path = config_dir / "model_config.toml"
    if model_config_path.exists():
        return

    from src.config.model_configs import TaskConfig

    provider = bootstrap.APIProvider(
        name="test-provider",
        base_url="http://127.0.0.1:1/v1",
        api_key="",
        auth_type="none",
    )
    model = bootstrap.ModelInfo(
        model_identifier="test-model",
        name="test-model",
        api_provider="test-provider",
    )
    task = TaskConfig(model_list=["test-model"])
    task_updates = {
        field_name: task
        for field_name in bootstrap.ModelTaskConfig.model_fields.keys()
        if field_name not in {"field_docs", "_validate_any", "suppress_any_warning"}
    }
    model_task_config = bootstrap.ModelTaskConfig(**task_updates)
    model_config = bootstrap.ModelConfig(
        models=[model],
        api_providers=[provider],
        model_task_config=model_task_config,
    )
    bootstrap.write_config_to_file(model_config, model_config_path, bootstrap.MODEL_CONFIG_VERSION, True)
    _CREATED_CONFIGS.append(model_config_path)


def _load_config_bootstrap_module():
    module_name = "src.config.config_test_bootstrap"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    config_py = _CORE_REPO_DIR / "src" / "config" / "config.py"
    source = config_py.read_text(encoding="utf-8-sig")
    marker = "\nconfig_manager = ConfigManager()"
    if marker not in source:
        raise RuntimeError("config bootstrap marker not found")

    bootstrap = source.split(marker, 1)[0]
    spec = importlib.util.spec_from_loader(module_name, loader=None, origin=str(config_py))
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "src.config"
    module.__file__ = str(config_py)
    sys.modules[module_name] = module
    exec(compile(bootstrap, str(config_py), "exec"), module.__dict__)
    return module


@atexit.register
def _cleanup_created_configs() -> None:
    for path in reversed(_CREATED_CONFIGS):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
    old_dir = _CORE_REPO_DIR / "config" / "old"
    if old_dir.exists():
        for pattern in ("bot_config_*.toml", "model_config_*.toml"):
            for backup in old_dir.glob(pattern):
                try:
                    if backup.stat().st_mtime >= (_PROCESS_STARTED_AT - 1.0):
                        backup.unlink()
                except FileNotFoundError:
                    continue
        try:
            next(old_dir.iterdir())
        except StopIteration:
            old_dir.rmdir()


ensure_host_test_environment()
