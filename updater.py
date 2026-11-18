"""Comprobación y aplicación de actualizaciones desde GitHub para DeskCast."""
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GITHUB_REPO = 'davidlpizano/deskcast'


def get_local_version():
    v_path = os.path.join(BASE_DIR, 'VERSION')
    try:
        with open(v_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return '1.0.0'


def _parse_ver(v):
    nums = []
    for part in str(v).lstrip('vV').split('.'):
        digits = ''.join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def detect_github_repo():
    """Detecta owner/repo desde `git remote get-url origin` o usa DEFAULT_GITHUB_REPO."""
    cfg_file = os.path.expanduser('~/.config/deskcast/config.json')
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, 'r', encoding='utf-8') as f:
                repo = json.load(f).get('github_repo', '').strip()
                if '/' in repo:
                    return repo
        except Exception:
            pass

    if shutil.which('git') and os.path.isdir(os.path.join(BASE_DIR, '.git')):
        try:
            r = subprocess.run(
                ['git', '-C', BASE_DIR, 'remote', 'get-url', 'origin'],
                capture_output=True, text=True, timeout=3, check=False,
            )
            url = r.stdout.strip()
            if 'github.com' in url:
                slug = url.split('github.com')[-1].lstrip(':/').removesuffix('.git')
                if '/' in slug:
                    return slug
        except Exception:
            pass
    return DEFAULT_GITHUB_REPO


def check_for_updates():
    """
    Comprueba si hay una versión nueva en GitHub (Releases o rama main).
    Devuelve dict: {ok, has_update, local_version, remote_version, repo, notes, method, download_url, error}.
    """
    local_ver = get_local_version()
    repo = detect_github_repo()
    headers = {'User-Agent': f'DeskCast/{local_ver}', 'Accept': 'application/vnd.github+json'}

    # 1. Intentar GitHub Releases API
    rel_url = f'https://api.github.com/repos/{repo}/releases/latest'
    try:
        req = urllib.request.Request(rel_url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            tag = str(data.get('tag_name', '')).strip()
            if tag:
                has_upd = _parse_ver(tag) > _parse_ver(local_ver)
                return {
                    'ok': True,
                    'has_update': has_upd,
                    'local_version': local_ver,
                    'remote_version': tag.lstrip('vV'),
                    'repo': repo,
                    'notes': (data.get('body') or '').strip()[:500],
                    'method': 'release',
                    'download_url': data.get('zipball_url') or f'https://github.com/{repo}/archive/refs/heads/main.zip',
                    'error': None,
                }
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 403):
            return {
                'ok': False, 'has_update': False, 'local_version': local_ver,
                'remote_version': None, 'repo': repo, 'error': f'HTTP {exc.code}',
            }
    except Exception as exc:
        return {
            'ok': False, 'has_update': False, 'local_version': local_ver,
            'remote_version': None, 'repo': repo, 'error': str(exc),
        }

    # 2. Si no hay Releases aún, comprobar archivo VERSION en rama main
    raw_url = f'https://raw.githubusercontent.com/{repo}/main/VERSION'
    try:
        req = urllib.request.Request(raw_url, headers={'User-Agent': f'DeskCast/{local_ver}'})
        with urllib.request.urlopen(req, timeout=6) as resp:
            remote_ver = resp.read().decode('utf-8').strip()
            has_upd = _parse_ver(remote_ver) > _parse_ver(local_ver)
            # Comprobar también si git tiene commits pendientes aunque VERSION no haya cambiado
            git_behind = _git_commits_behind()
            return {
                'ok': True,
                'has_update': has_upd or git_behind > 0,
                'local_version': local_ver,
                'remote_version': remote_ver + (f' (+{git_behind} commits)' if git_behind > 0 and not has_upd else ''),
                'repo': repo,
                'notes': 'Actualización disponible en la rama main de GitHub.',
                'method': 'main',
                'download_url': f'https://github.com/{repo}/archive/refs/heads/main.zip',
                'error': None,
            }
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                'ok': False, 'has_update': False, 'local_version': local_ver,
                'remote_version': None, 'repo': repo,
                'error': f'Repositorio github.com/{repo} no encontrado aún. Súbelo a GitHub o configura el remoto git.',
            }
        return {
            'ok': False, 'has_update': False, 'local_version': local_ver,
            'remote_version': None, 'repo': repo, 'error': f'HTTP {exc.code}',
        }
    except Exception as exc:
        return {
            'ok': False, 'has_update': False, 'local_version': local_ver,
            'remote_version': None, 'repo': repo, 'error': str(exc),
        }


