# FORGE Studio — the desktop shell

A native window around the app the Python package already serves. It does not
reimplement the editor, the agent or the diff: it starts `forge studio` for the
repository you open and shows it, so there is one implementation and no second
copy to drift out of step.

## What the shell adds

Things a browser cannot do, and nothing else:

- an application you install, with an icon and a Start Menu / Applications entry
- **File → Open Folder**, so a repository is something you choose rather than
  something you `cd` to before launching
- a native menu, real window state, and remembered size
- a server whose lifetime is tied to the window, rather than to a terminal
  somebody has to remember to keep open

## Run it

```bash
cd desktop
npm install
npm start
```

`npm start` expects FORGE itself to be installed for some Python on the
machine — the shell probes `forge`, then `python -m forge.cli`, and says so
plainly if it finds neither:

```bash
pip install "forge-runtime[api] @ git+https://github.com/Adeel2208/forge-runtime"
```

## Build installers

```bash
npm run dist        # installer for the current platform
npm run dist:dir    # unpacked, for a quick look
```

Produces `.exe` (NSIS) on Windows, `.dmg` on macOS, `AppImage` and `.deb` on
Linux, into `desktop/dist/`. Verified on Windows: `FORGE Studio Setup 0.6.0.exe`,
about 82 MB, from a 269 MB unpacked tree.

Two things about that output are worth knowing before you hand it to anyone.

**It is not code-signed.** electron-builder says so and continues. Windows
SmartScreen will warn on first run and macOS will refuse outright without
notarisation. Signing needs a purchased certificate; nothing in this repository
can conjure one.

**electron-builder only builds for the platform it runs on**, absent
cross-compilation, so a real release needs one CI runner per operating system.

Neither `node_modules/` nor `dist/` is committed: the Electron toolchain is a
build dependency of this directory, not source of the project.

## Two details that are correctness, not taste

**The API key travels in the URL fragment.** The shell mints it, passes it as
`#key=…`, and the page consumes it once and strips it from the address. A
fragment is never sent to the server, so the credential stays in the browser
context it is for, and the shell never has to scrape it out of the child
process's stdout.

**The server is killed on close, on quit, and on signals.** A child process
outliving its parent is how you end up with an orphaned agent holding a lock on
somebody's repository. On Windows that needs `taskkill /T`, because the process
tree does not go away on its own.

## What this is not

Not a fork of VS Code. There is no extension host, no language server, no
integrated terminal, no multi-cursor. Studio is for reading what an agent did to
your repository and deciding whether to keep it — the editor exists so you can
fix a line without leaving, not to replace the one you already use.
