#!/usr/bin/env python3
"""Visual Android workflow for the WeChat library-seat check-in."""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OPENCLAW = str(Path.home() / ".local/openclaw/bin/openclaw")
NODE = "my-android-phone"
WECHAT = "com.tencent.mm"
MODEL = "gemini-3.8-flash-high"
TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path.home() / ".local/share/openclaw-android-workflows"
WORKFLOW_PATH = ROOT / "workflows/checkin.json"
FAST_ACT = Path.home() / ".local/bin/openclaw-android-fast-act.mjs"
NODE_BIN = Path.home() / ".local/openclaw/tools/node-v24.15.0/bin/node"


class FlowError(RuntimeError):
    pass


def now_text():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def run_json(argv, timeout=55):
    proc = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise FlowError((proc.stderr or proc.stdout or "command failed").strip().splitlines()[-1])
    start = proc.stdout.find("{")
    if start < 0:
        raise FlowError("OpenClaw did not return JSON")
    return json.loads(proc.stdout[start:])


def invoke(command, params, timeout=55):
    data = run_json([
        OPENCLAW, "nodes", "invoke", "--node", NODE, "--command", command,
        "--params", json.dumps(params, ensure_ascii=False, separators=(",", ":")), "--json",
    ], timeout=timeout)
    if data.get("ok") is not True:
        raise FlowError(f"{command} failed")
    return data.get("payload") or {}


def ensure_node():
    data = run_json([OPENCLAW, "nodes", "status", "--json"], timeout=45)
    node = next((n for n in data.get("nodes", []) if n.get("displayName") == NODE), None)
    if not node or not node.get("connected"):
        raise FlowError("Android Node 离线")
    required = {"mobile.ui.observe", "mobile.ui.act", "mobile.ui.launch", "mobile.ui.screenshot"}
    if not required.issubset(set(node.get("commands", []))):
        raise FlowError("Android Node 缺少 UI/截图能力")


def observe():
    return invoke("mobile.ui.observe", {})


def act(snapshot, action):
    result = invoke("mobile.ui.act", {"snapshotId": snapshot["snapshotId"], "action": action})
    if result.get("code") not in (None, "completed"):
        raise FlowError(f"Android action failed: {result.get('code')} {result.get('message') or ''}".strip())
    return result


def fast_coordinate(*args):
    proc = subprocess.run(
        [str(NODE_BIN), str(FAST_ACT), *map(str, args)],
        text=True,
        capture_output=True,
        timeout=45,
    )
    if proc.returncode != 0:
        raise FlowError((proc.stderr or proc.stdout or "fast coordinate action failed").strip().splitlines()[-1])
    result = json.loads(proc.stdout)
    if result.get("ok") is not True:
        raise FlowError(f"coordinate action failed: {result.get('result')}")
    return result


def screenshot():
    payload = invoke("mobile.ui.screenshot", {"maxDimension": 2048, "quality": 88})
    try:
        raw = base64.b64decode(payload["base64"], validate=True)
    except Exception as exc:
        raise FlowError("Android screenshot data is invalid") from exc
    return raw, payload.get("mimeType") or "image/jpeg", int(payload["width"]), int(payload["height"])


def bounds_max(snapshot):
    width = height = 0
    for node in snapshot.get("nodes", []):
        bounds = node.get("bounds")
        if isinstance(bounds, list) and len(bounds) == 4:
            width = max(width, int(bounds[2]))
            height = max(height, int(bounds[3]))
        elif isinstance(bounds, dict):
            width = max(width, int(bounds.get("right", 0)))
            height = max(height, int(bounds.get("bottom", 0)))
    return width, height


def prepare_display():
    # A time-sensitive OpenClaw notification is the non-USB wake trigger. On
    # Samsung, the OpenClaw notification channel must be allowed to show while
    # the screen is off. The following upward gesture dismisses the resulting
    # non-secure keyguard; a real PIN/biometric lock is detected later and is
    # never bypassed.
    try:
        fast_coordinate("notify")
        time.sleep(1.8)
        fast_coordinate(
            "swipe",
            720, 2700,
            720, 700,
            650,
        )
        time.sleep(1.0)
    except Exception:
        # Continue when the display is already on. If it is still off, the
        # screenshot/lock checks below produce an explicit bounded failure.
        pass
    current = observe()
    act(current, {"type": "global_action", "name": "home"})
    time.sleep(1.0)
    home = observe()
    width, height = bounds_max(home)
    if width < 720 or height < 1200:
        raise FlowError("无法读取当前手机触控分辨率")
    invoke("mobile.ui.launch", {"package": WECHAT})
    time.sleep(2.5)
    return width, height


