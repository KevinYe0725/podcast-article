"""Create one isolated UI-test admin and a private browser session file."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

from podcast_article import auth
from podcast_article.platform_store import PlatformStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-file", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--queue-read", action="store_true")
    mode.add_argument("--queue-write", action="store_true")
    args = parser.parse_args()
    database = Path(os.environ["PA_PLATFORM_DB"])
    store = PlatformStore(database)
    if args.queue_read:
        print(json.dumps([_queue_item(job) for job in store.list_jobs(os.environ["PA_TEST_USER_ID"], limit=500)]))
        return
    if args.queue_write:
        fixtures = json.load(sys.stdin)
        owner_id = os.environ["PA_TEST_USER_ID"]
        with store._transaction() as db:
            db.execute("DELETE FROM jobs WHERE owner_id=?", (owner_id,))
        now = time.time()
        payloads = [{
            "url": item.get("url", ""), "pick": item.get("pick", 1),
            "title": item.get("title", ""), "source": item.get("source", "manual"),
            "opts": item.get("opts") or {},
        } for item in fixtures]
        jobs = store.enqueue_jobs(owner_id, payloads, now)
        for job, item in zip(jobs, fixtures):
            store.update_job(owner_id, job.id, {
                "title": item.get("title", ""), "pick": item.get("pick", 1),
                "source": item.get("source", "manual"), "opts": item.get("opts") or {},
            })
            if item.get("state") in {"done", "error", "skipped"}:
                store.finish_job(owner_id, job.id, item["state"],
                                 error=item.get("error", ""), dir_name=item.get("dir"))
        print(json.dumps([_queue_item(job) for job in store.list_jobs(owner_id, limit=500)]))
        return
    account = store.bootstrap_admin("ui-test-admin", auth.hash_password("test-only long passphrase"), time.time())
    session_token = auth.new_token()
    csrf_token = auth.new_token()
    now = time.time()
    store.create_session(
        account.id,
        auth.token_hash(session_token),
        now,
        now + 12 * 60 * 60,
        now + 7 * 24 * 60 * 60,
        csrf_token_hash=auth.token_hash(csrf_token),
    )
    args.session_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.session_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"user_id": account.id, "session_token": session_token, "csrf_token": csrf_token}, handle)
    os.chmod(args.session_file, stat.S_IRUSR | stat.S_IWUSR)
    print(account.id)


def _queue_item(job) -> dict:
    payload = job.payload
    return {
        "id": job.id, "url": payload.get("url") or "", "pick": payload.get("pick", 1),
        "title": payload.get("title") or "", "source": payload.get("source") or "manual",
        "state": job.state, "opts": payload.get("opts") or {}, "added_at": job.created_at,
        "started_at": job.started_at, "finished_at": job.finished_at,
        "dir": job.dir_name, "error": job.error,
    }


if __name__ == "__main__":
    main()
