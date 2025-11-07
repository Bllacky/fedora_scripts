#!/usr/bin/env python3
# Open-With sanity checker & cleaner for GNOME (Freedesktop .desktop files)
#
# New in this version:
# - Dedupe by Name only (helps when Flatpak/native have different Exec but same app)
# - Provider preference: choose which to keep among native/flatpak/snap
# - Clean ~/.config/mimeapps.list: remove duplicates & references to missing .desktop files
#
# Safe-by-default: no changes unless --fix-broken / --hide-duplicates / --fix-mimeapps is used.
#
import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Tuple, Optional

USER_DIRS = [
    os.path.expanduser('~/.local/share/applications'),
    os.path.expanduser('~/.local/share/flatpak/exports/share/applications'),
]

SYSTEM_DIRS = [
    '/usr/share/applications',
    '/var/lib/flatpak/exports/share/applications',
    '/var/lib/snapd/desktop/applications',
]

WRAPPER_CMDS = {
    'env','bash','sh','fish','zsh','/usr/bin/env','flatpak','snap',
    'python','python3','perl','ruby','java','podman','docker'
}

@dataclass
class Entry:
    path: str
    scope: str           # 'user' or 'system'
    name: str
    exec_line: str
    tryexec: str
    mimetypes: List[str]
    hidden: bool
    nodisplay: bool
    entry_type: str
    first_cmd: str
    provider: str        # 'native','flatpak','snap','other'
    broken: bool
    reason: str

def which_exists(cmd: str) -> bool:
    if not cmd:
        return False
    p = Path(cmd)
    if p.is_absolute():
        return p.exists()
    return shutil.which(cmd) is not None

def extract_first_command(exec_line: str) -> str:
    if not exec_line:
        return ''
    parts = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')+', exec_line.strip())
    if not parts:
        return ''
    first = parts[0]
    if (first.startswith('"') and first.endswith('"')) or (first.startswith("'") and first.endswith("'")):
        first = first[1:-1]
    return first

def detect_provider(path: Path, exec_line: str) -> str:
    low = str(path).lower() + ' ' + exec_line.lower()
    if 'flatpak' in low or '/var/lib/flatpak' in low:
        return 'flatpak'
    if 'snap' in low or '/var/lib/snapd' in low:
        return 'snap'
    return 'native' if which_exists(extract_first_command(exec_line)) else 'other'

def looks_broken(exec_line: str, tryexec: str) -> Tuple[bool, str]:
    if tryexec and not which_exists(tryexec):
        return True, f'TryExec not found: {tryexec}'
    cmd = extract_first_command(exec_line)
    if not cmd:
        return True, 'Empty Exec'
    if Path(cmd).name in WRAPPER_CMDS or cmd in WRAPPER_CMDS:
        if not which_exists(cmd):
            return True, f'Wrapper not found in PATH: {cmd}'
        return False, ''
    if not which_exists(cmd):
        return True, f'Executable not found: {cmd}'
    return False, ''

def read_desktop(path: Path) -> Optional[Entry]:
    try:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.read(path, encoding='utf-8')
        if 'Desktop Entry' not in cp:
            return None
        s = cp['Desktop Entry']
        entry_type = s.get('Type', 'Application')
        if entry_type.lower() != 'application':
            return None
        name = (s.get('Name') or '').strip()
        exec_line = (s.get('Exec') or '').strip()
        tryexec = (s.get('TryExec') or '').strip()
        hidden = s.getboolean('Hidden', fallback=False)
        nodisplay = s.getboolean('NoDisplay', fallback=False)
        mime_raw = (s.get('MimeType') or '').strip()
        mimetypes = [m for m in mime_raw.split(';') if m]
        if not mimetypes:
            return None
        first_cmd = extract_first_command(exec_line)
        broken, reason = looks_broken(exec_line, tryexec)
        scope = 'user' if str(path).startswith(str(Path.home())) else 'system'
        provider = detect_provider(path, exec_line)
        return Entry(
            path=str(path),
            scope=scope,
            name=name,
            exec_line=exec_line,
            tryexec=tryexec,
            mimetypes=mimetypes,
            hidden=hidden,
            nodisplay=nodisplay,
            entry_type=entry_type,
            first_cmd=first_cmd,
            provider=provider,
            broken=broken,
            reason=reason,
        )
    except Exception as e:
        return Entry(
            path=str(path),
            scope='user' if str(path).startswith(str(Path.home())) else 'system',
            name='',
            exec_line='',
            tryexec='',
            mimetypes=[],
            hidden=False,
            nodisplay=False,
            entry_type='Application',
            first_cmd='',
            provider='other',
            broken=True,
            reason=f'Parse error: {e}',
        )

