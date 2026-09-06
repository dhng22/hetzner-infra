"""
`admin/preview_build.py` must still build.

It is the only thing that renders every partial the panel has against real
Component objects, and it is run BY HAND — so when it broke, it stayed broken
silently, which is the same failure this repository keeps paying for elsewhere.

What broke it: the seed created a Redis with a `maxmemory` above the field's
default reservation, `create()` refused the spec, and the script exited before
rendering anything. A component seed and a component validator drifting apart is
exactly the kind of thing a unit test cannot see, because neither side is wrong
on its own.

A subprocess rather than an import: the script sets `INFRA_DIR` and other
environment at module scope and imports the panel against it, which would leak
into every other test sharing this process.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_ADMIN = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = _ADMIN / "preview_build.py"


class PreviewBuildTest(unittest.TestCase):
    def test_the_preview_builds_from_the_seed_it_ships_with(self):
        with tempfile.TemporaryDirectory() as out:
            env = dict(os.environ, PREVIEW_OUT=out)
            done = subprocess.run([sys.executable, str(SCRIPT)], env=env,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0,
                             f"{done.stdout}\n{done.stderr}")
            page = pathlib.Path(out) / "index.html"
            self.assertTrue(page.exists())
            # Not merely non-empty: a page that rendered no components is what
            # a refused seed produces, and it exits 0 doing it.
            self.assertIn("4 components", done.stdout)
            self.assertGreater(len(page.read_text()), 100_000)


if __name__ == "__main__":
    unittest.main()
