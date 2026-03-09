"""Flow2API Host Agent - Web UI"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import BackgroundTasks, FastAPI, Form
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'scripts'))
from core import health_report, load_config, read_json  # noqa: E402

CFG_PATH = str(BASE / 'agent.toml')
VENV_PYTHON = str(BASE / '.venv' / 'bin' / 'python')
DEFAULT_NOVNC = 'http://localhost:6080/vnc.html?autoconnect=true&resize=scale&quality=6'
DISPLAY_TZ = ZoneInfo('Asia/Shanghai')
UPDATE_STATE_PATH = BASE / 'update_state.json'
BACKUP_ROOT = BASE / 'backups'
RELEASE_CACHE_TTL = 600
UPDATE_SERVICE_NAMES = [
    'flow2api-host-agent.service',
    'flow2api-host-agent-ui.service',
]
SYNC_ITEMS = [
    'LICENSE',
    'README.md',
    'requirements.txt',
    'install-systemd.sh',
    'agent.example.toml',
    'assets',
    'docs',
    'scripts',
    'systemd',
    'web',
]
app = FastAPI(title='Flow2API Host Agent')
templates = Jinja2Templates(directory=str(BASE / 'web' / 'templates'))


def _read_update_state() -> dict:
    if UPDATE_STATE_PATH.exists():
        try:
            return json.loads(UPDATE_STATE_PATH.read_text('utf-8'))
        except Exception:
            pass
    return {
        'checked_at': 0,
        'current_version': 'unknown',
        'latest_version': None,
        'update_available': False,
        'release_url': None,
        'message': '未检查更新',
        'updating': False,
        'repo': None,
        'backup_dir': None,
        'last_error': None,
    }


def _write_update_state(data: dict) -> dict:
    current = _read_update_state()
    current.update(data)
    UPDATE_STATE_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return current


def _run_cmd(cmd: str) -> dict:
    python_bin = VENV_PYTHON if Path(VENV_PYTHON).exists() else sys.executable
    result = subprocess.run(
        [python_bin, str(BASE / 'scripts' / 'agent.py'), '--config', CFG_PATH, cmd],
        capture_output=True, text=True
    )
    stdout = (result.stdout or '').strip().splitlines()
    candidate = stdout[-1] if stdout else ''
    try:
        return json.loads(candidate)
    except Exception:
        return {
            'error': (result.stderr[:500] if result.stderr else 'no output'),
            'raw_stdout': result.stdout[:1000] if result.stdout else ''
        }


def _write_config(cfg: dict) -> None:
    lines = []
    for k, v in cfg.items():
        if isinstance(v, bool):
            lines.append(f'{k} = {str(v).lower()}')
        elif isinstance(v, (int, float)):
            lines.append(f'{k} = {v}')
        else:
            escaped = str(v).replace('"', '\\"')
            lines.append(f'{k} = "{escaped}"')
    Path(CFG_PATH).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _restart_service(name: str) -> None:
    subprocess.run(['systemctl', 'restart', name], capture_output=True, text=True)


def _restart_daemon() -> None:
    _restart_service('flow2api-host-agent.service')


def _restart_browser() -> None:
    _restart_service('flow2api-host-agent-browser.service')


def _restart_ui() -> None:
    _restart_service('flow2api-host-agent-ui.service')


def _fmt_local(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=DISPLAY_TZ).strftime('%Y-%m-%d %H:%M:%S') + ' (UTC+8)'


def _git(*args: str) -> str:
    result = subprocess.run(['git', '-C', str(BASE), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or 'git failed').strip())
    return (result.stdout or '').strip()


def _parse_repo_slug() -> str:
    cfg = load_config(CFG_PATH)
    explicit = (cfg.get('github_repo') or '').strip().strip('/')
    if explicit:
        return explicit
    try:
        remote = _git('remote', 'get-url', 'origin').strip()
    except Exception:
        return ''
    remote = remote.replace('.git', '').strip()
    m = re.search(r'github\.com[:/](.+/.+)$', remote)
    return m.group(1) if m else ''


def _current_version() -> str:
    try:
        return _git('describe', '--tags', '--always', '--dirty')
    except Exception:
        try:
            return _git('rev-parse', '--short', 'HEAD')
        except Exception:
            return 'unknown'


def _version_parts(v: str) -> list[int]:
    nums = re.findall(r'\d+', (v or '').lower())
    return [int(x) for x in nums[:4]] or [0]


def _is_newer(latest: str, current: str) -> bool:
    return _version_parts(latest) > _version_parts(current)


def _safe_extract_tar(tar_path: Path, target: Path) -> None:
    with tarfile.open(tar_path, 'r:*') as tf:
        for member in tf.getmembers():
            member_path = target / member.name
            if not str(member_path.resolve()).startswith(str(target.resolve())):
                raise RuntimeError(f'非法 tar 路径: {member.name}')
        tf.extractall(target)


def _copy_item(src: Path, dst: Path) -> None:
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _backup_repo() -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    backup_dir = BACKUP_ROOT / f'backup-{int(time.time())}'
    backup_dir.mkdir(parents=True, exist_ok=True)
    for rel in SYNC_ITEMS + ['agent.toml', 'update_state.json']:
        src = BASE / rel
        if src.exists():
            _copy_item(src, backup_dir / rel)
    return backup_dir


def _restore_backup(backup_dir: Path) -> None:
    for rel in SYNC_ITEMS + ['agent.toml', 'update_state.json']:
        src = backup_dir / rel
        if src.exists():
            _copy_item(src, BASE / rel)


def _latest_release(force: bool = False) -> dict:
    state = _read_update_state()
    now = int(time.time())
    if not force and state.get('checked_at') and now - int(state.get('checked_at') or 0) < RELEASE_CACHE_TTL:
        return state

    repo = _parse_repo_slug()
    current = _current_version()
    if not repo:
        return _write_update_state({
            'checked_at': now,
            'current_version': current,
            'latest_version': None,
            'update_available': False,
            'release_url': None,
            'repo': None,
            'message': '未发现 GitHub 仓库配置/remote，无法检查 release',
            'last_error': None,
        })

    try:
        resp = requests.get(
            f'https://api.github.com/repos/{repo}/releases/latest',
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'flow2api-host-agent-updater'},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        latest = data.get('tag_name') or data.get('name') or 'unknown'
        release_url = data.get('html_url')
        tarball_url = data.get('tarball_url')
        available = _is_newer(latest, current)
        message = f'当前 {current}，最新 {latest}' + ('，可更新' if available else '，已是最新')
        return _write_update_state({
            'checked_at': now,
            'current_version': current,
            'latest_version': latest,
            'update_available': available,
            'release_url': release_url,
            'tarball_url': tarball_url,
            'repo': repo,
            'message': message,
            'last_error': None,
        })
    except Exception as e:
        return _write_update_state({
            'checked_at': now,
            'current_version': current,
            'latest_version': None,
            'update_available': False,
            'release_url': None,
            'repo': repo,
            'message': f'检查更新失败：{e}',
            'last_error': str(e),
        })


def _schedule_restart_and_verify(backup_dir: Path, target_version: str) -> None:
    py = f"""
