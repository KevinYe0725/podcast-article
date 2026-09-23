"""Host-local administration commands for an invite-only installation."""
from __future__ import annotations

import argparse
import base64
import fcntl
import getpass
import hashlib
import os
import re
import secrets
import sqlite3
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from . import auth
from .platform_store import AccountQuota, InviteError, PlatformStore
from .workspace import data_root


def _store() -> PlatformStore:
    return PlatformStore(data_root() / "platform.sqlite")


def _read_password(prompt: str) -> str:
    password = getpass.getpass(prompt)
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise ValueError("passwords do not match")
    auth.validate_new_password(password)
    return password


def _secrets_key(env_file: Path) -> None:
    path = Path(env_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("refusing to write a key through a symlink")
    flags = os.O_CREAT | os.O_RDWR | os.O_APPEND
    descriptor = os.open(path, flags, 0o600)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8", newline="") as stream:
            descriptor = -1
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            existing = stream.read()
            if re.search(r"(?m)^\s*(?:export\s+)?PA_USER_SECRETS_KEY\s*=", existing):
                raise FileExistsError("PA_USER_SECRETS_KEY already exists; refusing to overwrite it")
            key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
            if existing and not existing.endswith(("\n", "\r")):
                stream.write("\n")
            stream.write(f"PA_USER_SECRETS_KEY={key}\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _valid_base_url(raw: str) -> str:
    value = raw.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base URL must be an HTTP or HTTPS origin")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL cannot contain a query or fragment")
    return value


def _find_account(store: PlatformStore, username: str):
    credential = store.credential_for_username(username)
    if credential is None:
        raise ValueError("account not found")
    return credential.account


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="podcast-admin", description="本机管理员命令；密码只通过安全提示输入")
    commands = parser.add_subparsers(dest="command", required=True)

    bootstrap = commands.add_parser("bootstrap", help="创建首个管理员账号")
    bootstrap.add_argument("--username", required=True)

    key = commands.add_parser("secrets-key", help="在环境文件中创建用户密钥加密密钥")
    key.add_argument("--env-file", type=Path, default=Path(os.environ.get("PA_SERVER_ENV_FILE", ".env")))

    invite = commands.add_parser("invite", help="管理一次性邀请")
    invite_commands = invite.add_subparsers(dest="invite_command", required=True)
    create = invite_commands.add_parser("create", help="创建邀请链接")
    create.add_argument("--admin-username", required=True)
    create.add_argument("--asr-month-seconds", required=True, type=int)
    create.add_argument("--llm-month-cny", required=True, type=Decimal)
    create.add_argument("--cache-bytes", required=True, type=int)
    create.add_argument("--queue-items", required=True, type=int)
    create.add_argument("--max-upload-bytes", required=True, type=int)
    create.add_argument("--expires-days", type=int, default=7)
    create.add_argument("--base-url", default="https://podcast.squareconf.cn")
    revoke = invite_commands.add_parser("revoke", help="撤销一次性邀请")

    user = commands.add_parser("user", help="管理账号")
    user_commands = user.add_subparsers(dest="user_command", required=True)
    for name, help_text in (("disable", "停用账号并撤销会话"), ("enable", "启用账号"), ("reset-password", "重设密码并要求下次登录修改")):
        subcommand = user_commands.add_parser(name, help=help_text)
        subcommand.add_argument("--username", required=True)

    # The dry-run/apply migration command is added alongside the migration implementation in Task 10.
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "secrets-key":
        _secrets_key(args.env_file)
        print(f"PA_USER_SECRETS_KEY added to {args.env_file}")
        return 0

    store = _store()
    if args.command == "bootstrap":
        password = _read_password("New administrator password: ")
        account = store.bootstrap_admin(args.username, auth.hash_password(password), now=time.time())
        print(f"Administrator account '{account.username}' created.")
        return 0

    if args.command == "invite":
        if args.invite_command == "create":
            account = _find_account(store, args.admin_username)
            if account.role != "admin" or not account.enabled:
                raise ValueError("an enabled administrator account is required")
            if not 1 <= args.expires_days <= 365:
                raise ValueError("expires-days must be between 1 and 365")
            quota = AccountQuota(
                asr_month_seconds=args.asr_month_seconds,
                llm_month_cny=args.llm_month_cny,
                cache_bytes=args.cache_bytes,
                queue_items=args.queue_items,
                max_upload_bytes=args.max_upload_bytes,
            )
            base_url = _valid_base_url(args.base_url)
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            store.create_invite(account.id, token_hash, quota, time.time() + args.expires_days * 24 * 60 * 60)
            print(f"Invite link (expires in {args.expires_days} days): {base_url}/invite#{raw_token}")
            return 0
        if args.invite_command == "revoke":
            raw_token = getpass.getpass("Invite token to revoke: ")
            if not raw_token:
                raise ValueError("invite token cannot be empty")
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            store.revoke_invite(token_hash)
            print("Invitation revoked.")
            return 0

    if args.command == "user":
        account = _find_account(store, args.username)
        if args.user_command == "disable":
            if account.role == "admin":
                raise ValueError("the bootstrap administrator cannot be disabled")
            store.set_enabled(account.id, False)
            print(f"Account '{account.username}' disabled.")
            return 0
        if args.user_command == "enable":
            store.set_enabled(account.id, True)
            print(f"Account '{account.username}' enabled.")
            return 0
        if args.user_command == "reset-password":
            password = _read_password("New account password: ")
            store.replace_password_hash(account.id, auth.hash_password(password), must_change=True)
            print(f"Password reset for '{account.username}'; it must be changed at next login.")
            return 0

    raise ValueError("unsupported command")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (OSError, sqlite3.Error, ValueError, KeyError, InviteError, InvalidOperation) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