def provider():
    config = json.loads((Path.home() / ".openclaw/openclaw.json").read_text(encoding="utf-8"))
    item = config["models"]["providers"]["antigravity-gemini"]
    key = (Path.home() / ".config/cliproxyapi/openclaw-api-key").read_text(encoding="utf-8").strip()
    return item["baseUrl"].rstrip("/"), key


def parse_model_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
    return json.loads(text.strip())


def vision(raw, mime, instruction):
    base_url, key = provider()
    prompt = (
        "你是Android手机视觉定位器。仅根据本次真实截图像素判断，不使用历史坐标。"
        "截图覆盖完整手机屏幕。坐标必须用相对整张图的0到1000归一化整数。"
        "若目标不清楚或置信度不足，target_found必须为false，禁止猜测。"
        "严格输出JSON：page, target_found, x_norm, y_norm, confidence, evidence, "
        "secure_lock, loading, success。" + instruction
    )
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        f"{base_url}/models/{MODEL}:generateContent",
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST",
    )
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                data = json.load(response)
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return parse_model_json(text)
        except Exception as exc:
            last = exc
            if attempt == 0:
                time.sleep(1.5)
    raise FlowError(f"视觉模型请求失败：{type(last).__name__}")


def inspect(instruction, artifact_dir, label):
    raw, mime, image_w, image_h = screenshot()
    (artifact_dir / f"{label}.jpg").write_bytes(raw)
    result = vision(raw, mime, instruction)
    safe = {k: v for k, v in result.items() if k not in {"token", "key"}}
    (artifact_dir / f"{label}.json").write_text(
        json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if result.get("secure_lock"):
        raise FlowError("手机需要人工解锁。")
    return result, image_w, image_h


def tap_visual(result, image_w, image_h, screen_w, screen_h):
    if not result.get("target_found") or float(result.get("confidence") or 0) < 0.70:
        raise FlowError(f"视觉目标未可靠找到：{result.get('evidence') or 'unknown'}")
    x_norm = max(0, min(1000, int(result["x_norm"])))
    y_norm = max(0, min(1000, int(result["y_norm"])))
    # Normalized coordinates avoid coupling the workflow to screenshot downscaling.
    x = round(x_norm * screen_w / 1000)
    y = round(y_norm * screen_h / 1000)
    fast_coordinate("tap", x, y)
    return {"xNorm": x_norm, "yNorm": y_norm, "screen": [screen_w, screen_h], "image": [image_w, image_h]}


def wait_target(instruction, artifact_dir, label, screen_w, screen_h, checks=6):
    last = None
    for index in range(1, checks + 1):
        result, image_w, image_h = inspect(instruction, artifact_dir, f"{label}-{index}")
        last = result
        if result.get("target_found") and float(result.get("confidence") or 0) >= 0.70:
            return result, image_w, image_h
        time.sleep(1.5 if result.get("loading") else 1.0)
    raise FlowError(f"找不到目标：{(last or {}).get('evidence') or label}")


def clear_recent_apps(artifact_dir, screen_w, screen_h):
    """Clear Samsung Recents after the workflow has succeeded."""
    current = observe()
    act(current, {"type": "global_action", "name": "recents"})
    time.sleep(1.5)
    prompt = (
        "当前是三星 One UI 最近任务页面。寻找底部的‘全部关闭’、‘关闭全部’或"
        "‘清除全部’按钮。若存在应用卡片且找到该按钮，target_found=true并返回按钮中心；"
        "若最近任务本来就是空的，success=true且target_found=false；若页面尚未加载完成，"
        "loading=true。不要点击应用卡片、分屏、图标或其他按钮。"
    )
    last = None
    for index in range(1, 5):
        result, image_w, image_h = inspect(prompt, artifact_dir, f"recents-{index}")
        last = result
        if result.get("target_found") and float(result.get("confidence") or 0) >= 0.70:
            tap_visual(result, image_w, image_h, screen_w, screen_h)
            time.sleep(1.8)
            return {"cleared": True, "evidence": result.get("evidence")}
        if result.get("success") and not result.get("target_found"):
            current = observe()
            act(current, {"type": "global_action", "name": "home"})
            return {"cleared": True, "alreadyEmpty": True, "evidence": result.get("evidence")}
        time.sleep(1.0)
    raise FlowError(f"签到已完成，但最近任务清理失败：{(last or {}).get('evidence') or '未找到全部关闭'}")


def run_once(run_dir):
    screen_w, screen_h = prepare_display()
    conversation_prompt = (
        "当前目标：在微信主界面会话列表中找到名称完全为‘我去图书馆’的会话行，"
        "给出该会话整行中心的可点击位置。若当前不在会话列表，target_found=false。"
    )
    # WeChat may resume a previous page. Walk back a bounded number of times.
    conversation = None
    for back_count in range(5):
        result, iw, ih = inspect(conversation_prompt, run_dir, f"conversation-{back_count}")
        if result.get("target_found") and float(result.get("confidence") or 0) >= 0.70:
            conversation = (result, iw, ih)
            break
        if back_count < 4:
            snap = observe()
            act(snap, {"type": "global_action", "name": "back"})
            time.sleep(1.2)
    if conversation is None:
        raise FlowError("微信会话列表中未找到‘我去图书馆’")
    tap_visual(*conversation, screen_w, screen_h)
    time.sleep(3.0)

    seat_prompt = (
        "当前目标：在‘我去图书馆’页面底部菜单中找到文字为‘座位’的按钮，"
        "给出‘座位’按钮中心。不要点击‘考研商城’或‘更多’。"
    )
    seat = wait_target(seat_prompt, run_dir, "seat", screen_w, screen_h, checks=7)
    tap_visual(*seat, screen_w, screen_h)
    time.sleep(4.0)

    # The target card sits partly below the fold on this page. Scroll by a
    # relative amount so the workflow remains independent of fixed UI element
    # coordinates and the card's own "选座 >" control becomes visible.
    fast_coordinate(
        "swipe",
        round(screen_w * 0.50), round(screen_h * 0.78),
        round(screen_w * 0.50), round(screen_h * 0.25),
        650,
    )
    time.sleep(1.5)

    favorite_prompt = (
        "当前目标：在‘常用座位’标题下，先锁定标题为‘博学楼四楼半圆弧平7号’的卡片，"
        "然后只定位该卡片内部左下方文字‘选座 >’的中心。必须点击‘选座 >’文字，"
        "不要点击整张卡片中心、右侧点赞图标、其他座位卡片、返回键或底部导航。"
    )
    favorite = wait_target(favorite_prompt, run_dir, "favorite", screen_w, screen_h, checks=8)
    tap_visual(*favorite, screen_w, screen_h)
    time.sleep(4.0)

    verify_prompt = (
        "验证刚才点击常用座位卡片后的结果。若出现选座/预约后的新页面、成功提示、"
        "已选座状态、明确的下一层座位详情，或者跳转到新闻/推广/广告文章页，均说明"
        "‘选座 >’点击已经被接受，success=true；若仍停留原常用座位卡片页、出现错误/"
        "登录/权限提示，则success=false。此步骤不需要点击，target_found=false。"
    )
    verify, _, _ = inspect(verify_prompt, run_dir, "verify")
    if not verify.get("success"):
        raise FlowError(f"最终成功状态未确认：{verify.get('evidence') or verify.get('page')}")
    # The site may open a promotional article after accepting the selection.
    # Exit it with Android Back; this does not undo the accepted action.
    if any(token in str(verify.get("page") or "") for token in ("新闻", "广告", "推广", "文章")):
        current = observe()
        act(current, {"type": "global_action", "name": "back"})
        time.sleep(1.0)
    cleanup = clear_recent_apps(run_dir, screen_w, screen_h)
    return {"screen": [screen_w, screen_h], "result": verify, "cleanup": cleanup}


def write_run(detail):
    run_dir = ROOT / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"checkin-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(detail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    return path


def main():
    parser = argparse.ArgumentParser(prog="android-signin")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()
    attempts = max(1, min(3, args.max_attempts))
    started = now_text()
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    base = ROOT / "artifacts" / f"checkin-{stamp}"
    base.mkdir(parents=True, exist_ok=False)
    errors = []
    try:
        ensure_node()
        for attempt in range(1, attempts + 1):
            attempt_dir = base / f"attempt-{attempt}"
            attempt_dir.mkdir()
            try:
                result = run_once(attempt_dir)
                ended = now_text()
                detail = {"status": "passed", "attempt": attempt, "startedAt": started,
                          "endedAt": ended, "artifacts": str(base), **result}
                write_run(detail)
                print("✅ 手机自动签到完成")
                print("任务：签到")
                print("App：微信 / 我去图书馆")
                print(f"时间：{ended}")
                print(f"结果：第 {attempt} 次尝试已由截图确认成功")
                return 0
            except Exception as exc:
                errors.append(f"第{attempt}次：{exc}")
                if "人工解锁" in str(exc):
                    break
                if attempt < attempts:
                    time.sleep(2.0)
        raise FlowError("；".join(errors))
    except Exception as exc:
        ended = now_text()
        detail = {"status": "failed", "startedAt": started, "endedAt": ended,
                  "error": str(exc), "artifacts": str(base)}
        write_run(detail)
        print("❌ 手机自动签到失败")
        print("任务：签到")
        print(f"时间：{ended}")
        print(f"原因：{exc}")
        print(f"最后截图：{base}")
        return 1


if __name__ == "__main__":
    raise SystemExit("Historical source archive; use offline tests.")
