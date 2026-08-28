/**
 * FORGE Studio - the desktop shell.
 *
 * This does not reimplement anything. It starts the Python server that already
 * exists, waits for it to answer, and shows it in a native window: the app you
 * see is the same one `forge studio` serves, so there is exactly one
 * implementation of the editor, the agent and the diff, and no second copy to
 * drift out of step.
 *
 * What the shell adds is the part a browser cannot: an application you install,
 * a real window with a native menu, Open Folder, and a server whose lifetime is
 * tied to the window rather than to a terminal someone has to remember to keep
 * open.
 *
 * Two details worth reading, because both are correctness rather than taste:
 *
 *   The API key is minted here and handed to the page through the URL fragment.
 *   A fragment is never sent to the server, so the key stays in the browser
 *   context it is for, and the shell never has to scrape it out of the child
 *   process's stdout.
 *
 *   The server is a child process, and a child process outliving its parent is
 *   how you end up with an orphaned agent holding a lock on someone's
 *   repository. It is killed on window close, on quit, and on the signals that
 *   skip both.
 */

"use strict";

const electron = require("electron");

// `ELECTRON_RUN_AS_NODE=1` makes Electron run this file as plain Node, so
// `require("electron")` hands back a path string instead of the API. VS Code
// exports that variable to its integrated terminal, so anyone starting Studio
// from inside an editor hits it - and the raw symptom is an undefined property
// deep in this file, which explains nothing.
if (typeof electron === "string" || !electron.app) {
  process.stderr.write(
    "\nFORGE Studio must run as an Electron app, not as Node.\n\n" +
      "ELECTRON_RUN_AS_NODE is set in this shell (VS Code's terminal sets it).\n" +
      "Clear it and try again:\n\n" +
      "  cmd:        set ELECTRON_RUN_AS_NODE=& npm start\n" +
      "  powershell: $env:ELECTRON_RUN_AS_NODE=''; npm start\n" +
      "  bash:       env -u ELECTRON_RUN_AS_NODE npm start\n\n" +
      "An installed build is unaffected: it does not inherit your shell.\n"
  );
  process.exit(1);
}

const { app, BrowserWindow, Menu, dialog, shell, session } = electron;
const { spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");

const STATE = path.join(app.getPath("userData"), "state.json");
const LOG = path.join(app.getPath("userData"), "studio.log");

/**
 * A desktop app has no console anyone will ever see. When Studio fails to
 * start, this file is the only thing that can say why, so it records the
 * decisions - which git, which Python, which port - rather than a stack trace
 * nobody can act on.
 */
function log(...parts) {
  const line = `${new Date().toISOString()}  ${parts.join(" ")}\n`;
  try {
    fs.mkdirSync(path.dirname(LOG), { recursive: true });
    fs.appendFileSync(LOG, line);
  } catch {
    /* logging must never be the thing that breaks startup */
  }
  process.stdout.write(line);
}

let win = null;
let server = null;
let current = { repo: null, port: null, key: null };

/* ---------------------------------------------------------------- state */

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(STATE, "utf8"));
  } catch {
    return {};
  }
}

function saveState(patch) {
  try {
    fs.mkdirSync(path.dirname(STATE), { recursive: true });
    fs.writeFileSync(STATE, JSON.stringify({ ...loadState(), ...patch }, null, 2));
  } catch {
    /* a lost window size is not worth a dialog */
  }
}

/* -------------------------------------------------------------- backend */

/**
 * How to run FORGE. A packaged app inherits almost nothing from the shell that
 * installed it, so `forge` on PATH is a hope rather than a plan: we look for
 * the console script, then fall back to any Python that can import the package.
 */
function findBackend() {
  const candidates = [
    { cmd: "forge", args: [] },
    { cmd: "forge.exe", args: [] },
    { cmd: "python", args: ["-m", "forge.cli"] },
    { cmd: "python3", args: ["-m", "forge.cli"] },
    { cmd: "py", args: ["-3", "-m", "forge.cli"] },
  ];
  for (const c of candidates) {
    const probe = spawnSync(c.cmd, [...c.args, "--help"], {
      encoding: "utf8",
      timeout: 20000,
      windowsHide: true,
    });
    if (probe.status === 0 && /studio/.test(probe.stdout || "")) return c;
  }
  return null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function waitForServer(port, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get(
        { host: "127.0.0.1", port, path: "/livez", timeout: 1500 },
        (res) => {
          res.resume();
          if (res.statusCode === 200) resolve();
          else retry();
        }
      );
      req.on("error", retry);
      req.on("timeout", () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (Date.now() > deadline) {
        reject(new Error("the FORGE server did not start within 60 seconds"));
      } else {
        setTimeout(attempt, 250);
      }
    };
    attempt();
  });
}

function stopServer() {
  if (!server) return;
  const child = server;
  server = null;
  try {
    // On Windows a detached tree needs taskkill; elsewhere the group signal
    // reaches uvicorn's workers as well as the launcher.
    if (process.platform === "win32") {
      spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
    } else {
      process.kill(-child.pid, "SIGTERM");
    }
  } catch {
    try { child.kill(); } catch { /* already gone */ }
  }
}

