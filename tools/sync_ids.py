#!/usr/bin/env python3
"""Regenerate every file that embeds a name, ID or URL from project.env.

    tools/sync_ids.py           rewrite generated files (and docs) in place
    tools/sync_ids.py --check   exit 1 if anything is out of date

Generated files come from tools/templates/*.in. Values that changed since the
last sync (as recorded in the generated ids.py) are also replaced literally in
the documentation and scripts listed in TEXT_FILES.
"""
import ast
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / 'tools' / 'templates'
IDS_PY = ROOT / 'mapper' / 'expose_for_gnome' / 'ids.py'
TEXT_GLOBS = ['README.md', 'CONTRIBUTING.md', 'docs/**/*.md', 'scripts/*', 'data/*',
              'install.sh', 'uninstall.sh']


def load_env(path=ROOT / 'project.env'):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, _, value = line.partition('=')
        env[key.strip()] = shlex.split(value)[0] if value.strip() else ''
    return env


def outputs(env):
    vals = dict(env)
    vals['SHELL_VERSIONS'] = ', '.join(f'"{v}"' for v in env['SHELL_VERSIONS'].split())

    def render(name):
        text = (TEMPLATES / name).read_text()
        return re.sub(r'@([A-Z_]+)@', lambda m: vals[m.group(1)], text)

    return {
        ROOT / 'extension' / 'metadata.json': render('metadata.json.in'),
        ROOT / 'extension' / 'ids.js': render('ids.js.in'),
        ROOT / 'extension' / 'schemas' / f'{env["SCHEMA_ID"]}.gschema.xml':
            render('gschema.xml.in'),
        IDS_PY: render('ids.py.in'),
    }


def previous_values():
    if not IDS_PY.exists():
        return {}
    tree = ast.parse(IDS_PY.read_text())
    return {n.targets[0].id: n.value.value for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)}


def main(argv):
    check = '--check' in argv
    env = load_env()
    stale = []
    old = previous_values()
    # Longest first so e.g. a URL is replaced before a prefix of it.
    renames = sorted(((v, env[k]) for k, v in old.items() if k in env and v != env[k]),
                     key=lambda p: -len(p[0]))
    if renames:
        for pattern in TEXT_GLOBS:
            for path in ROOT.glob(pattern):
                if not path.is_file():
                    continue
                text = path.read_text()
                new = text
                for a, b in renames:
                    new = new.replace(a, b)
                if new != text:
                    stale.append(path)
                    if not check:
                        path.write_text(new)

    schema_dir = ROOT / 'extension' / 'schemas'
    wanted = outputs(env)
    for path in schema_dir.glob('*.gschema.xml'):
        if path not in wanted:
            stale.append(path)
            if not check:
                path.unlink()
    for path, text in wanted.items():
        if not path.exists() or path.read_text() != text:
            stale.append(path)
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)

    for path in stale:
        print(('out of date: ' if check else 'updated: ') + str(path.relative_to(ROOT)))
    return 1 if check and stale else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