def find_desktop_files() -> List[Path]:
    paths = []
    seen = set()
    for d in USER_DIRS + SYSTEM_DIRS:
        p = Path(d)
        if p.exists():
            for f in p.glob('*.desktop'):
                if f not in seen:
                    seen.add(f)
                    paths.append(f)
    return paths

def load_entries() -> List[Entry]:
    entries = []
    for p in find_desktop_files():
        e = read_desktop(p)
        if e and e.mimetypes:
            entries.append(e)
    return entries

def ensure_user_dir() -> Path:
    ud = Path.home() / '.local' / 'share' / 'applications'
    ud.mkdir(parents=True, exist_ok=True)
    return ud

def set_nodisplay_override(system_path: Path) -> Path:
    # Copy a system .desktop to user dir and set NoDisplay=true to hide it.
    ud = ensure_user_dir()
    dst = ud / system_path.name
    try:
        content = system_path.read_text(encoding='utf-8', errors='ignore')
        lines = content.splitlines()
        changed = False
        for i, line in enumerate(lines):
            if line.startswith('NoDisplay='):
                lines[i] = 'NoDisplay=true'
                changed = True
                break
        if not changed:
            lines.append('NoDisplay=true')
        dst.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return dst
    except Exception as e:
        print(f'Failed to override {system_path}: {e}', file=sys.stderr)
        return system_path

def disable_user_file(user_path: Path) -> Path:
    # Move a user .desktop into a 'disabled' folder to hide it without deletion.
    disabled_dir = Path.home() / '.local' / 'share' / 'applications' / '.disabled'
    disabled_dir.mkdir(parents=True, exist_ok=True)
    dst = disabled_dir / user_path.name
    try:
        if user_path.exists():
            shutil.move(str(user_path), str(dst))
        return dst
    except Exception as e:
        print(f'Failed to move {user_path}: {e}', file=sys.stderr)
        return user_path

def provider_rank(provider: str, prefer: str) -> int:
    pref_maps = {
        'native': {'native':0,'flatpak':1,'snap':2,'other':3},
        'flatpak': {'flatpak':0,'native':1,'snap':2,'other':3},
        'snap': {'snap':0,'native':1,'flatpak':2,'other':3},
    }
    return pref_maps.get(prefer, pref_maps['native']).get(provider, 3)

def clean_mimeapps(path: Path, existing_desktops: List[str]) -> Tuple[int,int]:
    # Return (removed_duplicates, removed_missing)
    if not path.exists():
        return (0,0)
    cfg = configparser.ConfigParser(interpolation=None, strict=False, delimiters=('='))
    with path.open('r', encoding='utf-8', errors='ignore') as f:
        cfg.read_file(f)
    changed = False
    removed_dup = 0
    removed_missing = 0
    for section in ['Added Associations','Default Applications']:
        if section in cfg:
            for key in list(cfg[section].keys()):
                val = cfg[section][key]
                items = [x for x in val.split(';') if x]
                new = []
                seen = set()
                for it in items:
                    if it not in existing_desktops:
                        removed_missing += 1
                        continue
                    if it in seen:
                        removed_dup += 1
                        continue
                    seen.add(it)
                    new.append(it)
                new_val = ';'.join(new) + (';' if new else '')
                if new_val != val:
                    cfg[section][key] = new_val
                    changed = True
    if changed:
        with path.open('w', encoding='utf-8') as f:
            cfg.write(f)
    return (removed_dup, removed_missing)

