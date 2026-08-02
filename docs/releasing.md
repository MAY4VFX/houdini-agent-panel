# Release

The panel is distributed via PyPI, because installing it has to be one
command on any machine. Everything else — the installer, the plugin tree,
per-Houdini dependencies — arrives from there.

## What happens on someone's install

Three steps, and only the first one needs PyPI:

1. `uvx --from houdini-agent-panel …` — downloads the package and runs its
   installer, leaving nothing in the system.
2. The installer finds every Houdini on the machine and installs the panel
   **into each one's own Python**: `hython -m pip install --target
   <data>/deps/py3.11`. A separate tree per version, because `pydantic`
   carries a compiled core, and Python 3.11 (H20.5) and 3.13 (H22) have
   different ABIs.
3. Writes the package json into every Houdini's prefs.

There's a consequence that's easy to miss: **the version on PyPI must
match the one the installer is running.** Step 2 asks pip for exactly
`houdini-agent-panel==<its own version>`. Publishing a wheel but not an
sdist, or publishing under a different number, means an install that fails
on the second step, after already having told the person everything's
fine.

## Order of operations

```bash
# 1. The version lives in one place — python/houdini_agent_panel/__init__.py and
#    pyproject.toml must match. Checked by a test.
.venv/bin/python -m pytest -q

# 2. Build both distributions. The sdist is mandatory: without it, machines
#    with no matching wheel are left without the panel.
rm -rf dist && uv build -o dist

# 3. Metadata and contents
.venv/bin/python -m twine check dist/*

# 4. Verify against a real Houdini before publishing, from the local wheel
uvx --from ./dist/houdini_agent_panel-*.whl python -m houdini_agent_panel install --find-links "$(pwd)/dist"

# 5. Publish
UV_PUBLISH_TOKEN=$(grep -E '^PYPI_TOKEN=' "$SECRETS_ENV" | cut -d= -f2-) uv publish dist/*
```

The token lives in a local secrets file outside git (`$SECRETS_ENV` is the
path to it, chmod 600) and is read at the point of use. It never ends up
in the repository or in shell history.

## What to check by hand before publishing

Tests don't catch these, and they'll break for everyone at once:

- **The plugin tree made it into the wheel.** `houdini_agent_panel/houdini/`
  with `python_panels/*.pypanel` and both `python3.*libs/uiready.py`.
  Without it Houdini won't show the panel at all, while the install
  completes with zero errors.
- **The panel comes up in a real Houdini.** No unit test checks this:
  outside Houdini, asyncio is stock, certificates are the system's, there's
  one Qt — while inside, all three of those are different (see
  [`facts/houdini.md`](facts/houdini.md) §9).
- **The version number.** Once published, it can't be reused: PyPI won't
  let you re-upload a file under the same number, only release the next one.

## Announcements feed

The default address points into this repository, and while the repository
is private, `raw.githubusercontent.com` returns 404 to an anonymous
client — meaning users simply get no announcements. The panel doesn't
break because of this, the network error is swallowed. Once the channel is
actually needed — either make the repository public, or put
`announcements.json` at any public address and change `DEFAULT_FEED_URL`.
A studio can override it with the `HAP_FEED_URL` variable, without
rebuilding the package.