import json, pathlib, shutil, subprocess, time, urllib.request
base = pathlib.Path({str(BASE)!r})
backup = pathlib.Path({str(backup_dir)!r})
state_path = pathlib.Path({str(UPDATE_STATE_PATH)!r})
target_version = {target_version!r}
services = {UPDATE_SERVICE_NAMES!r}
sync_items = {SYNC_ITEMS!r}

def write_state(**kw):
    try:
        data = json.loads(state_path.read_text('utf-8')) if state_path.exists() else {{}}
    except Exception:
        data = {{}}
    data.update(kw)
    state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')

for svc in services:
    subprocess.run(['systemctl', 'restart', svc], check=False)

time.sleep(5)
ok = False
for _ in range(8):
    try:
        with urllib.request.urlopen('http://127.0.0.1:38110/api/status', timeout=5) as r:
            if getattr(r, 'status', 200) == 200:
                ok = True
                break
    except Exception:
        time.sleep(2)

if ok:
    write_state(
        updating=False,
        update_available=False,
        current_version=target_version,
        message=f'已更新到 {{target_version}}，UI/daemon 已重启并通过本地探活',
        last_error=None,
    )
else:
    for rel in sync_items + ['agent.toml', 'update_state.json']:
        src = backup / rel
        dst = base / rel
        if src.exists():
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    for svc in services:
        subprocess.run(['systemctl', 'restart', svc], check=False)
    write_state(
        updating=False,
        message='更新后探活失败，已自动回滚到更新前版本',
        last_error='post-restart healthcheck failed; rollback completed',
    )