def main():
    ap = argparse.ArgumentParser(description='Open-With sanity checker & cleaner for GNOME')
    ap.add_argument('--scan', action='store_true', help='Scan and print a report (default if no action chosen)')
    ap.add_argument('--fix-broken', action='store_true', help='Hide/disable entries that look broken')
    ap.add_argument('--hide-duplicates', action='store_true', help='Hide redundant duplicates')
    ap.add_argument('--strategy', choices=['name','name+cmd','auto'], default='auto', help='Duplicate detection strategy')
    ap.add_argument('--prefer', choices=['native','flatpak','snap'], default='native', help='Which provider to keep when deduping by name')
    ap.add_argument('--fix-mimeapps', action='store_true', help='Clean ~/.config/mimeapps.list duplicates & missing targets')
    ap.add_argument('--json', type=str, help='Write a JSON report to this path')
    args = ap.parse_args()

    entries = load_entries()
    all_desktop_ids = [Path(e.path).name for e in entries]

    broken = [e for e in entries if e.broken and not e.hidden]
    by_name = defaultdict(list)
    by_name_cmd = defaultdict(list)
    for e in entries:
        by_name[e.name.strip().lower()].append(e)
        by_name_cmd[(e.name.strip().lower(), e.first_cmd.strip().lower())].append(e)

    dup_name_groups = [g for g in by_name.values() if len(g) > 1 and g[0].name]
    dup_name_cmd_groups = [g for g in by_name_cmd.values() if len(g) > 1 and g[0].name]

    if args.scan or (not args.fix_broken and not args.hide_duplicates and not args.fix_mimeapps):
        print(f'Total Open-With candidates: {len(entries)}')
        print(f'Likely broken entries: {len(broken)}')
        for e in broken:
            label = e.name if e.name else '(no name)'
            print(f'  - [{e.scope}] {label} -> {e.reason} ({e.path})')
        print(f'\nDuplicate groups by NAME: {len(dup_name_groups)}')
        for g in dup_name_groups:
            providers = ", ".join(sorted(set(x.provider for x in g)))
            print(f"  - '{g[0].name}'  x{len(g)}  providers=[{providers}]")
            for e in g:
                print(f'      [{e.provider}/{e.scope}] {e.path}')
        print(f'\nDuplicate groups by NAME+CMD: {len(dup_name_cmd_groups)}')
        for g in dup_name_cmd_groups:
            print(f"  - '{g[0].name}' via '{g[0].first_cmd}' x{len(g)}")
            for e in g:
                print(f'      [{e.provider}/{e.scope}] {e.path}')

    if args.json:
        payload = {
            'total_candidates': len(entries),
            'broken': [asdict(e) for e in broken],
            'dup_by_name': [[asdict(x) for x in g] for g in dup_name_groups],
            'dup_by_name_cmd': [[asdict(x) for x in g] for g in dup_name_cmd_groups],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding='utf-8')
        print(f'\nJSON report written to {args.json}')

    if args.fix_broken:
        print('\nHiding/Disabling broken entries...')
        for e in broken:
            p = Path(e.path)
            if e.scope == 'user':
                newp = disable_user_file(p)
                print(f'  - disabled user entry: {p} -> {newp}')
            else:
                newp = set_nodisplay_override(p)
                print(f'  - shadowed system entry with NoDisplay=true at: {newp}')

    if args.hide_duplicates:
        print('\nHiding redundant duplicates...')
        if args.strategy in ('name','auto'):
            for group in dup_name_groups:
                keep = min(group, key=lambda x: (provider_rank(x.provider, args.prefer), 0 if x.scope=='system' else 1, x.path))
                for e in group:
                    if e is keep:
                        print(f'  - keeping (by name): {e.path} [{e.provider}/{e.scope}]')
                        continue
                    if e.scope == 'user':
                        newp = disable_user_file(Path(e.path))
                        print(f'    * disabled user duplicate: {e.path} -> {newp}')
                    else:
                        newp = set_nodisplay_override(Path(e.path))
                        print(f'    * shadowed system duplicate with NoDisplay=true: {newp}')
        if args.strategy in ('name+cmd','auto'):
            for group in dup_name_cmd_groups:
                keep = None
                for e in group:
                    if e.scope == 'system':
                        keep = e
                        break
                if keep is None:
                    keep = group[0]
                for e in group:
                    if e is keep:
                        print(f"  - keeping (by name+cmd): {e.path}")
                        continue
                    if e.scope == 'user':
                        newp = disable_user_file(Path(e.path))
                        print(f'    * disabled user duplicate: {e.path} -> {newp}')
                    else:
                        newp = set_nodisplay_override(Path(e.path))
                        print(f'    * shadowed system duplicate with NoDisplay=true: {newp}')

    if args.fix_mimeapps:
        mimeapps = Path.home() / '.config' / 'mimeapps.list'
        removed_dup, removed_missing = clean_mimeapps(mimeapps, all_desktop_ids)
        print(f'\nCleaned mimeapps.list: removed {removed_dup} duplicate refs, {removed_missing} missing refs')

    if args.fix_broken or args.hide_duplicates or args.fix_mimeapps:
        try:
            subprocess.run(['update-desktop-database', str(ensure_user_dir())], check=False)
        except Exception:
            pass
        print('\nDone. You may need to restart xdg-desktop-portal (or log out/in) for menus to refresh:')
        print('  systemctl --user restart xdg-desktop-portal xdg-desktop-portal-gtk || true')

if __name__ == '__main__':
    main()
