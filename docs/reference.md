# Reference

## Installation details

The package belongs in your active profile's `$HERMES_HOME/plugins/hermes-session-diff/` directory. Use the home reported by your Hermes installation. The usual defaults are `~/.hermes` on macOS/Linux and `%LOCALAPPDATA%\hermes` on Windows. Custom profiles can use a different home.

The package contains `plugin.yaml`, `__init__.py`, `session_diff/`, `dashboard/`, and `desktop/plugin.js`. Both the Agent recorder and Desktop viewer are required. Installing only `desktop/plugin.js` does not record changes.

For a remote gateway, install and enable the package on that machine. Copy its `desktop/plugin.js` to the Desktop machine's `$HERMES_HOME/desktop-plugins/hermes-session-diff/plugin.js`, then rescan and enable the Desktop component. The viewer uses Hermes's authenticated plugin API. No separate server or port is required.

Hermes preserves manually installed Desktop folders. An old manual copy can take precedence over a package-managed copy; update or remove that old copy when switching installation methods.

The Agent and Desktop switches are separate. Disabling the viewer does not stop recording; disable the Agent plugin as well to stop capturing new changes.

## What is captured

| Operation | Coverage |
| --- | --- |
| `write_file`, `patch`, `apply_patch` | Before and after snapshots of explicit target paths. Supports replacement edits and multi-file patch headers. |
| `terminal`, `execute_code` | A bounded workspace comparison before and after the tool returns. Git ignore rules are respected when Git is available. |
| Failed tool call | Changes that occurred before the failure are still recorded when snapshots are available. The original result or exception is preserved. |
| Changed and restored file | Kept in the list, with intermediate snapshots available in history. |
| Binary, non-UTF-8 or large file | Metadata only. Binary content is not stored. |

Capture runs on the Agent host and currently supports **local terminal execution**, including a local terminal on a remote gateway machine. An SSH connection to a gateway is different from Hermes's `terminal.env: ssh` execution backend. Docker, SSH terminal backends and other sandboxes report unavailable content capture; this release does not read desktop or gateway host files as substitutes for sandbox files.

Terminal entries mean **observed during the command**, not proven authorship. Concurrent editor/session changes may be included. Writes after the tool returns, outside its starting workspace, or through unrecognised tools can be missed. Subagents with separate session IDs have separate journals. A snapshot comparison cannot detect a file that was created and deleted entirely inside a single terminal call.

Explicit file captures also have a small before/after observation interval. They do not lock out other writers. If a file differs between two recorded operations, the viewer flags intervening changes instead of presenting the whole-session diff as exclusively agent-authored.

Capture begins when the plugin is enabled; it cannot reconstruct the original contents of files from earlier runs. If Hermes changes its session ID during compaction, use the session selector to inspect earlier records. Renames are currently shown as a deletion and an addition.

## Storage, limits and settings

The journal lives at `$HERMES_HOME/plugin-data/hermes-session-diff/changes.sqlite3`. It is profile-local and survives restarts. Snapshots can contain source code and other file contents. Nothing is uploaded. Unix installations create the data directory and database with owner-only permissions; Windows uses the directory's inherited permissions.

The recorder excludes common credential filenames, private-key files, dependency/build directories, links, devices, and paths blocked by Hermes's own file-read rules. These filters are not a general secret detector: a token embedded in an ordinary source file will be captured with that file.

Defaults:

- UTF-8 text snapshots up to 512 KiB per file.
- Workspace enumeration up to 3,000 entries, 16 MiB of text, and four seconds, with up to three seconds for Git enumeration. Capture happens on both sides of a command and adds latency.
- 256 MiB of deduplicated text content. When full, recording reports the problem and tool calls continue.
- 30-day retention, pruned at most hourly when a recorded tool runs. Unreferenced blobs are deleted; SQLite may retain freed disk pages for reuse.
- Up to 10,000 change events in one summary, 100 runs in the selector, and 100 operations in a file's history. Summary truncation is labelled.
- Text diff generation up to 12,000 combined lines.

The file/byte/time caps can make workspace coverage partial; the pane labels this. Excluded, unreadable and unstable files are not silently treated as deletions.

Optional `config.yaml` settings:

```yaml
plugins:
  entries:
    hermes-session-diff:
      settings:
        retention_days: 30
        terminal_capture: true
```

Restart the gateway after changing settings. Set `terminal_capture: false` to keep explicit file capture without workspace scans. To erase retained snapshots, stop the gateway and remove only the plugin's `plugin-data/hermes-session-diff` directory.

## Development checks

Run the following with the Python environment from a normal Hermes installation. If needed, set `PYTHONPATH` to its source checkout.

```sh
python tests/integration.py
python scripts/package.py
python tests/integration.py --archive dist/hermes-session-diff-0.1.0.zip
```

The integration check installs into a temporary Hermes home, runs the native validator, loads the real plugin manager, executes file tools, and reads the resulting HTTP diff. It does not modify the active installation.

For browser checks, install Chromium with `npx playwright install chromium`, set `PYTHON` to the Hermes Python executable, then run `npm run test:browser`. An existing browser can be selected with `CHROMIUM_PATH`.

The fixture under `tests/browser/` serves temporary recorded data through the actual API. Checks cover file selection, history, restored files, filtering, clipboard handoff, session switching, error recovery, and light/dark/narrow layouts. Hover contrast is checked with and without the host's interaction tokens. The fixture's SDK adapter does not replace native Electron acceptance testing. Screenshots are written to the ignored `test-results/` directory.

## Runtime structure

`__init__.py` registers one observer and one execution middleware. `session_diff/recorder.py` brackets the existing tool callback and uses Hermes's task-aware file resolver. `capture.py` reads bounded snapshots; `store.py` persists versions and computes diffs. `dashboard/plugin_api.py` mounts read-only routes through Hermes's backend plugin loader. `src/plugin.jsx` builds to `desktop/plugin.js`, importing React and `@hermes/plugin-sdk` from the host.

Existing extension points: [Desktop plugin SDK](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk), [middleware](https://hermes-agent.nousresearch.com/docs/developer-guide/middleware), and [observer hooks](https://hermes-agent.nousresearch.com/docs/developer-guide/observer-hooks).
