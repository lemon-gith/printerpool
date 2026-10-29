"""Backups: a zip (optionally AES-256 encrypted) holding a pg_dump of the database and, optionally, uploaded files.

    printpool-20261029-033000.zip
        database.dump   pg_dump custom format, restored with pg_restore
        media/...       uploaded model files (only with include_files)
        manifest.json   what's inside, and when it was made
"""

import json
import logging
import os
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

import pyzipper
from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone

log = logging.getLogger(__name__)

PREFIX = "printpool-"
SUFFIX = ".zip"
DUMP_NAME = "database.dump"
MEDIA_PREFIX = "media/"


class BackupError(Exception):
    pass


def _postgres():
    """Environment and connection arguments for pg_dump/pg_restore. The password goes via the environment, so
    it never shows up in a process list."""
    db = settings.DATABASES["default"]
    env = os.environ | {"PGPASSWORD": db["PASSWORD"] or ""}
    return env, ["-h", db["HOST"], "-p", str(db["PORT"]), "-U", db["USER"], "-d", db["NAME"]]


def _run(cmd, env=None):
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise BackupError(f"{cmd[0]} isn't installed in this container")
    except subprocess.CalledProcessError as e:
        raise BackupError(f"{cmd[0]} failed: {(e.stderr or e.stdout).strip()[:500]}")


def _media_root():
    return Path(default_storage.location)


def _hand_to_owner_of(path, folder):
    """When running as root (the worker), give new files to whoever owns the folder they're in, so they're
    usable on the host. Backups hold everything, so only the owner may read them."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        st = Path(folder).stat()
        os.chown(path, st.st_uid, st.st_gid)
    os.chmod(path, 0o600)


def _give_tree_to_owner(root):
    """After writing as root, hand everything under root (folders included) back to root's owner, so the app,
    which runs as an ordinary user, can still save and delete uploads there."""
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    st = Path(root).stat()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            os.lchown(os.path.join(dirpath, name), st.st_uid, st.st_gid)


def create_backup(output_dir, *, encrypt, password, include_files, keep, now=None):
    """Write a backup into output_dir, then delete the oldest ones beyond `keep` (0 keeps all).
    Returns (path of the new backup, list of deleted paths)."""
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        raise BackupError(f"backup folder {output_dir} doesn't exist")
    if encrypt and not password:
        raise BackupError("encryption is on but BACKUP_PASSWORD is empty (set it in .env)")

    created = timezone.localtime(now or timezone.now())
    name = f"{PREFIX}{created:%Y%m%d-%H%M%S}{SUFFIX}"
    final = output_dir / name
    partial = output_dir / f".{name}.partial"
    env, connection = _postgres()

    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / DUMP_NAME
        _run(["pg_dump", "--format=custom", "--no-owner", *connection, "--file", str(dump)], env=env)
        # Check the dump is readable before keeping it, so a broken file never pushes out a good backup.
        _run(["pg_restore", "--list", str(dump)])

        media_files = []
        if include_files and _media_root().is_dir():
            media_files = [p for p in _media_root().rglob("*") if p.is_file()]
        manifest = {
            "created": created.isoformat(),
            "database": DUMP_NAME,
            "includes_files": include_files,
            "file_count": len(media_files),
        }
        try:
            options = {"compression": pyzipper.ZIP_DEFLATED}
            if encrypt:
                options["encryption"] = pyzipper.WZ_AES  # AES-256, as opened by 7-Zip and others
            with pyzipper.AESZipFile(partial, "w", **options) as zf:
                if encrypt:
                    zf.setpassword(password.encode())
                zf.write(dump, DUMP_NAME)
                for path in media_files:
                    zf.write(path, MEDIA_PREFIX + path.relative_to(_media_root()).as_posix())
                zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            _hand_to_owner_of(partial, output_dir)
            os.replace(partial, final)  # appears complete or not at all
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    return final, prune(output_dir, keep)


def list_backups(folder):
    """Backups in folder, oldest first (the names sort by time)."""
    return sorted(p for p in Path(folder).glob(f"{PREFIX}*{SUFFIX}") if p.is_file())


def prune(folder, keep, now=None):
    """Keep the newest `keep` backups (0 keeps all). Also clears partial files left by an interrupted backup."""
    removed = []
    if keep > 0:
        for path in list_backups(folder)[:-keep]:
            path.unlink()
            removed.append(path)
    cutoff = (now or timezone.now()).timestamp() - timedelta(days=1).total_seconds()
    for path in Path(folder).glob(f".{PREFIX}*.partial"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
    return removed


def is_encrypted(zip_path):
    with pyzipper.AESZipFile(zip_path) as zf:
        return any(info.flag_bits & 0x1 for info in zf.infolist())


def restore_backup(zip_path, *, password, restore_files=True):
    """Replace the database (and, if the backup has them and restore_files is set, uploaded files) with a backup.
    Runs in one transaction: if anything fails, the database is left as it was."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise BackupError(f"{zip_path} not found")
    env, connection = _postgres()
    with pyzipper.AESZipFile(zip_path) as zf, tempfile.TemporaryDirectory() as tmp:
        names = zf.namelist()
        if DUMP_NAME not in names:
            raise BackupError(f"{zip_path.name} isn't a PrintPool backup (no {DUMP_NAME} inside)")
        encrypted = any(info.flag_bits & 0x1 for info in zf.infolist())
        if encrypted:
            if not password:
                raise BackupError("this backup is encrypted; set BACKUP_PASSWORD")
            zf.setpassword(password.encode())
        try:
            zf.extract(DUMP_NAME, tmp)
        except RuntimeError:
            raise BackupError("wrong password for this backup")
        _run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--single-transaction", "--exit-on-error",
              *connection, str(Path(tmp) / DUMP_NAME)], env=env)

        media = [n for n in names if n.startswith(MEDIA_PREFIX) and not n.endswith("/")]
        if restore_files and media:
            root = _media_root().resolve()
            for name in media:
                target = (root / name[len(MEDIA_PREFIX):]).resolve()
                if root not in target.parents:
                    raise BackupError(f"refusing to restore {name}: it points outside the uploads folder")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(name))
            _give_tree_to_owner(root)
        return {"files_restored": len(media) if restore_files else 0, "files_in_backup": len(media)}
