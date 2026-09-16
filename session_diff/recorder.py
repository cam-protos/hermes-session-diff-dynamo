"""Tool middleware. Capture failures never retry or change the actual tool call."""

import logging
import re
import threading
import time
import uuid
from pathlib import Path

from .capture import snapshot, workspace
from .store import Store, UNKNOWN

log = logging.getLogger(__name__)
FILE_TOOLS = {"write_file", "patch", "apply_patch"}
WORKSPACE_TOOLS = {"terminal", "execute_code"}


class HermesPaths:
    """Read the existing task-aware resolver; no replacement tools or core patches."""

    def __init__(self, task_id):
        from tools import terminal_tool
        from tools.environments.local import LocalEnvironment
        from tools.file_tools_paths import _resolve_path_for_task
        from agent.file_safety import get_read_block_error

        config = terminal_tool._get_env_config()
        overrides = terminal_tool.resolve_task_overrides(task_id)
        backend = overrides.get("env_type") or config["env_type"]
        key = terminal_tool._resolve_container_task_id(task_id)
        with terminal_tool._env_lock:
            active = terminal_tool._active_environments.get(key) or terminal_tool._active_environments.get(task_id)
        if backend != "local" or (active is not None and not isinstance(active, LocalEnvironment)):
            raise RuntimeError(f"Content capture is unavailable for the {backend} terminal backend. No host files were read.")
        self.task_id = task_id
        self.resolve = lambda path: Path(_resolve_path_for_task(str(path), task_id))
        self.blocked = get_read_block_error
        self.root = self.resolve(".")


def file_targets(args):
    targets = []
    if isinstance(args.get("path"), str):
        targets.append(args["path"])
    patch = args.get("patch", args.get("input", ""))
    if isinstance(patch, str):
        for line in patch.splitlines():
            header = re.match(r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$", line)
            move = re.match(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", line)
            move_to = re.match(r"^\*\*\*\s*Move to:\s*(.+)$", line)
            if header:
                targets.append(header[1])
            if move:
                targets.extend([move[1], move[2]])
            if move_to:
                targets.append(move_to[1])
    return list(dict.fromkeys(targets))[:100]


class Recorder:
    def __init__(self, terminal_capture=True, retention_days=30, store_factory=Store, paths_factory=HermesPaths):
        self.terminal_capture = terminal_capture is not False
        self.retention_days = retention_days
        self.store_factory, self.paths_factory = store_factory, paths_factory
        self.runs = {}
        self.lock = threading.Lock()
        self.last_prune = 0

    def begin_run(self, session_id="", **kwargs):
        if session_id:
            with self.lock:
                # Hooks define user runs; provider requests may have many different IDs.
                self.runs[str(session_id)] = uuid.uuid4().hex
                if len(self.runs) > 2000:
                    self.runs.pop(next(iter(self.runs)))
                run = self.runs[str(session_id)]
            try:
                self.store_factory().touch(str(session_id), run)
            except Exception:
                log.warning("Session diff could not initialize a run", exc_info=True)

    def execute(self, *, next_call, args, tool_name="", session_id="", task_id="", turn_id="", tool_call_id="", **kwargs):
        if tool_name not in FILE_TOOLS | WORKSPACE_TOOLS or not session_id:
            return next_call(args)
        session = str(session_id)
        with self.lock:
            run = self.runs.setdefault(session, str(turn_id) or uuid.uuid4().hex)
        store = None
        before, blobs, paths, notes, root = {}, {}, None, [], ""
        scan = tool_name in WORKSPACE_TOOLS
        try:
            store = self.store_factory()
            if time.monotonic() - self.last_prune > 3600:
                store.prune(self.retention_days)
                self.last_prune = time.monotonic()
            paths = self.paths_factory(str(task_id or session))
            root = paths.root
            if scan and isinstance(args.get("workdir"), str):
                root = paths.resolve(args["workdir"])
            if scan and not self.terminal_capture:
                notes.append("Terminal workspace capture is disabled in plugin settings.")
            elif scan:
                if not root.is_dir() or root == Path(root.anchor) or root == Path.home():
                    notes.append("Workspace capture is skipped for filesystem roots, the home directory or missing directories.")
                else:
                    before, blobs, warnings = workspace(root, paths.blocked)
                    notes.extend(warnings)
                    notes.append("Terminal changes are observed within the command's workspace and execution interval. External edits may be included; background writes and paths outside that workspace may be missed.")
            else:
                targets = file_targets(args)
                if not targets:
                    notes.append(f"No file paths could be resolved for {tool_name}.")
                for target in targets:
                    path = str(paths.resolve(target))
                    meta, data = snapshot(path, paths.blocked)
                    before[path] = meta
                    if data is not None:
                        blobs[meta["hash"]] = data
            store.touch(session, run, str(root), notes)
        except Exception as exc:
            self._note(store, session, run, f"Capture unavailable: {exc}")
            paths = None

        # Deliberately outside all capture exception handlers. Exactly once.
        try:
            return next_call(args)
        finally:
            if paths is not None:
                try:
                    after, extra = {}, {}
                    if scan and self.terminal_capture and root.is_dir() and root != Path(root.anchor) and root != Path.home():
                        after, extra, warnings = workspace(root, paths.blocked)
                        notes.extend(warnings)
                        # Re-read baseline paths even when enumeration was cut short.
                        # Missing from a limited directory listing does not mean deleted.
                        for path in before:
                            if path not in after:
                                meta, data = snapshot(path, paths.blocked)
                                after[path] = meta
                                if data is not None:
                                    extra[meta["hash"]] = data
                        if any("partial" in n or "limit" in n for n in notes):
                            # Without a full baseline, an unmatched after path may predate the run.
                            after = {p: m for p, m in after.items() if p in before}
                    elif not scan:
                        for path in before:
                            meta, data = snapshot(path, paths.blocked)
                            after[path] = meta
                            if data is not None:
                                extra[meta["hash"]] = data
                    if any(meta["kind"] in UNKNOWN for meta in [*before.values(), *after.values()]):
                        notes.append("Some files were excluded, unreadable, changing during capture or unsupported; their contents were not recorded.")
                    store.record(session, run, str(tool_call_id), tool_name, "observed" if scan else "file tool", before, after, blobs | extra)
                    store.touch(session, run, str(root), notes)
                except Exception as exc:
                    self._note(store, session, run, f"Capture incomplete: {exc}")

    @staticmethod
    def _note(store, session, run, message):
        log.warning("Session diff: %s", message)
        try:
            if store is not None:
                store.touch(session, run, notes=[message])
        except Exception:
            log.warning("Session diff could not save capture status", exc_info=True)
