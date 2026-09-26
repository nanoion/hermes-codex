"""Hermes-managed Codex workers."""

__all__ = ['Service', 'register']


def __getattr__(name):
    # State-only processes must not initialize the SDK's generated models.
    if name == 'Service':
        from .service import Service
        return Service
    if name == 'register':
        from .plugin import register
        return register
    if name == 'presentation':
        from importlib import import_module
        return import_module('.presentation', __name__)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
