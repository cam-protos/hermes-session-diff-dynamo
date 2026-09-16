"""Temporary browser-test data served through the actual plugin routes."""
import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='session-diff-preview-') as tmp:
        os.environ['HERMES_HOME'] = str(Path(tmp) / 'home')
        from session_diff.recorder import Recorder
        from session_diff.store import Store
        from session_diff.api import router
        from fastapi import FastAPI
        from fastapi.responses import FileResponse
        import uvicorn

        work = Path(tmp) / 'project'
        work.mkdir()
        class Paths:
            root = work
            blocked = None
            def resolve(self, path): return work / path

        recorder = Recorder(paths_factory=lambda task: Paths())
        recorder.begin_run(session_id='demo-session')
        def write(name, content):
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            return recorder.execute(next_call=lambda args: path.write_text(content, encoding='utf-8'),
                                    args={'path': name}, tool_name='write_file', session_id='demo-session')
        before = 'export async function loadSession(id) {\n  const response = await fetch(`/sessions/${id}`);\n  return response.json();\n}\n'
        (work / 'src').mkdir()
        (work / 'src/session.ts').write_text(before)
        write('src/session.ts', 'export async function loadSession(id, signal) {\n  const response = await fetch(`/sessions/${id}`, { signal });\n  if (!response.ok) throw new Error("Session unavailable");\n  return response.json();\n}\n')
        write('tests/session.test.ts', 'test("rejects unavailable sessions", async () => {\n  await expect(loadSession("missing")).rejects.toThrow();\n});\n')
        write('README.md', '# Session loading\n\nRequests can be cancelled when the active chat changes.\n')
        write('scratch.txt', 'temporary experiment\n')
        recorder.execute(next_call=lambda args: (work / 'scratch.txt').unlink(), args={'path': 'scratch.txt'},
                         tool_name='patch', session_id='demo-session')
        app = FastAPI()
        app.include_router(router, prefix='/api/plugins/hermes-session-diff')
        @app.get('/')
        def index(): return FileResponse(ROOT / 'tests/browser/index.html')
        @app.get('/bundle.js')
        def bundle(): return FileResponse(ROOT / 'tests/browser/bundle.js', media_type='text/javascript')
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__': main()