async function startServer(repo) {
  stopServer();
  const backend = findBackend();
  log("backend =", backend ? backend.cmd + " " + backend.args.join(" ") : "NOT FOUND");
  if (!backend) {
    throw new Error(
      "FORGE is not installed for any Python on this machine.\n\n" +
      'Install it with:\n  pip install "forge-runtime[api] @ ' +
      'git+https://github.com/Adeel2208/forge-runtime"'
    );
  }

  const port = await freePort();
  const key = crypto.randomBytes(18).toString("base64url");

  server = spawn(
    backend.cmd,
    [...backend.args, "studio", "--port", String(port), "--no-open"],
    {
      cwd: repo,
      // The server shells out to git, so it needs git on PATH - and a desktop
      // launch frequently has no useful PATH at all. We already know where git
      // is, so put its directory on the child's PATH rather than leaving the
      // server to fail the repository check and exit with a usage error the
      // user never sees.
      env: {
        ...process.env,
        PATH: [path.dirname(GIT), process.env.PATH].filter(Boolean).join(path.delimiter),
        FORGE_API_KEYS: `studio:${key}`,
        FORGE_REPO: repo,
      },
      detached: process.platform !== "win32",
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    }
  );

  let stderr = "";
  server.stderr.on("data", (b) => {
    const chunk = b.toString();
    stderr += chunk.slice(0, 4000);
    log("server:", chunk.trimEnd().split(/\r?\n/).slice(-1)[0]);
  });
  // stdout must be drained: an unread pipe fills, and a blocked write is
  // indistinguishable from a hung server.
  let stdout = "";
  server.stdout.on("data", (b) => { stdout += b.toString().slice(0, 4000); });
  server.on("error", (err) => log("spawn error:", err.message));
  server.on("exit", (code) => {
    log("server exited with", code);
    const why = (stderr + stdout).trim();
    if (why) {
      const tail = why.split(/\r?\n/).filter(Boolean).slice(-6).join(" | ");
      log("server said:", tail);
    }
    // A server that dies while the window is open leaves a blank app; say so
    // rather than letting the user stare at it.
    if (server && win && !win.isDestroyed()) {
      dialog.showErrorBox(
        "The FORGE server stopped",
        `It exited with code ${code}.\n\n${stderr.slice(-1200)}`
      );
    }
  });

  log("spawned server on port", port, "pid", server.pid);
  await waitForServer(port);
  log("server ready");
  current = { repo, port, key };
  saveState({ repo });
  return current;
}

/* --------------------------------------------------------------- window */

/**
 * Where git is. A packaged app launched from a menu inherits a minimal PATH,
 * so `git` frequently is not on it even on a machine that plainly has git -
 * and the failure then looks identical to "this is not a repository", which
 * sends the user to fix the wrong thing entirely.
 */
function findGit() {
  const candidates = [
    "git",
    "C:\\Program Files\\Git\\cmd\\git.exe",
    "C:\\Program Files (x86)\\Git\\cmd\\git.exe",
    "/usr/bin/git",
    "/usr/local/bin/git",
    "/opt/homebrew/bin/git",
  ];
  for (const c of candidates) {
    const probe = spawnSync(c, ["--version"], {
      encoding: "utf8", timeout: 10000, windowsHide: true,
    });
    if (probe.status === 0) return c;
  }
  return null;
}

let GIT = null;

function isRepo(dir) {
  if (!GIT) return false;
  const probe = spawnSync(GIT, ["-C", dir, "rev-parse", "--show-toplevel"], {
    encoding: "utf8",
    timeout: 10000,
    windowsHide: true,
  });
  if (probe.status !== 0) return false;
  return path.resolve(probe.stdout.trim()) === path.resolve(dir);
}

async function openRepo(repo) {
  log("openRepo", repo);
  GIT = GIT || findGit();
  log("git =", GIT || "NOT FOUND");
  if (!GIT) {
    await win.loadFile(path.join(__dirname, "boot.html"), {
      hash: encodeURIComponent(
        "Git was not found.\n\nStudio branches and commits a repository, so it " +
        "needs git on PATH. Install it, or start Studio from a shell where " +
        "`git --version` works."
      ),
    });
    return false;
  }
  if (!isRepo(repo)) {
    // "Choose another folder" is the default, and creating a repository is
    // the deliberate choice. With `git init` on the default button, a dismissed
    // dialog silently made a repository in a directory nobody meant to track -
    // which is exactly what happened to this app's own source folder while
    // testing it.
    const choice = dialog.showMessageBoxSync(win, {
      type: "question",
      buttons: ["Choose another folder", "Create a repository here", "Cancel"],
      defaultId: 0,
      cancelId: 2,
      title: "Not a git repository",
      message: `${path.basename(repo)} is not the root of a git repository.`,
      detail:
        "Studio branches and commits a whole repository, so it needs to be at " +
        "the root of one. Every task the agent runs lands on its own branch.",
    });
    if (choice === 2) return false;           // Cancel
    if (choice === 0) return chooseFolder();  // Choose another folder (default)
    log("git init requested for", repo);
    const init = spawnSync(GIT, ["-C", repo, "init"], { encoding: "utf8", windowsHide: true });
    if (init.status !== 0) {
      dialog.showErrorBox("Could not create a repository", init.stderr || "git init failed");
      return false;
    }
  }

  win.webContents.loadURL("about:blank");
  win.setTitle(`FORGE Studio — starting ${path.basename(repo)}…`);
  try {
    const { port, key } = await startServer(repo);
    // The fragment never reaches the server, so the key stays in the page.
    await win.loadURL(`http://127.0.0.1:${port}/code#key=${encodeURIComponent(key)}`);
    win.setTitle(`FORGE Studio — ${path.basename(repo)}`);
    return true;
  } catch (err) {
    await win.loadFile(path.join(__dirname, "boot.html"), {
      hash: encodeURIComponent(err.message),
    });
    win.setTitle("FORGE Studio");
    return false;
  }
}

