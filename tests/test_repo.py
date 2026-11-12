"""Repository-level checks: generated IDs in sync, no personal identifiers,
IDs distinct from the original personal build so both can be installed."""
import hashlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import sync_ids  # noqa: E402

# IDs of the personal build this project started from. Entries that would name a
# person are stored as SHA-256 digests so this file does not publish them.
PERSONAL_IDS = {
    'org.gnome.shell.extensions.app-expose', 'expose-mapper.service', 'expose-mapper',
    '70-expose-mapper',
}
PERSONAL_DIGESTS = {
    'b3fcee715f49c7115bcf2a87a89ce6b9c36d90acbf543a1a9d700aefd9ee8982',
    '5574a9f10b5e931900f50713a3581ccba819a502f567f4f6f3669ba314188cc7',
    '897e9370d10d09751e1b4e14a21b5c36d64e5cd1bad5ae680a6f9bab805b59f7',
}
PERSONAL_WORD_DIGESTS = {'3916284aafcf7adac206d7ea999696b1f22df2e0bb168e3c5896246fb8495485'}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class RepoTest(unittest.TestCase):
    def test_generated_files_in_sync(self):
        r = subprocess.run([sys.executable, str(ROOT / 'tools' / 'sync_ids.py'), '--check'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_no_personal_identifiers(self):
        files = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True,
                               text=True).stdout.split() or [
            str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.parts]
        for f in files:
            path = ROOT / f
            if path.suffix in ('.zip', '.png', '.pyc') or '__pycache__' in path.parts \
                    or 'dist' in path.parts:
                continue
            words = set(re.findall(r'[a-z0-9]+', path.read_text(errors='ignore').lower()))
            self.assertFalse({digest(w) for w in words} & PERSONAL_WORD_DIGESTS, f)

    def test_ids_differ_from_personal_build(self):
        env = sync_ids.load_env()
        for key in ('EXTENSION_UUID', 'SHELL_BUS_NAME', 'APP_ID', 'DAEMON_BUS_NAME', 'SCHEMA_ID',
                    'SYSTEMD_UNIT', 'SLUG', 'UDEV_PREFIX'):
            self.assertNotIn(env[key], PERSONAL_IDS, key)
            self.assertNotIn(digest(env[key]), PERSONAL_DIGESTS, key)
        self.assertNotEqual(env['APP_ID'], env['SHELL_BUS_NAME'],
                            'the GUI app ID must not collide with the bus name the shell owns')

    def test_metadata(self):
        env = sync_ids.load_env()
        meta = json.loads((ROOT / 'extension' / 'metadata.json').read_text())
        self.assertEqual(meta['uuid'], env['EXTENSION_UUID'])
        self.assertEqual(meta['url'], env['HOMEPAGE'])
        self.assertFalse(meta['name'].lower().startswith('gnome'),
                         'extensions.gnome.org: names should not start with "GNOME"')
        self.assertNotIn('version', meta, 'extensions.gnome.org assigns "version" itself')
        self.assertTrue(all(v.isdigit() for v in meta['shell-version']))


if __name__ == '__main__':
    unittest.main()
