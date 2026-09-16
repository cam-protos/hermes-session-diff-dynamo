// src/plugin.jsx
import React, { useEffect, useRef, useState } from "react";
import { host, useValue } from "@hermes/plugin-sdk";

// src/diff-rows.js
function diffRows(patch) {
  let before = 0;
  let after = 0;
  let inHunk = false;
  const rows = [];
  const lines = patch.split("\n");
  if (lines.at(-1) === "") lines.pop();
  for (const text of lines) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    if (hunk) {
      before = Number(hunk[1]);
      after = Number(hunk[2]);
      inHunk = true;
      rows.push({ kind: "hunk", text });
    } else if (!inHunk) {
      if (!text.startsWith("--- ") && !text.startsWith("+++ ")) rows.push({ kind: "meta", text });
    } else if (text.startsWith("+")) rows.push({ kind: "add", after: after++, text });
    else if (text.startsWith("-")) rows.push({ kind: "remove", before: before++, text });
    else if (text.startsWith(" ")) rows.push({ kind: "context", before: before++, after: after++, text });
    else rows.push({ kind: "meta", text });
  }
  return rows;
}

// src/plugin.jsx
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
var css = `
.hsd{height:100%;min-height:0;display:flex;flex-direction:column;color:var(--ui-text-primary,#242424);background:var(--ui-sidebar-surface-background,#fafafa);font:13px/1.5 var(--font-sans,system-ui);container-type:inline-size}
.hsd *{box-sizing:border-box}.hsd button,.hsd select,.hsd input{font:inherit;color:inherit}
.hsd button,.hsd select{border:1px solid var(--ui-stroke-secondary,#d0d0d0);background:var(--ui-surface-background,#fff);border-radius:4px;padding:5px 9px}
.hsd{--hsd-hover:var(--ui-row-hover-background,color-mix(in srgb,currentColor 7%,transparent));--hsd-active:var(--ui-row-active-background,color-mix(in srgb,currentColor 12%,transparent));scrollbar-color:var(--ui-stroke-secondary,#888) transparent;caret-color:currentColor}
.hsd button{cursor:pointer}.hsd button:hover:not(:disabled){background:var(--ui-control-hover-background,color-mix(in srgb,currentColor 7%,transparent))}.hsd button:active:not(:disabled){background:var(--ui-control-active-background,color-mix(in srgb,currentColor 12%,transparent))}.hsd button:disabled{opacity:.5;cursor:default}
.hsd :focus-visible{outline:2px solid var(--ui-text-primary,#242424);outline-offset:2px}.hsd ::selection{background:var(--ui-selection-background,#b9d9fc)}
.hsd header{padding:16px 16px 12px;border-bottom:1px solid var(--ui-stroke-secondary,#ddd)}.hsd h2{font-size:16px;font-weight:600;margin:0}.hsd h3{font-size:14px;margin:0 0 6px}
.hsd .title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.hsd .controls{display:flex;gap:8px;flex-wrap:wrap}.hsd label{display:flex;flex-direction:column;gap:4px;min-width:0;flex:1}.hsd label span{font-size:11px;color:var(--ui-text-secondary,#555)}
.hsd select{width:100%;min-width:0;text-overflow:ellipsis}.hsd .summary{padding:9px 16px;color:var(--ui-text-secondary,#555);border-bottom:1px solid var(--ui-stroke-secondary,#ddd);font-variant-numeric:tabular-nums}
.hsd .body{min-height:0;flex:1;display:grid;grid-template-columns:minmax(180px,32%) minmax(0,1fr)}.hsd .files{overflow:auto;border-right:1px solid var(--ui-stroke-secondary,#ddd);min-height:0}
.hsd .search{padding:10px}.hsd input{width:100%;padding:6px 8px;background:var(--ui-surface-background,#fff);border:1px solid var(--ui-stroke-secondary,#ccc);border-radius:4px}.hsd input::placeholder{color:var(--ui-text-secondary,#555)}
.hsd .file{display:block;width:100%;text-align:left;border:0;border-radius:0;padding:10px 12px;background:transparent}.hsd .file:hover:not(:disabled),.hsd .history button:hover:not(:disabled){background:var(--hsd-hover)}.hsd .file[aria-pressed=true],.hsd .file[aria-pressed=true]:hover,.hsd .history button[aria-pressed=true],.hsd .history button[aria-pressed=true]:hover{background:var(--hsd-active);color:var(--ui-text-primary,#242424)}
.hsd .file:focus-visible{outline-offset:-2px}.hsd .file[aria-pressed=true] .filename{font-weight:600}.hsd .filemeta .status{text-transform:capitalize}.hsd .filepath{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ui-text-secondary,#555);font-size:11px;margin-top:3px}.hsd .filemeta{justify-content:space-between}
.hsd .filename{font-family:var(--font-mono,monospace);overflow-wrap:anywhere;font-size:12px}.hsd .filemeta{display:flex;gap:7px;flex-wrap:wrap;color:var(--ui-text-secondary,#555);font-size:11px;margin-top:3px}
.hsd .detail{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}.hsd .detailhead{padding:12px 14px;border-bottom:1px solid var(--ui-stroke-secondary,#ddd)}.hsd .actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:9px}
.hsd .diff{overflow:auto;flex:1;min-height:0;margin:0;padding:0 0 12px;tab-size:4;font:12px/1.8 var(--font-mono,monospace);background:var(--ui-surface-background,#fff)}.hsd .line{display:flex;min-width:max-content;white-space:pre;padding-right:16px}.hsd .lineno{display:inline-block;flex:0 0 4ch;width:4ch;text-align:right;padding-right:1ch;color:var(--ui-text-secondary,#555);user-select:none;font-variant-numeric:tabular-nums}.hsd .linecode{padding-left:10px}.hsd .line.hunk .linecode,.hsd .line.meta .linecode{padding:0 12px}.hsd .line.meta{color:var(--ui-text-secondary,#555)}
.hsd .line.add{background:color-mix(in srgb,#278344 14%,transparent)}.hsd .line.remove{background:color-mix(in srgb,#cc3434 12%,transparent)}.hsd .line.hunk{color:var(--ui-text-secondary,#555);background:var(--hsd-hover);padding:5px 0;margin:0 0 4px}.hsd .line.hunk:not(:first-child){margin-top:12px}
.hsd .empty{padding:28px 20px;max-width:58ch;color:var(--ui-text-secondary,#555)}.hsd .empty p{margin:6px 0 12px}.hsd .notice{padding:9px 16px;font-size:12px;border-bottom:1px solid var(--ui-stroke-secondary,#ddd)}.hsd details p{margin:7px 0;max-width:75ch}.hsd summary{cursor:pointer}
.hsd .history{overflow:auto;max-height:180px;border-bottom:1px solid var(--ui-stroke-secondary,#ddd);padding:10px 12px}.hsd .history p{font-size:11px;margin:0 0 7px}.hsd .history button{display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;text-align:left;margin-bottom:3px;border-color:transparent;background:transparent;padding:6px 8px}.hsd .history button[aria-pressed=true]{border-color:var(--ui-stroke-secondary,#d0d0d0)}.hsd .historytool{font-weight:500}.hsd .historysource,.hsd .history time{font-size:11px;color:var(--ui-text-secondary,#555)}.hsd .history time{white-space:nowrap;font-variant-numeric:tabular-nums}.hsd .historysource{display:block}.hsd .muted{color:var(--ui-text-secondary,#555)}.hsd .counts{font-family:var(--font-mono,monospace);font-variant-numeric:tabular-nums;display:flex;gap:8px;margin-right:auto}.hsd .detailhead>.filename{font-weight:600}.hsd .comparison{padding:6px 14px;border-bottom:1px solid var(--ui-stroke-secondary,#ddd);font-size:11px;color:var(--ui-text-secondary,#555)}.hsd .history button[aria-pressed=true] .historytool{font-weight:600}
@container(max-width:560px){.hsd .body{grid-template-columns:1fr;grid-template-rows:minmax(100px,32%) minmax(0,1fr)}.hsd .files{border-right:0;border-bottom:1px solid var(--ui-stroke-secondary,#ddd)}.hsd header{padding:12px}.hsd .detailhead{padding:10px}.hsd .controls{gap:6px}}
`;
var date = (seconds) => new Date(seconds * 1e3).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
var basename = (path) => path.split(/[\\/]/).pop();
var emptyAtom = { get: () => null, subscribe: () => () => {
}, listen: () => () => {
} };
function SessionChanges({ ctx }) {
  const focused = useValue(host.state.focusedStoredSessionId || host.state.activeSessionId);
  const runtime = useValue(host.state.focusedSessionId || host.state.activeSessionId);
  const profile = useValue(host.state.profile);
  const gateway = useValue(host.state.gateway);
  const connection = useValue(host.state.connectionId || emptyAtom);
  const owner = useValue(host.state.focusedSessionOwner || emptyAtom);
  const [sessions, setSessions] = useState([]);
  const [chosen, setChosen] = useState("");
  const [scope, setScope] = useState("latest");
  const [data, setData] = useState(null);
  const [selected, setSelected] = useState("");
  const [filter, setFilter] = useState("");
  const [sessionError, setSessionError] = useState("");
  const [changesError, setChangesError] = useState("");
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const generation = useRef(0);
  const otherBackend = owner && (owner.profile !== profile || connection && owner.connectionId !== connection);
  const session = chosen || !otherBackend && sessions.find((s) => s.session === focused || s.session === runtime)?.session || "";
  useEffect(() => {
    setChosen("");
    setScope("latest");
    setData(null);
    setSelected("");
    setSessions([]);
  }, [profile, connection, focused, runtime]);
  useEffect(() => {
    let gone = false;
    let timer;
    async function refresh() {
      try {
        const response = await ctx.rest("/sessions", { timeoutMs: 1e4 });
        if (!gone) {
          setSessions(response.sessions);
          setSessionError("");
        }
      } catch {
        if (!gone) setSessionError("The recorder is unavailable on this backend. Enable hermes-session-diff in the Agent plugins settings, then restart the gateway and retry.");
      } finally {
        if (!gone) {
          setLoading(false);
          timer = setTimeout(refresh, 4e3);
        }
      }
    }
    setLoading(true);
    refresh();
    return () => {
      gone = true;
      clearTimeout(timer);
    };
  }, [ctx, profile, connection, gateway, focused, runtime, tick]);
  useEffect(() => {
    const current = ++generation.current;
    let timer;
    let gone = false;
    setData(null);
    setChangesError("");
    if (!session) return;
    async function refresh() {
      try {
        const response = await ctx.rest(`/changes?session=${encodeURIComponent(session)}&run=${encodeURIComponent(scope)}`, { timeoutMs: 1e4 });
        if (!gone && current === generation.current) {
          setData(response);
          setSelected((previous) => response.files.some((f) => f.path === previous) ? previous : response.files[0]?.path || "");
          setChangesError("");
        }
      } catch {
        if (!gone) setChangesError("Could not load recorded changes. Check the gateway connection and retry.");
      } finally {
        if (!gone) timer = setTimeout(refresh, 2500);
      }
    }
    refresh();
    return () => {
      gone = true;
      clearTimeout(timer);
    };
  }, [ctx, session, scope, profile, connection, gateway, tick]);
  const file = data?.files.find((f) => f.path === selected);
  const files = data?.files.filter((f) => f.path.toLowerCase().includes(filter.toLowerCase())) || [];
  return /* @__PURE__ */ jsxs("section", { className: "hsd", "aria-label": "Session changes", children: [
    /* @__PURE__ */ jsx("style", { children: css }),
    /* @__PURE__ */ jsxs("header", { children: [
      /* @__PURE__ */ jsxs("div", { className: "title", children: [
        /* @__PURE__ */ jsx("h2", { children: "Session changes" }),
        /* @__PURE__ */ jsx("button", { onClick: () => setTick((n) => n + 1), children: "Refresh" })
      ] }),
      /* @__PURE__ */ jsxs("div", { className: "controls", children: [
        /* @__PURE__ */ jsxs("label", { children: [
          /* @__PURE__ */ jsx("span", { children: "Session on this backend" }),
          /* @__PURE__ */ jsxs("select", { "aria-label": "Session", value: chosen, onChange: (e) => {
            setChosen(e.target.value);
            setScope("latest");
          }, children: [
            /* @__PURE__ */ jsx("option", { value: "", children: "Follow focused chat" }),
            sessions.map((s) => /* @__PURE__ */ jsxs("option", { value: s.session, children: [
              date(s.updated),
              " \xB7 ",
              s.session.slice(0, 12)
            ] }, s.session))
          ] })
        ] }),
        /* @__PURE__ */ jsxs("label", { children: [
          /* @__PURE__ */ jsx("span", { children: "Compare" }),
          /* @__PURE__ */ jsxs("select", { "aria-label": "Compare", value: scope, onChange: (e) => setScope(e.target.value), children: [
            /* @__PURE__ */ jsx("option", { value: "latest", children: "Latest run" }),
            /* @__PURE__ */ jsx("option", { value: "all", children: "Whole session" }),
            data?.runs.map((r, i) => /* @__PURE__ */ jsxs("option", { value: r.run, children: [
              "Run ",
              data.runs.length - i,
              " \xB7 ",
              date(r.started)
            ] }, r.run))
          ] })
        ] })
      ] })
    ] }),
    sessionError && /* @__PURE__ */ jsx("div", { className: "notice", role: "alert", children: sessionError }),
    changesError && /* @__PURE__ */ jsx("div", { className: "notice", role: "alert", children: changesError }),
    otherBackend && !chosen && /* @__PURE__ */ jsx("div", { className: "notice", children: "The focused chat belongs to another backend. Switch to that backend to follow it, or choose a recorded session on this backend." }),
    data?.notes.length > 0 && /* @__PURE__ */ jsxs("details", { className: "notice", children: [
      /* @__PURE__ */ jsx("summary", { children: "Capture coverage" }),
      data.notes.map((n) => /* @__PURE__ */ jsx("p", { children: n }, n))
    ] }),
    data && /* @__PURE__ */ jsxs("div", { className: "summary", role: "status", children: [
      data.files.length,
      " ",
      data.files.length === 1 ? "file" : "files",
      " changed",
      data.files.some((f) => f.status === "restored") ? " \xB7 includes restored files" : ""
    ] }),
    !session ? /* @__PURE__ */ jsxs("div", { className: "empty", children: [
      /* @__PURE__ */ jsx("h3", { children: loading ? "Connecting to the recorder\u2026" : "No capture for this chat yet" }),
      /* @__PURE__ */ jsx("p", { children: "File changes appear here after a run uses a file or terminal tool. Recording starts when the Agent component is enabled." }),
      sessions.length > 0 && /* @__PURE__ */ jsx("p", { children: "You can also select a recorded session above." })
    ] }) : !data ? /* @__PURE__ */ jsx("div", { className: "empty", role: "status", children: changesError ? "Use Refresh to try again." : "Loading recorded changes\u2026" }) : !data.files.length ? /* @__PURE__ */ jsxs("div", { className: "empty", children: [
      /* @__PURE__ */ jsx("h3", { children: "No recorded changes in this scope" }),
      /* @__PURE__ */ jsx("p", { children: "Choose Whole session to see earlier runs. Files created before recording started cannot be reconstructed." })
    ] }) : /* @__PURE__ */ jsxs("div", { className: "body", children: [
      /* @__PURE__ */ jsxs("div", { className: "files", children: [
        /* @__PURE__ */ jsx("div", { className: "search", children: /* @__PURE__ */ jsx("input", { "aria-label": "Filter files", placeholder: "Filter files", value: filter, onChange: (e) => setFilter(e.target.value) }) }),
        files.map((f) => /* @__PURE__ */ jsxs("button", { className: "file", "aria-pressed": f.path === selected, onClick: () => setSelected(f.path), title: f.path, children: [
          /* @__PURE__ */ jsx("div", { className: "filename", children: basename(f.path) }),
          /* @__PURE__ */ jsxs("div", { className: "filemeta", children: [
            /* @__PURE__ */ jsx("span", { className: "status", children: f.status }),
            /* @__PURE__ */ jsxs("span", { children: [
              f.edits,
              " ",
              f.edits === 1 ? "edit" : "edits"
            ] }),
            f.observed && /* @__PURE__ */ jsx("span", { children: "Observed" })
          ] }),
          /* @__PURE__ */ jsx("div", { className: "filepath", children: f.path.slice(0, -basename(f.path).length) })
        ] }, f.path)),
        !files.length && /* @__PURE__ */ jsx("p", { className: "empty", children: "No matching files." })
      ] }),
      file && /* @__PURE__ */ jsx(FileDetail, { ctx, session, file }, `${connection}:${profile}:${session}:${scope}:${file.path}`)
    ] })
  ] });
}
function FileDetail({ ctx, session, file }) {
  const [diff, setDiff] = useState(null);
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [copyError, setCopyError] = useState("");
  const [history, setHistory] = useState(null);
  const [event, setEvent] = useState(null);
  const [copied, setCopied] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let gone = false;
    setDiff(null);
    setError("");
    setCopyError("");
    setCopied(false);
    ctx.rest(`/diff?session=${encodeURIComponent(session)}&first=${event || file.first_id}&last=${event || file.last_id}`, { timeoutMs: 1e4 }).then((value) => {
      if (!gone) setDiff(value);
    }).catch(() => {
      if (!gone) setError("Could not load this snapshot. It may have expired. Refresh the file list or retry.");
    });
    return () => {
      gone = true;
    };
  }, [ctx, session, file.first_id, file.last_id, event, retry]);
  useEffect(() => {
    setHistory(null);
  }, [file.last_id]);
  async function loadHistory() {
    if (history) {
      setHistory(null);
      return;
    }
    try {
      const response = await ctx.rest(`/history?session=${encodeURIComponent(session)}&first=${file.first_id}&last=${file.last_id}`);
      setHistory(response.history);
      setHistoryError("");
    } catch {
      setHistoryError("Could not load edit history.");
    }
  }
  async function copy() {
    try {
      if (!await ctx.os.writeClipboard(diff.patch)) throw new Error("Clipboard unavailable");
      setCopied(true);
      setCopyError("");
    } catch {
      setCopyError("Could not copy the patch. Select the diff text to copy it manually.");
    }
  }
  const operation = history?.find((item) => item.id === event);
  return /* @__PURE__ */ jsxs("div", { className: "detail", children: [
    /* @__PURE__ */ jsxs("div", { className: "detailhead", children: [
      /* @__PURE__ */ jsx("div", { className: "filename", children: basename(file.path) }),
      /* @__PURE__ */ jsx("div", { className: "filepath", title: file.path, children: file.path }),
      /* @__PURE__ */ jsxs("div", { className: "actions", children: [
        /* @__PURE__ */ jsx("span", { className: "counts", children: diff ? /* @__PURE__ */ jsxs(Fragment, { children: [
          /* @__PURE__ */ jsxs("span", { children: [
            "+",
            diff.added
          ] }),
          /* @__PURE__ */ jsxs("span", { children: [
            "\u2212",
            diff.removed
          ] })
        ] }) : "Loading\u2026" }),
        /* @__PURE__ */ jsx("button", { onClick: copy, disabled: !diff?.patch, children: copied ? "Copied" : "Copy patch" }),
        /* @__PURE__ */ jsx("button", { onClick: loadHistory, children: history ? "Hide history" : "Edit history" }),
        event && /* @__PURE__ */ jsx("button", { onClick: () => setEvent(null), children: "Show net diff" })
      ] })
    ] }),
    file.gap && /* @__PURE__ */ jsx("div", { className: "notice", children: "The file changed between recorded edits. This net diff includes intervening changes; use edit history to inspect each captured operation." }),
    copyError && /* @__PURE__ */ jsxs("div", { className: "notice", role: "alert", children: [
      copyError,
      " ",
      /* @__PURE__ */ jsx("button", { onClick: copy, children: "Retry copy" })
    ] }),
    historyError && /* @__PURE__ */ jsxs("div", { className: "notice", role: "alert", children: [
      historyError,
      " ",
      /* @__PURE__ */ jsx("button", { onClick: loadHistory, children: "Retry history" })
    ] }),
    history && /* @__PURE__ */ jsxs("div", { className: "history", "aria-label": "Edit history", children: [
      /* @__PURE__ */ jsxs("p", { className: "muted", children: [
        "Latest ",
        history.length,
        " captured operations"
      ] }),
      history.map((item) => /* @__PURE__ */ jsxs("button", { "aria-label": `${date(item.created)} \xB7 ${item.tool} \xB7 ${item.source}`, "aria-pressed": event === item.id, onClick: () => setEvent(item.id), children: [
        /* @__PURE__ */ jsxs("span", { children: [
          /* @__PURE__ */ jsx("span", { className: "historytool", children: item.tool }),
          /* @__PURE__ */ jsx("span", { className: "historysource", children: item.source })
        ] }),
        /* @__PURE__ */ jsx("time", { dateTime: new Date(item.created * 1e3).toISOString(), children: date(item.created) })
      ] }, item.id))
    ] }),
    /* @__PURE__ */ jsx("div", { className: "comparison", children: event ? `Recorded edit${operation ? ` \xB7 ${date(operation.created)}` : ""}` : "Net diff \xB7 before first edit \u2192 after last edit" }),
    error ? /* @__PURE__ */ jsxs("div", { className: "empty", role: "alert", children: [
      /* @__PURE__ */ jsx("p", { children: error }),
      /* @__PURE__ */ jsx("button", { onClick: () => setRetry((n) => n + 1), children: "Retry" })
    ] }) : !diff ? /* @__PURE__ */ jsx("div", { className: "empty", role: "status", children: "Loading diff\u2026" }) : diff.patch ? /* @__PURE__ */ jsx("pre", { className: "diff", tabIndex: "0", "aria-label": "File diff", children: diffRows(diff.patch).map((row, i) => /* @__PURE__ */ jsxs("span", { className: `line ${row.kind}`, children: [
      row.kind !== "hunk" && row.kind !== "meta" && /* @__PURE__ */ jsxs(Fragment, { children: [
        /* @__PURE__ */ jsx("span", { className: "lineno", "aria-hidden": "true", children: row.before ?? "" }),
        /* @__PURE__ */ jsx("span", { className: "lineno", "aria-hidden": "true", children: row.after ?? "" })
      ] }),
      /* @__PURE__ */ jsx("span", { className: "linecode", children: row.text || " " })
    ] }, i)) }) : /* @__PURE__ */ jsx("div", { className: "empty", children: diff.note || "The file matches its original contents. Edit history still contains the intermediate changes." })
  ] });
}
var plugin_default = {
  id: "hermes-session-diff",
  name: "Session changes",
  defaultEnabled: false,
  register(ctx) {
    ctx.register({ id: "pane", area: "panes", title: "Session changes", data: { placement: "right", width: "680px" }, render: () => /* @__PURE__ */ jsx(SessionChanges, { ctx }) });
    ctx.register({ id: "open", area: "palette", data: { id: "hermes-session-diff.open", label: "Open session changes", keywords: ["diff", "files", "changes"], run: () => {
      if (typeof host.openWorkspace === "function") host.openWorkspace("hermes-session-diff:viewer", { title: "Session changes", render: () => /* @__PURE__ */ jsx(SessionChanges, { ctx }) });
      else host.notify({ kind: "info", message: "Open the Session changes pane from the layout tabs." });
    } } });
  }
};
export {
  SessionChanges,
  plugin_default as default
};
