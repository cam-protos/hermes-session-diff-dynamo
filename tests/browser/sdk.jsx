import { useSyncExternalStore } from 'react';

function atom(initial) {
  let value = initial;
  const listeners = new Set();
  return { get: () => value, subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); },
    set: next => { value = next; listeners.forEach(fn => fn()); } };
}
export const host = {
  state: { focusedStoredSessionId: atom('demo-session'), focusedSessionId: atom('demo-session'),
    activeSessionId: atom('demo-session'), profile: atom('default'), gateway: atom('open'),
    connectionId: atom('local'), focusedSessionOwner: atom({ profile: 'default', connectionId: 'local' }) },
  openWorkspace: () => {}, notify: () => {}
};
export const useValue = atom => useSyncExternalStore(atom.subscribe, atom.get);
window.previewHost = host;
