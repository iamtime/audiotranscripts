// Voice Rec — macOS app shell around the web UI (server.py).
//
// Starts the bundled Python (Resources/python) with server.py on a free local
// port and shows the UI in a window. Recordings and settings live in a writable
// data folder (default ~/Library/Application Support/Voice Rec/data); the
// bundled models are clone-copied into <data>/models on first launch, so the
// libraries see an ordinary writable cache and can still download extra models.
//
// Dev mode (`npm start` in app/): uses the repo sources, build/python (or
// $VOICE_REC_PYTHON) and build/models.

const { app, BrowserWindow, Menu, dialog, shell, systemPreferences } = require('electron');
const { spawn, execFile, execFileSync } = require('child_process');
const fs = require('fs');
const http = require('http');
const net = require('net');
const os = require('os');
const path = require('path');

const RES = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..', 'build');
const SRC = app.isPackaged ? path.join(RES, 'voice-rec') : path.resolve(__dirname, '..');
const PYTHON = app.isPackaged ? path.join(RES, 'python', 'bin', 'python3')
  : (process.env.VOICE_REC_PYTHON || path.join(RES, 'python', 'bin', 'python3'));
const BUNDLED_MODELS = path.join(RES, 'models');
const CONFIG = path.join(app.getPath('userData'), 'config.json');

let server = null;        // server.py child process
let serverUrl = null;
let mainWin = null;
let splash = null;
let quitting = false;

// ── Config (data folder) ────────────────────────────────────────────────────

function readConfig() {
  try { return JSON.parse(fs.readFileSync(CONFIG, 'utf8')); } catch { return {}; }
}

function writeConfig(cfg) {
  fs.mkdirSync(path.dirname(CONFIG), { recursive: true });
  fs.writeFileSync(CONFIG, JSON.stringify(cfg, null, 2));
}

function dataDir() {
  return readConfig().dataDir || path.join(app.getPath('userData'), 'data');
}

// ── Splash ──────────────────────────────────────────────────────────────────

function showSplash() {
  splash = new BrowserWindow({
    width: 420, height: 220, frame: false, resizable: false, show: false,
    backgroundColor: '#14161a', webPreferences: { contextIsolation: true },
  });
  splash.loadFile(path.join(__dirname, 'splash.html'));
  splash.once('ready-to-show', () => splash && splash.show());
  splash.webContents.once('did-finish-load', () => splashStatus(splashText));
}

let splashText = 'Starting…';

function splashStatus(text) {
  splashText = text;
  if (splash && !splash.isDestroyed() && !splash.webContents.isLoading()) {
    splash.webContents.executeJavaScript(`window.setStatus && setStatus(${JSON.stringify(text)})`).catch(() => {});
  }
}

// ── Environment ─────────────────────────────────────────────────────────────

// Apps started from Finder get a bare environment. Take the login shell's — PATH,
// and also whatever ~/.zshrc exports for the Claude Code CLI (ANTHROPIC_API_KEY,
// CLAUDE_CODE_USE_BEDROCK, CLAUDE_CONFIG_DIR, proxies, …) — so Beautify and chat
// run `claude` exactly as in Terminal. Also resolve `claude` the way the shell
// does: an alias (e.g. to ~/.claude/local/claude) is invisible on PATH.
const SHELL_ENV_SKIP = new Set(['PWD', 'OLDPWD', 'SHLVL', '_', 'TERM', 'TERM_PROGRAM', 'TERM_PROGRAM_VERSION',
  'TERM_SESSION_ID', 'PYTHONHOME', 'PYTHONPATH', 'ELECTRON_RUN_AS_NODE']);
