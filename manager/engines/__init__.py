"""Engine plugins. Add a module here and register its class in PLUGINS to support a new engine."""
from .base import Engine, Estimate, Launch, Profile
from .command import CommandEngine
from .llamacpp import LlamaCppEngine
from .mlx import MlxEngine
from .maya import MayaEngine
from .strata import StrataEngine

PLUGINS: dict[str, type[Engine]] = {
    "llamacpp": LlamaCppEngine,
    "strata": StrataEngine,
    "maya": MayaEngine,
    "mlx": MlxEngine,
    "command": CommandEngine,
}


def create(name: str, cfg: dict) -> Engine:
    plugin = cfg.get("plugin", name)
    if plugin not in PLUGINS:
        raise ValueError(f"engine {name}: unknown plugin {plugin!r} (known: {', '.join(PLUGINS)})")
    return PLUGINS[plugin](name, cfg)


__all__ = ["Engine", "Estimate", "Launch", "Profile", "PLUGINS", "create"]
