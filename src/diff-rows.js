// Keep the original patch for copying; derive gutters only for the viewer.
export function diffRows(patch) {
  let before = 0;
  let after = 0;
  let inHunk = false;
  const rows = [];
  const lines = patch.split('\n');
  if (lines.at(-1) === '') lines.pop();
  for (const text of lines) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    if (hunk) {
      before = Number(hunk[1]);
      after = Number(hunk[2]);
      inHunk = true;
      rows.push({ kind: 'hunk', text });
    } else if (!inHunk) {
      // The detail header already identifies this file. Preserve other metadata.
      if (!text.startsWith('--- ') && !text.startsWith('+++ ')) rows.push({ kind: 'meta', text });
    } else if (text.startsWith('+')) rows.push({ kind: 'add', after: after++, text });
    else if (text.startsWith('-')) rows.push({ kind: 'remove', before: before++, text });
    else if (text.startsWith(' ')) rows.push({ kind: 'context', before: before++, after: after++, text });
    else rows.push({ kind: 'meta', text });
  }
  return rows;
}