"""
    cmd = 'sleep 1; ' + subprocess.list2cmdline([str(Path(VENV_PYTHON) if Path(VENV_PYTHON).exists() else Path(sys.executable)), '-c', py])
    subprocess.Popen(['/bin/sh', '-c', cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _perform_update_job() -> None:
    state = _latest_release(force=True)
    if state.get('updating'):
        return
    _write_update_state({'updating': True, 'message': '正在下载并部署最新 release…', 'last_error': None})
    backup_dir = None
    try:
        state = _latest_release(force=True)
        if not state.get('update_available'):
            _write_update_state({'updating': False, 'message': state.get('message') or '当前已是最新版本'})
            return
        tarball_url = state.get('tarball_url')
        if not tarball_url:
            raise RuntimeError('latest release 未返回 tarball_url')

        with tempfile.TemporaryDirectory(prefix='host-agent-update-') as tmp:
            tmpdir = Path(tmp)
            archive = tmpdir / 'release.tar.gz'
            resp = requests.get(tarball_url, headers={'User-Agent': 'flow2api-host-agent-updater'}, timeout=120)
            resp.raise_for_status()
            archive.write_bytes(resp.content)

            extract_dir = tmpdir / 'extract'
            extract_dir.mkdir(parents=True, exist_ok=True)
            _safe_extract_tar(archive, extract_dir)

            roots = [p for p in extract_dir.iterdir() if p.is_dir()]
            if not roots:
                raise RuntimeError('release tarball 内容为空')
            src_root = roots[0]

            backup_dir = _backup_repo()
            _write_update_state({'backup_dir': str(backup_dir)})

            for rel in SYNC_ITEMS:
                src = src_root / rel
                if src.exists():
                    _copy_item(src, BASE / rel)

            py_bin = Path(VENV_PYTHON) if Path(VENV_PYTHON).exists() else Path(sys.executable)
            subprocess.run([str(py_bin), '-m', 'pip', 'install', '-q', '-r', str(BASE / 'requirements.txt')], check=True, timeout=300)
            subprocess.run([str(py_bin), '-m', 'compileall', str(BASE / 'scripts'), str(BASE / 'web')], check=True, timeout=120)

        _write_update_state({
            'current_version': state.get('current_version'),
            'latest_version': state.get('latest_version'),
            'update_available': False,
            'message': f"代码已切换到 {state.get('latest_version')}，正在重启服务并做探活校验…",
        })
        _schedule_restart_and_verify(backup_dir, str(state.get('latest_version') or _current_version()))
    except Exception as e:
        if backup_dir is not None:
            try:
                _restore_backup(backup_dir)
                subprocess.run([str(Path(VENV_PYTHON) if Path(VENV_PYTHON).exists() else Path(sys.executable)), '-m', 'compileall', str(BASE / 'scripts'), str(BASE / 'web')], check=False, timeout=120)
            except Exception:
                pass
        _write_update_state({'updating': False, 'message': f'更新失败：{e}', 'last_error': str(e)})


def _get_context(force_release_check: bool = False):
    cfg = load_config(CFG_PATH)
    cfg.setdefault('novnc_url', DEFAULT_NOVNC)
    state = read_json(cfg['state_file']) or {}
    status = _run_cmd('status')
    health = health_report(cfg, status=status, state=state)
    update = _latest_release(force=force_release_check)
    last_update_display = '—'
    next_refresh_display = '—'
    next_refresh_ts = None
    if state.get('time'):
        try:
            last_update_display = _fmt_local(int(state['time']))
            next_ts = int(state['time']) + int(cfg.get('refresh_interval_minutes', 30)) * 60
            next_refresh_ts = next_ts
            next_refresh_display = _fmt_local(next_ts)
        except Exception:
            pass
    return cfg, state, status, health, update, last_update_display, next_refresh_display, next_refresh_ts


@app.get('/', response_class=HTMLResponse)
def index(request: Request):
    cfg, state, status, health, update, last_update_display, next_refresh_display, next_refresh_ts = _get_context()
    return templates.TemplateResponse('index.html', {
        'request': request,
        'cfg': cfg,
        'state': state,
        'status': status,
        'health': health,
        'update': update,
        'last_update_display': last_update_display,
        'next_refresh_display': next_refresh_display,
        'next_refresh_ts': next_refresh_ts,
    })


@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request):
    cfg, _state, status, health, _update, _, _, _ = _get_context()
    novnc_url = cfg.get('novnc_url', DEFAULT_NOVNC)
    return templates.TemplateResponse('login.html', {
        'request': request,
        'cfg': cfg,
        'status': status,
        'novnc_url': novnc_url,
        'health': health,
    })


@app.get('/api/status')
def api_status():
    return _run_cmd('status')


@app.get('/api/health')
def api_health():
    cfg = load_config(CFG_PATH)
    state = read_json(cfg['state_file']) or {}
    status = {
        'chrome_running': _run_cmd('status').get('chrome_running', False),
        'debug_port': cfg['remote_debugging_port'],
        'profile_dir': cfg['chrome_profile_dir'],
        'last_state': state,
    }
    return health_report(cfg, status=status, state=state)


@app.get('/api/update-status')
def api_update_status(force: int = 0):
    return JSONResponse(_latest_release(force=bool(force)))


@app.post('/action/launch-browser')
def action_launch_browser():
    _restart_browser()
    return RedirectResponse('/login', status_code=303)


@app.post('/action/run-once')
def action_run_once():
    result = _run_cmd('run-once')
    flag = '1' if result.get('success', False) else '0'
    return RedirectResponse(f'/?refreshed={flag}', status_code=303)


@app.post('/action/check-update')
def action_check_update():
    _latest_release(force=True)
    return RedirectResponse('/?checked_update=1', status_code=303)


@app.post('/action/update-release')
def action_update_release(background_tasks: BackgroundTasks):
    state = _read_update_state()
    if state.get('updating'):
        return RedirectResponse('/?update_started=1', status_code=303)
    background_tasks.add_task(_perform_update_job)
    _write_update_state({'updating': True, 'message': '更新任务已启动，正在后台拉取最新 release…'})
    return RedirectResponse('/?update_started=1', status_code=303)


@app.post('/action/save')
def action_save(
    flow2api_url: str = Form(...),
    connection_token: str = Form(...),
    chrome_profile_dir: str = Form(...),
    remote_debugging_port: int = Form(...),
    display: str = Form(...),
    refresh_interval_minutes: int = Form(...),
    novnc_url: str = Form(''),
    github_repo: str = Form(''),
):
    cfg = load_config(CFG_PATH)
    cfg.update({
        'flow2api_url': flow2api_url,
        'connection_token': connection_token,
        'chrome_profile_dir': chrome_profile_dir,
        'remote_debugging_port': int(remote_debugging_port),
        'display': display,
        'refresh_interval_minutes': int(refresh_interval_minutes),
        'novnc_url': novnc_url or DEFAULT_NOVNC,
        'github_repo': github_repo.strip(),
    })
    _write_config(cfg)
    _restart_daemon()
    return RedirectResponse('/?saved=1', status_code=303)
