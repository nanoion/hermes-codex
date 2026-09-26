#!/usr/bin/env python3
"""Apply the Hermes 0.21.3 plugin-command session-context fix safely."""
from __future__ import annotations

import argparse
import py_compile
import shutil
from pathlib import Path


def replace_once(path: Path, old: str, new: str, *, applied_marker: str | None = None) -> bool:
    text = path.read_text(encoding="utf-8")
    if new in text or applied_marker is not None and applied_marker in text:
        return False
    if text.count(old) != 1:
        raise RuntimeError(f"Unsupported Hermes source at {path}; expected patch anchor exactly once")
    backup = path.with_suffix(path.suffix + ".hermes-codex.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text.replace(old, new), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hermes-source",
        type=Path,
        default=Path.home() / ".hermes" / "hermes-agent",
        help="Hermes Agent source directory",
    )
    args = parser.parse_args()
    root = args.hermes_source.resolve()
    inbound = root / "gateway" / "run_inbound.py"
    run = root / "gateway" / "run.py"
    if not inbound.is_file() or not run.is_file():
        raise SystemExit(f"Hermes source not found at {root}")

    changed = False
    changed |= replace_once(
        run,
        "            session_key=context.session_key,\n"
        "            message_id=str(context.source.message_id) if context.source.message_id else \"\",",
        "            session_key=context.session_key,\n"
        "            session_id=context.session_id,\n"
        "            message_id=str(context.source.message_id) if context.source.message_id else \"\",",
    )
    changed |= replace_once(
        inbound,
        "    async def _hm_dispatch_quick_and_plugin_commands(\n"
        "        self, event: \"MessageEvent\", source: SessionSource, command: Optional[str]\n"
        "    ) -> Tuple[bool, Optional[str], Optional[str]]:",
        "    async def _hm_dispatch_quick_and_plugin_commands(\n"
        "        self, event: \"MessageEvent\", source: SessionSource, command: Optional[str], session_key: str,\n"
        "    ) -> Tuple[bool, Optional[str], Optional[str]]:",
    )
    changed |= replace_once(
        inbound,
        "                if plugin_handler:\n"
        "                    result = plugin_handler(event.get_command_args().strip())\n"
        "                    if asyncio.iscoroutine(result):\n"
        "                        result = await result\n"
        "                    return True, str(result) if result else None, command",
        "                if plugin_handler:\n"
        "                    # Bind the authenticated gateway identity around direct plugin commands.\n"
        "                    from gateway.session import build_session_context\n"
        "                    session_store = getattr(self, \"session_store\")\n"
        "                    session_entry = session_store.lookup_by_session_key(session_key)\n"
        "                    context = build_session_context(source, getattr(self, \"config\"), session_entry)\n"
        "                    tokens = getattr(self, \"_set_session_env\")(context)\n"
        "                    try:\n"
        "                        result = plugin_handler(event.get_command_args().strip())\n"
        "                        if asyncio.iscoroutine(result):\n"
        "                            result = await result\n"
        "                    finally:\n"
        "                        getattr(self, \"_clear_session_env\")(tokens)\n"
        "                    return True, str(result) if result else None, command",
        applied_marker="session_store.lookup_by_session_key(session_key)",
    )
    changed |= replace_once(
        inbound,
        "            _handled, _result, command = await self._hm_dispatch_quick_and_plugin_commands(event, source, command)",
        "            _handled, _result, command = await self._hm_dispatch_quick_and_plugin_commands(\n"
        "                event, source, command, _quick_key)",
    )

    py_compile.compile(str(run), doraise=True)
    py_compile.compile(str(inbound), doraise=True)
    print("Hermes session-context patch applied." if changed else "Hermes session-context patch already present.")
    print("Restart affected Hermes gateways after installation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
