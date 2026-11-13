#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("OPENCLAW_ANDROID_WORKFLOW_ROOT", Path.home() / ".local/share/openclaw-android-workflows"))
CURRENT = ROOT / "teaching" / "current.json"
WORKFLOWS = ROOT / "workflows"
TRIALS = ROOT / "trials"
RUNS = ROOT / "runs"


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(value, code=0):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(code)


def fail(message):
    emit({"ok": False, "error": message}, 1)


def init_dirs():
    for path in (CURRENT.parent, WORKFLOWS, TRIALS, RUNS):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)


def safe_name(value):
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        fail("invalid workflow name; use 1-64 ASCII letters, digits, dot, underscore, or hyphen")
    return value


def decode_json(value):
    try:
        return json.loads(base64.b64decode(value, validate=True).decode("utf-8"))
    except Exception:
        fail("invalid base64 JSON")


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"not found: {path.name}")


def atomic_write(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_step(step):
    if not isinstance(step, dict):
        fail("step must be an object")
    required = ("intent", "before", "action", "after")
    if any(key not in step for key in required):
        fail("step requires intent, before, action, and after")
    action = step.get("action")
    if not isinstance(action, dict) or action.get("type") not in {
        "activate", "set_text", "scroll", "global_action", "wait", "swipe"
    }:
        fail("unsupported workflow action")
    if action.get("type") == "activate":
        target = action.get("target")
        if not isinstance(target, dict) or not any(target.get(k) for k in ("viewId", "text", "textContains", "contentDescription")):
            fail("activate requires a semantic target")
    if action.get("type") == "swipe" and not action.get("semanticDirection"):
        fail("swipe requires semanticDirection")
    if action.get("type") == "set_text" and action.get("sensitive"):
        fail("credentials, PINs, passwords, and other sensitive text cannot be saved")


def main():
    parser = argparse.ArgumentParser(prog="android-workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("begin")
    begin.add_argument("--app", required=True)
    begin.add_argument("--package")
    record = sub.add_parser("record")
    record.add_argument("--json-base64", required=True)
    sub.add_parser("current")
    save = sub.add_parser("save")
    save.add_argument("--name", required=True)
    show = sub.add_parser("show")
    show.add_argument("--name", required=True)
    sub.add_parser("list")
    trial = sub.add_parser("trial")
    trial.add_argument("--name", required=True)
    trial.add_argument("--status", choices=("passed", "failed"), required=True)
    trial.add_argument("--detail-base64")
    confirm = sub.add_parser("confirm")
    confirm.add_argument("--name", required=True)
    delete = sub.add_parser("delete")
    delete.add_argument("--name", required=True)
    runlog = sub.add_parser("run-log")
    runlog.add_argument("--name", required=True)
    runlog.add_argument("--json-base64", required=True)
    args = parser.parse_args()
    init_dirs()

    if args.command == "begin":
        value = {"schemaVersion": 1, "status": "teaching", "app": args.app, "package": args.package, "startedAt": now(), "steps": []}
        atomic_write(CURRENT, value)
        emit({"ok": True, "status": "teaching", "app": args.app})

    if args.command == "record":
        value = load(CURRENT)
        step = decode_json(args.json_base64)
        validate_step(step)
        step["recordedAt"] = now()
        value["steps"].append(step)
        value["updatedAt"] = now()
        atomic_write(CURRENT, value)
        emit({"ok": True, "recorded": len(value["steps"]), "intent": step["intent"]})

    if args.command == "current":
        emit({"ok": True, "workflow": load(CURRENT)})

    if args.command == "save":
        name = safe_name(args.name)
        value = load(CURRENT)
        if not value.get("steps"):
            fail("cannot save an empty teaching flow")
        value.update({"name": name, "status": "draft", "savedAt": now(), "confirmed": False})
        path = WORKFLOWS / f"{name}.json"
        atomic_write(path, value)
        receipt = TRIALS / f"{name}.json"
        if receipt.exists():
            receipt.unlink()
        emit({"ok": True, "name": name, "status": "draft", "steps": len(value["steps"]), "path": str(path)})

    if args.command == "show":
        name = safe_name(args.name)
        emit({"ok": True, "workflow": load(WORKFLOWS / f"{name}.json")})

    if args.command == "list":
        items = []
        for path in sorted(WORKFLOWS.glob("*.json")):
            value = load(path)
            items.append({"name": value.get("name", path.stem), "app": value.get("app"), "status": value.get("status"), "steps": len(value.get("steps", [])), "confirmed": bool(value.get("confirmed"))})
        emit({"ok": True, "workflows": items})

    if args.command == "trial":
        name = safe_name(args.name)
        path = WORKFLOWS / f"{name}.json"
        workflow = load(path)
        detail = decode_json(args.detail_base64) if args.detail_base64 else {}
        receipt = {"name": name, "status": args.status, "workflowSha256": digest(path), "recordedAt": now(), "detail": detail}
        atomic_write(TRIALS / f"{name}.json", receipt)
        emit({"ok": True, **receipt})

    if args.command == "confirm":
        name = safe_name(args.name)
        path = WORKFLOWS / f"{name}.json"
        value = load(path)
        receipt = load(TRIALS / f"{name}.json")
        if receipt.get("status") != "passed" or receipt.get("workflowSha256") != digest(path):
            fail("the current workflow revision needs a successful full trial")
        value.update({"status": "confirmed", "confirmed": True, "confirmedAt": now()})
        atomic_write(path, value)
        emit({"ok": True, "name": name, "status": "confirmed", "trialAt": receipt.get("recordedAt")})

    if args.command == "delete":
        name = safe_name(args.name)
        path = WORKFLOWS / f"{name}.json"
        if not path.exists():
            fail("workflow not found")
        archive = ROOT / "archive"
        archive.mkdir(mode=0o700, exist_ok=True)
        target = archive / f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        shutil.move(path, target)
        trial_path = TRIALS / f"{name}.json"
        if trial_path.exists():
            shutil.move(trial_path, archive / f"{name}-trial-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        emit({"ok": True, "name": name, "archived": str(target)})

    if args.command == "run-log":
        name = safe_name(args.name)
        detail = decode_json(args.json_base64)
        detail.update({"name": name, "recordedAt": now()})
        path = RUNS / f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        atomic_write(path, detail)
        emit({"ok": True, "path": str(path)})


if __name__ == "__main__":
    main()