def _git_commits_behind():
    if not shutil.which('git') or not os.path.isdir(os.path.join(BASE_DIR, '.git')):
        return 0
    try:
        subprocess.run(['git', '-C', BASE_DIR, 'fetch', '--quiet'], timeout=6, check=False)
        r = subprocess.run(
            ['git', '-C', BASE_DIR, 'rev-list', '--count', 'HEAD..@{u}'],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            return int(r.stdout.strip())
    except Exception:
        pass
    return 0


def apply_update(download_url=None):
    """
    Aplica la actualización vía `git pull --ff-only` o descargando el zip de GitHub.
    Devuelve (ok: bool, msg: str).
    """
    if shutil.which('git') and os.path.isdir(os.path.join(BASE_DIR, '.git')):
        r = subprocess.run(
            ['git', '-C', BASE_DIR, 'pull', '--ff-only'],
            capture_output=True, text=True, timeout=25, check=False,
        )
        if r.returncode == 0:
            _post_update_hooks()
            return True, f'Actualizado a v{get_local_version()} correctamente. Reinicia DeskCast para aplicar.'

    if not download_url:
        repo = detect_github_repo()
        download_url = f'https://github.com/{repo}/archive/refs/heads/main.zip'

    tmp_zip = '/tmp/deskcast-update.zip'
    tmp_ext = '/tmp/deskcast-update-ext'
    try:
        req = urllib.request.Request(download_url, headers={'User-Agent': 'DeskCast-Updater'})
        with urllib.request.urlopen(req, timeout=20) as resp, open(tmp_zip, 'wb') as f:
            f.write(resp.read())
        shutil.rmtree(tmp_ext, ignore_errors=True)
        with zipfile.ZipFile(tmp_zip, 'r') as zf:
            zf.extractall(tmp_ext)
        subdirs = [os.path.join(tmp_ext, d) for d in os.listdir(tmp_ext) if os.path.isdir(os.path.join(tmp_ext, d))]
        if not subdirs:
            return False, 'Archivo ZIP vacío'
        src_root = subdirs[0]
        for root, _, files in os.walk(src_root):
            rel = os.path.relpath(root, src_root)
            dst_dir = os.path.join(BASE_DIR, rel) if rel != '.' else BASE_DIR
            os.makedirs(dst_dir, exist_ok=True)
            for fn in files:
                if fn == 'debug.keystore':
                    continue
                shutil.copy2(os.path.join(root, fn), os.path.join(dst_dir, fn))
        _post_update_hooks()
        return True, f'Actualizado a v{get_local_version()}. Reinicia DeskCast para aplicar.'
    except Exception as exc:
        return False, f'Error al actualizar: {exc}'
    finally:
        for p in (tmp_zip, tmp_ext):
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass


def _post_update_hooks():
    c_path = os.path.join(BASE_DIR, 'fast_diff.c')
    so_path = os.path.join(BASE_DIR, 'fast_diff.so')
    if os.path.isfile(c_path) and shutil.which('gcc'):
        subprocess.run(['gcc', '-O3', '-shared', '-fPIC', c_path, '-o', so_path], check=False)
    inst = os.path.join(BASE_DIR, 'instalar.sh')
    if os.path.isfile(inst):
        subprocess.run(['bash', inst], check=False)