function loginShell() {
  const fallbackPath = ['/opt/homebrew/bin', '/usr/local/bin', path.join(os.homedir(), '.local/bin'),
    '/usr/bin', '/bin', '/usr/sbin', '/sbin'].join(':');
  try {
    const out = execFileSync(process.env.SHELL || '/bin/zsh',
      ['-ilc', 'printf "__C__%s__C__" "$(command -v claude 2>/dev/null)"; printf "__E__"; /usr/bin/env -0'],
      { timeout: 8000, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], maxBuffer: 16 << 20 });
    const env = {};
    const at = out.indexOf('__E__');
    for (const kv of (at >= 0 ? out.slice(at + 5) : '').split('\0')) {
      const i = kv.indexOf('=');
      if (i > 0 && !SHELL_ENV_SKIP.has(kv.slice(0, i))) env[kv.slice(0, i)] = kv.slice(i + 1);
    }
    let claude = ((out.match(/__C__([\s\S]*?)__C__/) || [])[1] || '').trim();
    const alias = claude.match(/^(?:alias )?claude=(['"]?)(.+)\1$/);
    if (alias) claude = alias[2].trim().split(/\s+/)[0].replace(/^~(?=\/)/, os.homedir());
    if (!claude.startsWith('/')) claude = '';
    return { env, path: env.PATH || fallbackPath, claude };
  } catch {
    return { env: {}, path: fallbackPath, claude: '' };
  }
}

// Files of an app downloaded from the internet carry the quarantine flag; the
// Python libraries it loads would then be checked one by one by Gatekeeper.
function clearQuarantine() {
  if (!app.isPackaged) return;
  const bundle = path.resolve(RES, '..', '..');
  execFile('/usr/bin/xattr', ['-dr', 'com.apple.quarantine', bundle], () => {});
}

function copyClone(src, dst) {
  return new Promise((resolve, reject) => {
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    const tmp = dst + '.partial';
    fs.rmSync(tmp, { recursive: true, force: true });
    // -c: APFS clone (instant, no extra disk space) when on the same volume.
    execFile('/bin/cp', ['-cR', src, tmp], (err) => {
      if (err) return reject(err);
      fs.renameSync(tmp, dst);
      resolve();
    });
  });
}

// Copy every bundled model the user's cache doesn't have yet. Entries are whole
// model folders (hf/hub/models--*, torch/hub/*, easyocr/model/*).
async function seedModels(models) {
  if (!fs.existsSync(BUNDLED_MODELS)) return;
  const entries = [];
  for (const sub of ['hf/hub', 'torch/hub', 'easyocr/model']) {
    const dir = path.join(BUNDLED_MODELS, sub);
    if (!fs.existsSync(dir)) continue;
    for (const name of fs.readdirSync(dir)) {
      if (name.startsWith('.')) continue;
      const dst = path.join(models, sub, name);
      if (!fs.existsSync(dst)) entries.push([path.join(dir, name), dst, name]);
    }
  }
  for (let i = 0; i < entries.length; i++) {
    const [src, dst, name] = entries[i];
    splashStatus(`Preparing models (${i + 1}/${entries.length}): ${name.replace(/^models--/, '').replace('--', '/')}`);
    await copyClone(src, dst);
  }
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.unref();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

function getJson(url, timeout = 3000) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { timeout }, (res) => {
      let body = '';
      res.on('data', (d) => { body += d; });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch (e) { reject(e); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject);
  });
}

// ── server.py ───────────────────────────────────────────────────────────────

async function startServer() {
  const data = dataDir();
  const models = path.join(data, 'models');
  const logs = path.join(data, 'logs');
  fs.mkdirSync(logs, { recursive: true });
  await seedModels(models);

  splashStatus('Starting…');
  const port = await freePort();
  const pyBin = path.dirname(PYTHON);
  const shell = loginShell();
  const env = {
    ...process.env,
    ...shell.env,
    PATH: [path.join(pyBin, '..', 'extra-bin'), pyBin, shell.path].join(':'),
    ...(shell.claude ? { VOICE_REC_CLAUDE: shell.claude } : {}),
    VOICE_REC_DATA: data,
    VOICE_REC_RECORDER_PYTHON: PYTHON,
    VOICE_REC_SILERO_DIR: path.join(models, 'torch', 'hub', 'snakers4_silero-vad_master'),
    HF_HOME: path.join(models, 'hf'),
    TORCH_HOME: path.join(models, 'torch'),
    EASYOCR_MODULE_PATH: path.join(models, 'easyocr'),
    HF_HUB_DISABLE_TELEMETRY: '1',
    // Plain HTTP downloads grow the file as they go, so downloads.py can show progress.
    HF_HUB_DISABLE_XET: '1',
    TOKENIZERS_PARALLELISM: 'false',
    PYTHONNOUSERSITE: '1',
    PYTHONUNBUFFERED: '1',
    ...(app.isPackaged ? { PYTHONDONTWRITEBYTECODE: '1' } : {}),
  };
  delete env.PYTHONHOME;
  delete env.PYTHONPATH;
  delete env.ELECTRON_RUN_AS_NODE;

  const log = fs.createWriteStream(path.join(logs, 'server.log'), { flags: 'a' });
  log.write(`\n=== ${new Date().toISOString()} Voice Rec ${app.getVersion()} port ${port}\n`);
  server = spawn(PYTHON, ['-u', path.join(SRC, 'server.py'), '--port', String(port), '--no-browser'],
    { cwd: data, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let tail = '';
  const onOut = (d) => {
    log.write(d);
    tail = (tail + d).slice(-4000);
  };
  server.stdout.on('data', onOut);
  server.stderr.on('data', onOut);
  server.on('exit', (code, sig) => {
    log.write(`=== server exited: ${code ?? sig}\n`);
    server = null;
    if (!quitting) {
      dialog.showErrorBox('Voice Rec stopped',
        `The recognition server exited (${code ?? sig}).\n\n${tail.slice(-1500)}\n\nLog: ${path.join(logs, 'server.log')}`);
      app.quit();
    }
  });

  serverUrl = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 180000;   // first start compiles nothing, but imports torch & co.
  while (Date.now() < deadline) {
    if (!server) throw new Error(`Server failed to start:\n${tail.slice(-1500)}`);
    try {
      await getJson(`${serverUrl}/api/status`);
      return;
    } catch { await new Promise((r) => setTimeout(r, 300)); }
  }
  throw new Error('The server did not answer in 3 minutes');
}

function stopServer() {
  return new Promise((resolve) => {
    if (!server) return resolve();
    const proc = server;
    proc.once('exit', () => resolve());
    proc.kill('SIGTERM');   // uvicorn shutdown → server stops a running recording cleanly
    setTimeout(() => { if (proc.exitCode === null) proc.kill('SIGKILL'); }, 600000);
  });
}

async function recorderActive() {
  try {
    const st = await getJson(`${serverUrl}/api/status`, 1500);
    return !!(st.recorder && st.recorder.active);
  } catch { return false; }
}

// ── Windows ─────────────────────────────────────────────────────────────────

function sameOrigin(url) {
  return serverUrl && url.startsWith(serverUrl);
}

function wireLinks(win) {
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (sameOrigin(url)) {
      return { action: 'allow', overrideBrowserWindowOptions: { width: 1100, height: 900, backgroundColor: '#ffffff' } };
    }
    shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (!sameOrigin(url)) { e.preventDefault(); shell.openExternal(url); }
  });
  win.webContents.on('did-create-window', (child) => wireLinks(child));
}

function createMainWindow() {
  mainWin = new BrowserWindow({
    width: 1440, height: 920, minWidth: 900, minHeight: 600, show: false,
    title: 'Voice Rec', backgroundColor: '#14161a',
    webPreferences: { contextIsolation: true, spellcheck: true },
  });
  wireLinks(mainWin);
  mainWin.loadURL(serverUrl);
  mainWin.once('ready-to-show', () => {
    mainWin.show();
    if (splash && !splash.isDestroyed()) splash.close();
    splash = null;
  });
  mainWin.on('close', (e) => {
    if (quitting) return;
    e.preventDefault();
    requestQuit();
  });
  mainWin.on('closed', () => { mainWin = null; });
}

let askingToQuit = false;

async function requestQuit() {
  if (askingToQuit) return;
  askingToQuit = true;
  try {
    if (await recorderActive()) {
      const { response } = await dialog.showMessageBox(mainWin, {
        type: 'warning', buttons: ['Stop and Quit', 'Cancel'], defaultId: 1, cancelId: 1,
        message: 'A recording is in progress',
        detail: 'Quitting stops it; the transcript recorded so far is kept.',
      });
      if (response !== 0) return;
    }
    quitting = true;
    if (mainWin) mainWin.hide();
    await stopServer();
    app.quit();
  } finally {
    askingToQuit = false;
  }
}

// ── Menu ────────────────────────────────────────────────────────────────────

async function changeDataFolder() {
  const { canceled, filePaths } = await dialog.showOpenDialog({
    title: 'Choose the data folder (projects/ and settings live there)',
    defaultPath: dataDir(), properties: ['openDirectory', 'createDirectory'],
  });
  if (canceled || !filePaths[0]) return;
  if (await recorderActive()) {
    dialog.showErrorBox('Recording in progress', 'Stop the recording before changing the data folder.');
    return;
  }
  writeConfig({ ...readConfig(), dataDir: filePaths[0] });
  app.relaunch();
  app.quit();
}

function buildMenu() {
  const template = [
    { role: 'appMenu' },
    {
      label: 'File',
      submenu: [
        { label: 'Open Data Folder', click: () => shell.openPath(dataDir()) },
        { label: 'Change Data Folder…', click: changeDataFolder },
        { label: 'Show Server Log', click: () => shell.openPath(path.join(dataDir(), 'logs', 'server.log')) },
        { type: 'separator' },
        { role: 'close' },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { role: 'reload' }, { role: 'forceReload' }, { role: 'toggleDevTools' }, { type: 'separator' },
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
    {
      role: 'help',
      submenu: [
        {
          label: 'Privacy Settings: Microphone',
          click: () => shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone'),
        },
        {
          label: 'Privacy Settings: Screen Recording',
          click: () => shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'),
        },
        {
          label: 'Privacy Settings: Accessibility',
          click: () => shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── Lifecycle ───────────────────────────────────────────────────────────────

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWin) { if (mainWin.isMinimized()) mainWin.restore(); mainWin.focus(); }
  });

  app.whenReady().then(async () => {
    buildMenu();
    if (app.isPackaged && !app.isInApplicationsFolder()) {
      const { response } = await dialog.showMessageBox({
        type: 'question', buttons: ['Move to Applications', 'Not Now'], defaultId: 0, cancelId: 1,
        message: 'Move Voice Rec to the Applications folder?',
        detail: 'Run from the disk image, Voice Rec has to copy its bundled models to your disk; from Applications they take no extra space.',
      });
      if (response === 0) {
        try { if (app.moveToApplicationsFolder()) return; } catch (e) { dialog.showErrorBox('Could not move', String(e)); }
      }
    }
    clearQuarantine();
    showSplash();
    // Ask for the microphone up front, so the prompt names Voice Rec (the
    // recorder is a child process and inherits the app's permission).
    systemPreferences.askForMediaAccess('microphone').catch(() => {});
    try {
      await startServer();
      createMainWindow();
    } catch (e) {
      dialog.showErrorBox('Voice Rec could not start', `${e.message || e}\n\nLog: ${path.join(dataDir(), 'logs', 'server.log')}`);
      quitting = true;
      await stopServer();
      app.exit(1);
    }
  });

  app.on('before-quit', (e) => {
    if (quitting || !server) { quitting = true; return; }
    e.preventDefault();
    requestQuit();
  });

  app.on('window-all-closed', () => app.quit());
}
