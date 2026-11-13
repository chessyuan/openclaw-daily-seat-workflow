#!/usr/bin/env python3
import argparse
import base64
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path.home() / ".local/share/openclaw-android-workflows"
NODE = "my-android-phone"
BEIJING = ZoneInfo("Asia/Shanghai")


class RunFailure(RuntimeError):
    pass


def now_text():
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def run_json(argv, timeout=45):
    proc = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "command failed").strip()
        raise RunFailure(message.splitlines()[-1])
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RunFailure(f"invalid OpenClaw JSON: {exc}") from exc


def node_status():
    data = run_json(["openclaw", "nodes", "status", "--json"])
    node = next((item for item in data.get("nodes", []) if item.get("displayName") == NODE), None)
    if not node or not node.get("connected"):
        raise RunFailure("Android Node 离线")
    required = {"mobile.ui.observe", "mobile.ui.act"}
    if not required.issubset(set(node.get("commands", []))):
        raise RunFailure("Android Node 未提供 mobile UI 能力")


def invoke(command, params):
    data = run_json([
        "openclaw", "nodes", "invoke", "--node", NODE,
        "--command", command, "--params", json.dumps(params, ensure_ascii=False, separators=(",", ":")), "--json",
    ])
    if not data.get("ok"):
        raise RunFailure(f"{command} failed")
    return data.get("payload") or {}


def observe():
    return invoke("mobile.ui.observe", {})


def text_of(node):
    return " ".join(str(node.get(key) or "") for key in ("text", "contentDescription"))


def is_secure_lock(snapshot):
    if snapshot.get("package") != "com.android.systemui":
        return False
    text = " ".join(text_of(node) for node in snapshot.get("nodes", []))
    return any(token in text for token in ("PIN", "密码", "指纹", "解锁", "紧急呼叫", "Emergency call"))


def find_target(snapshot, target):
    nodes = snapshot.get("nodes", [])
    by_ref = {node.get("ref"): node for node in nodes}

    def matches(node):
        if target.get("viewId") and node.get("viewId") == target["viewId"]:
            return True
        if target.get("text") and node.get("text") == target["text"]:
            return True
        if target.get("contentDescription") and node.get("contentDescription") == target["contentDescription"]:
            return True
        if target.get("textContains") and target["textContains"] in text_of(node):
            return True
        return False

    candidate = next((node for node in nodes if matches(node)), None)
    if candidate is None:
        return None
    current = candidate
    while current:
        if "activate" in (current.get("actions") or []):
            return current
        current = by_ref.get(current.get("parentRef"))
    return candidate


def postcondition(snapshot, expected):
    if expected.get("package") and snapshot.get("package") != expected["package"]:
        return False
    if expected.get("windowTitle") and snapshot.get("windowTitle") != expected["windowTitle"]:
        return False
    nodes = snapshot.get("nodes", [])
    if expected.get("viewId") and not any(n.get("viewId") == expected["viewId"] for n in nodes):
        return False
    if expected.get("text") and not any(n.get("text") == expected["text"] for n in nodes):
        return False
    if expected.get("textContains") and not any(expected["textContains"] in text_of(n) for n in nodes):
        return False
    return True


def act_once(snapshot, action):
    kind = action.get("type")
    if kind == "global_action":
        payload = {"type": "global_action", "name": action.get("name")}
    elif kind == "wait":
        payload = {"type": "wait", "ms": int(action.get("ms", 1000))}
    elif kind in {"activate", "set_text", "scroll"}:
        target = find_target(snapshot, action.get("target") or {})
        if target is None:
            raise RunFailure("找不到语义目标")
        payload = {"type": kind, "ref": target.get("ref")}
        if kind == "set_text":
            payload["text"] = str(action.get("text", ""))
        if kind == "scroll":
            payload["direction"] = action.get("direction", "forward")
    elif kind == "swipe":
        direction = action.get("semanticDirection")
        scrollable = next((n for n in snapshot.get("nodes", []) if "scroll_forward" in (n.get("actions") or []) or "scroll_backward" in (n.get("actions") or [])), None)
        if scrollable is None:
            raise RunFailure("当前页面没有可滚动语义容器")
        payload = {"type": "scroll", "ref": scrollable.get("ref"), "direction": "forward" if direction in {"down", "forward"} else "backward"}
    else:
        raise RunFailure(f"不支持的操作类型：{kind}")
    return invoke("mobile.ui.act", {"snapshotId": snapshot["snapshotId"], "action": payload})


def execute_step(step, index):
    last_error = None
    for attempt in range(2):
        snapshot = observe()
        if is_secure_lock(snapshot):
            raise RunFailure("手机需要人工解锁。")
        try:
            act_once(snapshot, step["action"])
        except RunFailure as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.2)
                continue
            raise
        time.sleep(float(step.get("waitSeconds", 1.0)))
        after = observe()
        if postcondition(after, step.get("after") or {}):
            return after
        last_error = RunFailure("操作后成功条件未出现")
        if attempt == 0:
            time.sleep(1.2)
    raise RunFailure(f"第 {index} 步失败：{last_error}")


def write_run_log(name, detail):
    encoded = base64.b64encode(json.dumps(detail, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    subprocess.run([str(Path.home() / ".local/bin/android-workflow"), "run-log", "--name", name, "--json-base64", encoded], text=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(prog="android-workflow-run")
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    path = ROOT / "workflows" / f"{args.name}.json"
    started = now_text()
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
        if not workflow.get("confirmed"):
            raise RunFailure("workflow 尚未确认")
        node_status()
        steps = workflow.get("steps") or []
        if not steps:
            raise RunFailure("workflow 没有步骤")
        initial = observe()
        first_before = steps[0].get("before") or {}
        if first_before.get("package") == "com.sec.android.app.launcher" and initial.get("package") != "com.sec.android.app.launcher":
            invoke("mobile.ui.act", {"snapshotId": initial["snapshotId"], "action": {"type": "global_action", "name": "home"}})
            time.sleep(1.0)
        final = None
        for index, step in enumerate(steps, start=1):
            final = execute_step(step, index)
        ended = now_text()
        detail = {"status": "passed", "startedAt": started, "endedAt": ended, "steps": len(steps), "finalPackage": (final or {}).get("package")}
        write_run_log(args.name, detail)
        print("✅ Android 自动任务完成")
        print(f"任务：{args.name}")
        print(f"App：{workflow.get('app') or workflow.get('package')}")
        print(f"开始时间：{started}")
        print(f"完成时间：{ended}")
        print("结果：语义重放成功")
        return 0
    except Exception as exc:
        ended = now_text()
        detail = {"status": "failed", "startedAt": started, "endedAt": ended, "error": str(exc)}
        write_run_log(args.name, detail)
        print("❌ Android 自动任务失败")
        print(f"任务：{args.name}")
        print(f"原因：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
