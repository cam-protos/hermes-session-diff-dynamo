import React from 'react';
import { createRoot } from 'react-dom/client';
import { SessionChanges } from '../../src/plugin.jsx';

const ctx = {
  async rest(path) {
    const response = await fetch('/api/plugins/hermes-session-diff' + path);
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  },
  os: { writeClipboard: async text => { if (window.failClipboard) throw new Error('fixture'); window.copiedPatch = text; return true; } }
};
createRoot(document.getElementById('root')).render(<SessionChanges ctx={ctx} />);
