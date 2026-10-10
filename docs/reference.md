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

The journal has no local database. Run and change records live in one **DynamoDB** table; snapshot contents live in **S3**, content-addressed at `<prefix>/blobs/<sha256>` and written with `AES256` server-side encryption. Snapshots can contain source code and other file contents, so restrict the bucket and table to the roles that run and read the plugin. Any process with the same configuration reads the same journal, which is how a separate UI container can display changes recorded by the gateway.

### Provisioning

Create the table with on-demand billing, partition key `pk` (S), sort key `sk` (S) and a global secondary index `gsi` on `gpk` (S) / `gsk` (N) projecting all attributes. `session_diff.store.table_definition(name)` returns the exact `create_table` arguments:

```sh
python -c "import json; from session_diff.store import table_definition as t; print(json.dumps(t('session-diff')))" > table.json
aws dynamodb create-table --cli-input-json file://table.json
```

Create or choose a private S3 bucket. The plugin never creates resources.

| Setting | Environment variable | Meaning |
| --- | --- | --- |
| `dynamodb_table` | `SESSION_DIFF_TABLE` | Table name (required) |
| `s3_bucket` | `SESSION_DIFF_BUCKET` | Bucket name (required) |
| `s3_prefix` | `SESSION_DIFF_PREFIX` | Key prefix, default `session-diff` |
| `aws_region` | `SESSION_DIFF_REGION` | Region; empty uses the standard AWS chain |

Environment variables win over settings. Credentials come from the standard AWS chain (for example an ECS task role).

Permissions: the **recorder** needs `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query`, `BatchWriteItem`, `TransactWriteItems` on the table and its `gsi` index, and `s3:PutObject` (with `AES256` encryption), `GetObject`, `DeleteObject` on `<prefix>/blobs/*`. **`s3:ListBucket` is not needed.** Without it S3 answers `403 AccessDenied` for a missing object; the plugin treats `AccessDenied`/`403`/`NoSuchKey`/`404` as a missing blob and the viewer reports "Snapshot content is no longer available". A **read-only viewer** needs only `GetItem` and `Query` on the table and index, and `s3:GetObject` on the same prefix.

Item layout (for operators): `S#<session>/META`, `S#<session>/RUN#<run>`, `S#<session>/CHG#<id>`, `BLOB#<hash>/REF` (reference count), and `STATS/BLOBS` (byte budget). Blob `REF` items also carry `gpk=BLOB` and `gsk` = time of the last reference change, which lets `prune` find blobs left at zero references. Blobs are reference counted, so a snapshot shared by several changes is deleted only after the last one expires. The last releaser sets a short-lived `dying` marker before deleting the S3 object; a writer that wants the same blob waits for it (up to about 5 seconds) so it cannot lose a freshly uploaded object.

The recorder excludes common credential filenames, private-key files, dependency/build directories, links, devices, and paths blocked by Hermes's own file-read rules. These filters are not a general secret detector: a token embedded in an ordinary source file will be captured with that file.

Defaults:

- UTF-8 text snapshots up to 512 KiB per file (stored in S3, so DynamoDB's 400 KB item limit does not apply).
- Workspace enumeration up to 3,000 entries, 16 MiB of text, and four seconds, with up to three seconds for Git enumeration. Capture happens on both sides of a command and adds latency.
- 256 MiB of deduplicated text content, tracked as a running counter. When full, recording reports the problem and tool calls continue. Concurrent writers can overshoot the limit by at most the size of their in-flight snapshots.
- 30-day retention, pruned at most hourly when a recorded tool runs. Each prune handles at most 500 change records (`prune(days, limit=500)`), so a large backlog is cleared over several runs; run and session records are always swept. Expiring a change and dropping its blob references is one DynamoDB transaction. Unreferenced blobs are deleted from S3 when their last reference expires, and a later prune also removes blobs a crash left at zero references.
- Up to 10,000 change events in one summary, 100 runs in the selector, and 100 operations in a file's history. Summary truncation is labelled.
- Text diff generation up to 12,000 combined lines.

### Consistency and known limits

- **Blobs are re-uploaded on every reference.** Each recorded change that uses a blob writes it to S3 again (idempotent for a content-addressed key). If the bucket has a lifecycle rule that expires objects N days after upload, the clock therefore restarts at the *last reference*, so shared blobs do not expire while still in use. Plugin retention should still be N days or less (30 by default), because a blob that is no longer referenced is not refreshed.
- **A crash between acquiring blob references and committing a change can leak a reference and its bytes.** Leaked blobs are retained, not lost. The S3 lifecycle (if any) bounds the object, but the `STATS/BLOBS` byte counter is not self-correcting; if it drifts upward, reset it by deleting the `STATS/BLOBS` item after clearing the journal.
- **Mutating DynamoDB calls are not retried automatically**, because a replayed `ADD` after a lost response would double count references, ids or bytes. The clients are configured with a single attempt, and only throttling errors (`ThrottlingException`, `ProvisionedThroughputExceededException`, `RequestLimitExceeded`, `ThrottledException`, which guarantee the request was not applied) are retried, up to 5 attempts with backoff. A lost response surfaces as a recorder error (tool calls continue) and at worst a leaked count.
- Timeouts are 3 s connect / 10 s read per call so a DynamoDB or S3 brownout cannot stall a tool call for more than a few seconds per call.
- A failed multi-row write removes the change rows it may have written before it releases its blob references.

The file/byte/time caps can make workspace coverage partial; the pane labels this. Excluded, unreadable and unstable files are not silently treated as deletions.

Optional `config.yaml` settings:

```yaml
plugins:
  entries:
    hermes-session-diff:
      settings:
        retention_days: 30
        terminal_capture: true
        dynamodb_table: session-diff
        s3_bucket: my-session-diff-bucket
        s3_prefix: session-diff
```

Restart the gateway after changing settings. Set `terminal_capture: false` to keep explicit file capture without workspace scans. To erase retained snapshots, stop the gateway and delete the table's items and the `<prefix>/blobs/` objects (or let the retention period expire them).

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