async function chooseFolder() {
  const picked = await dialog.showOpenDialog(win, {
    title: "Open a repository",
    properties: ["openDirectory"],
  });
  if (picked.canceled || !picked.filePaths.length) return false;
  return openRepo(picked.filePaths[0]);
}

function buildMenu() {
  const isMac = process.platform === "darwin";
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      ...(isMac ? [{ role: "appMenu" }] : []),
      {
        label: "File",
        submenu: [
          { label: "Open Folder…", accelerator: "CmdOrCtrl+Shift+O", click: chooseFolder },
          {
            label: "Reopen Last Folder",
            click: () => { const s = loadState(); if (s.repo) openRepo(s.repo); },
          },
          { type: "separator" },
          isMac ? { role: "close" } : { role: "quit" },
        ],
      },
      { label: "Edit", submenu: [
        { role: "undo" }, { role: "redo" }, { type: "separator" },
        { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" },
      ] },
      { label: "View", submenu: [
        { role: "reload" }, { role: "forceReload" }, { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" },
        { type: "separator" }, { role: "togglefullscreen" },
      ] },
      { label: "Window", submenu: [{ role: "minimize" }, { role: "zoom" }] },
      {
        label: "Help",
        submenu: [
          {
            label: "Project on GitHub",
            click: () => shell.openExternal("https://github.com/Adeel2208/forge-runtime"),
          },
          {
            label: "About",
            click: () =>
              dialog.showMessageBox(win, {
                type: "info",
                title: "FORGE Studio",
                message: `FORGE Studio ${app.getVersion()}`,
                detail:
                  "A coding agent whose work you read before you merge it.\n\n" +
                  "Every task runs on its own branch. Nothing reaches yours " +
                  "until you press Merge.",
              }),
          },
        ],
      },
    ])
  );
}

function createWindow() {
  const s = loadState();
  win = new BrowserWindow({
    width: s.width || 1440,
    height: s.height || 900,
    minWidth: 900,
    minHeight: 560,
    backgroundColor: "#0c1015",
    title: "FORGE Studio",
    show: false,
    icon: path.join(__dirname, "icon.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  win.once("ready-to-show", () => win.show());
  win.on("resize", () => {
    const [width, height] = win.getSize();
    saveState({ width, height });
  });
  win.on("closed", () => { win = null; });

  // External links open in the user's browser, never inside the app frame.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  return win;
}

/* ----------------------------------------------------------- lifecycle */

app.whenReady().then(async () => {
  // The app talks to one loopback origin and nothing else. Stating that here
  // means a compromised page cannot reach out even if it wanted to.
  session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
    const url = new URL(details.url);
    const ok =
      url.hostname === "127.0.0.1" ||
      url.protocol === "devtools:" ||
      url.protocol === "file:" ||
      url.protocol === "about:";
    callback({ cancel: !ok });
  });

  buildMenu();
  createWindow();

  // In development Electron is invoked as `electron . <repo>`, so argv carries
  // the app's own path too. Taking the first existing path would pick `.` -
  // the desktop directory - and land on "not a git repository" for a launch
  // that named a perfectly good repo.
  const here = path.resolve(__dirname);
  const argRepo = process.argv
    .slice(1)
    .filter((a) => !a.startsWith("-"))
    .map((a) => path.resolve(a))
    .find((a) => a !== here && fs.existsSync(a) && fs.statSync(a).isDirectory());

  const start = argRepo || loadState().repo || process.cwd();
  log("argv =", JSON.stringify(process.argv.slice(1)), "start =", start);
  const opened = await openRepo(start);
  if (!opened && win && !win.isDestroyed()) {
    await win.loadFile(path.join(__dirname, "boot.html"));
  }
});

app.on("window-all-closed", () => {
  stopServer();
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
    const s = loadState();
    openRepo(s.repo || process.cwd());
  }
});

// Belt and braces: an orphaned server holds a lock on someone's repository.
app.on("before-quit", stopServer);
process.on("exit", stopServer);
process.on("SIGINT", () => { stopServer(); process.exit(0); });
process.on("SIGTERM", () => { stopServer(); process.exit(0); });
