"""Build an installable archive containing only runtime files and user docs."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
out = root / 'dist'
out.mkdir(exist_ok=True)
archive = out / 'hermes-session-diff-0.1.0.zip'
files = [root / name for name in ('plugin.yaml', '__init__.py', 'README.md', 'LICENSE')]
for directory in ('session_diff', 'dashboard', 'desktop', 'docs', 'assets'):
    files.extend(p for p in (root / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
with ZipFile(archive, 'w', ZIP_DEFLATED) as output:
    for path in sorted(files):
        output.write(path, 'hermes-session-diff/' + path.relative_to(root).as_posix())
print(archive)
