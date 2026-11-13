#!/usr/bin/env python3
"""Visual Android workflow for the WeChat library-seat check-in."""

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
import math
import urllib.error
import urllib.parse
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
WEGOLIBRARY_BASE = "https://attendance.invalid"
WEGOLIBRARY_CREDENTIALS = Path.home() / ".config/wegolibrary-checkin/credentials.json"


class FlowError(RuntimeError):
    pass


class WindowClosed(FlowError):
    pass


class WegoLibraryError(FlowError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def now_text():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_until(value):
    try:
        hour, minute = (int(part) for part in value.split(":"))
        now = datetime.now(TZ)
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("--until must use HH:MM") from exc


def ensure_window_open(deadline):
    if deadline is not None and datetime.now(TZ) >= deadline:
        raise WindowClosed(f"执行窗口已于 {deadline.strftime('%H:%M')} 结束")


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
    except Exception:
        # Continue when the display is already on. The launch RPC performs the
        # local Samsung protection/keyguard handling and returns a precise error.
        pass
    last_error = None
    for launch_attempt in range(2):
        try:
            invoke("mobile.ui.launch", {"package": WECHAT}, timeout=70)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if launch_attempt == 0:
                time.sleep(1.2)
    if last_error is not None:
        raise FlowError(f"手机唤醒/本地解锁/启动微信失败：{last_error}")
    time.sleep(2.5)
    current = observe()
    width, height = bounds_max(current)
    if width < 720 or height < 1200:
        # Accessibility trees are intentionally empty inside WeChat. Measure
        # Android's actual gesture coordinate space on the launcher, then
        # return to WeChat. Screenshot dimensions are capped at 2048 and must
        # never be used as gesture dimensions on this 1440x3120 device.
        act(current, {"type": "global_action", "name": "home"})
        time.sleep(1.0)
        launcher = observe()
        width, height = bounds_max(launcher)
        invoke("mobile.ui.launch", {"package": WECHAT})
        time.sleep(2.0)
    if width < 720 or height < 1200:
        width, height = 1440, 3120
    if width < 720 or height < 1200:
        raise FlowError("无法读取当前手机触控分辨率")
    return width, height


def provider():
    config = json.loads((Path.home() / ".openclaw/openclaw.json").read_text(encoding="utf-8"))
    item = config["models"]["providers"]["antigravity-gemini"]
    key = (Path.home() / ".config/cliproxyapi/openclaw-api-key").read_text(encoding="utf-8").strip()
    return item["baseUrl"].rstrip("/"), key


def _wegolibrary_request(path, token=None, data=None, form=False, timeout=25):
    raise RuntimeError("Attendance transport unavailable in archive")


def complete_wegolibrary_checkin():
    """Run the exact backend action used by the site's orange button."""
    try:
        credentials = json.loads(WEGOLIBRARY_CREDENTIALS.read_text(encoding="utf-8"))
        username = str(credentials["username"])
        password = str(credentials["password"])
    except Exception as exc:
        raise WegoLibraryError("WegoLibrary本地登录凭据不可用") from exc

    login = _wegolibrary_request(
        "/api/auth/login",
        data={"username": username, "password": password},
        form=True,
    )
    token = str(login.get("access_token") or "")
    if not token:
        raise WegoLibraryError("WegoLibrary登录未返回有效会话")

    status = _wegolibrary_request("/api/status", token=token)
    if not status.get("is_configured"):
        raise WegoLibraryError("WegoLibrary尚未配置微信连接")
    if status.get("wechat_connection_status") != "connected":
        raise WegoLibraryError("WegoLibrary微信连接当前不可用")

    # A retry after a successful first pass must be idempotent.
    previous_time = str(status.get("last_checkin") or "")
    previous_result = str(status.get("last_checkin_result") or "")
    if previous_time.startswith(datetime.now(TZ).strftime("%Y-%m-%d")) and "签到成功" in previous_result:
        return {"success": True, "message": previous_result, "idempotent": True}

    result = _wegolibrary_request("/api/checkin", token=token, data={})
    if not result.get("success"):
        raise WegoLibraryError(str(result.get("message") or "WegoLibrary签到未成功"))

    verified = _wegolibrary_request("/api/status", token=token)
    verified_result = str(verified.get("last_checkin_result") or "")
    if "签到成功" not in verified_result:
        raise WegoLibraryError("WegoLibrary返回成功，但状态页未确认成功")
    return {
        "success": True,
        "message": verified_result,
        "lastCheckin": verified.get("last_checkin"),
    }


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


def diagnose_failures(artifact_root, errors, label):
    raise RuntimeError("Attendance transport unavailable in archive")


def notify_wechat(message, channel, account, target):
    if not (channel and account and target):
        return {"sent": False, "reason": "notification destination not configured"}
    proc = subprocess.run(
        [OPENCLAW, "message", "send", "--json", "--channel", channel,
         "--account", account, "--target", target, "--message", message],
        text=True, capture_output=True, timeout=45,
    )
    if proc.returncode != 0:
        return {"sent": False, "reason": "message send failed"}
    return {"sent": True}


def merge_visual_votes(first, second):
    """Require two independent visual passes to agree before a coordinate click."""
    first_found = bool(first.get("target_found"))
    second_found = bool(second.get("target_found"))
    if not (first_found and second_found):
        merged = dict(first)
        merged["target_found"] = False
        merged["confidence"] = min(
            float(first.get("confidence") or 0), float(second.get("confidence") or 0)
        )
        merged["evidence"] = (
            f"双重视觉未达成一致；首轮：{first.get('evidence')}; "
            f"复核：{second.get('evidence')}"
        )
        return merged
    try:
        x1, y1 = int(first["x_norm"]), int(first["y_norm"])
        x2, y2 = int(second["x_norm"]), int(second["y_norm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FlowError("视觉模型返回了无效坐标") from exc
    distance = math.hypot(x1 - x2, y1 - y2)
    merged = dict(first)
    if distance > 120:
        merged["target_found"] = False
        merged["confidence"] = 0
        merged["evidence"] = (
            f"双重视觉坐标分歧过大({distance:.0f}/1000)；"
            f"首轮：{first.get('evidence')}; 复核：{second.get('evidence')}"
        )
        return merged
    merged["x_norm"] = round((x1 + x2) / 2)
    merged["y_norm"] = round((y1 + y2) / 2)
    merged["confidence"] = min(
        float(first.get("confidence") or 0), float(second.get("confidence") or 0)
    )
    merged["evidence"] = (
        f"双重视觉一致(distance={distance:.0f}/1000)；"
        f"首轮：{first.get('evidence')}; 复核：{second.get('evidence')}"
    )
    return merged


def inspect(instruction, artifact_dir, label, consensus=False):
    raw, mime, image_w, image_h = screenshot()
    (artifact_dir / f"{label}.jpg").write_bytes(raw)
    result = vision(raw, mime, instruction)
    if consensus:
        review = vision(
            raw,
            mime,
            "这是独立复核，不参考另一模型的坐标。重新从完整截图定位目标。" + instruction,
        )
        (artifact_dir / f"{label}-review.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        result = merge_visual_votes(result, review)
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


def school_binding_popup_visible(result):
    """Return true only for affirmative evidence that the known modal is visible."""
    page = str(result.get("page") or "")
    evidence = str(result.get("evidence") or "")
    if "绑定账户" in page:
        return True
    if "您的学校需要您绑定账户" not in evidence:
        return False
    # Vision explanations for an unrelated screen may quote the requested
    # string while explicitly saying that it is absent.
    absent_markers = ("不存在", "未出现", "未显示", "未检测到", "不是这个弹窗")
    return not any(marker in evidence for marker in absent_markers)


def dismiss_school_binding_popup(artifact_dir, label, screen_w, screen_h, deadline=None):
    """Dismiss the known transient school-binding prompt and wait for refresh.

    The traceint page sometimes shows this modal immediately after opening the
    seat page even though the account is already usable.  It is transient: the
    safe recovery is to press its sole blue Confirm button and wait for the
    page to refresh.  Never guess a coordinate or dismiss any other dialog.
    """
    prompt = (
        "只检查当前屏幕是否存在这个精确弹窗：标题‘提示’，正文‘您的学校需要您绑定账户’，"
        "底部只有一个蓝色‘确定’按钮。若完全匹配，target_found=true，并返回蓝色‘确定’按钮"
        "中心坐标；若不是这个弹窗或文字不完全匹配，target_found=false，禁止猜测或点击其他按钮。"
        "此处success仅表示弹窗不存在。"
    )
    handled = False
    for index in range(1, 4):
        ensure_window_open(deadline)
        result, image_w, image_h = inspect(
            prompt, artifact_dir, f"{label}-{index}", consensus=True
        )
        if result.get("target_found") and float(result.get("confidence") or 0) >= 0.80:
            tap_visual(result, image_w, image_h, screen_w, screen_h)
            handled = True
            # The page refreshes asynchronously after confirmation.
            time.sleep(5.0)
            continue
        if result.get("success") is True:
            return {"handled": handled, "evidence": result.get("evidence")}
        if school_binding_popup_visible(result):
            raise FlowError("识别到学校绑定提示，但未能可靠定位‘确定’按钮")
        return {"handled": handled, "evidence": result.get("evidence")}
    raise FlowError("学校绑定提示点击后未在有限重试内消失")


def wait_target(
    instruction, artifact_dir, label, screen_w, screen_h, checks=6,
    accept_success_without_target=False, deadline=None,
):
    last = None
    for index in range(1, checks + 1):
        ensure_window_open(deadline)
        result, image_w, image_h = inspect(
            instruction, artifact_dir, f"{label}-{index}", consensus=True
        )
        last = result
        if school_binding_popup_visible(result):
            dismiss_school_binding_popup(
                artifact_dir, f"{label}-binding-popup-{index}",
                screen_w, screen_h, deadline=deadline,
            )
            continue
        if result.get("target_found") and float(result.get("confidence") or 0) >= 0.70:
            return result, image_w, image_h
        if accept_success_without_target and result.get("success"):
            return result, image_w, image_h
        time.sleep(1.5 if result.get("loading") else 1.0)
    raise FlowError(f"找不到目标：{(last or {}).get('evidence') or label}")


def clear_recent_apps(artifact_dir, screen_w, screen_h, label_prefix="recents"):
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
        result, image_w, image_h = inspect(
            prompt, artifact_dir, f"{label_prefix}-{index}", consensus=True
        )
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


def turn_screen_off(artifact_dir, screen_w, screen_h):
    """Use Samsung's enabled 'double tap to turn off screen' launcher gesture."""
    # Use the low-latency connection so Home is delivered even when a browser
    # custom tab is in front. If Android has already timed out to keyguard,
    # there is nothing more to do.
    fast_coordinate("global", "home")
    time.sleep(2.0)
    current = observe()
    package = str(current.get("package") or "")
    view_ids = {
        str(node.get("viewId") or "") for node in current.get("nodes", [])
    }
    if package == "com.android.systemui" and any(
        "keyguard" in view_id.lower() or "lockpattern" in view_id.lower()
        for view_id in view_ids
    ):
        return {"requested": True, "method": "already_keyguard"}
    prompt = (
        "当前是三星 One UI 主屏幕。寻找一块足够大的纯壁纸空白区域，不能位于应用图标、"
        "文件夹、Widget、Dock、状态栏或导航栏上。返回适合执行‘双击关闭屏幕’的安全中心点。"
        "找到则target_found=true；没有安全空白区域则target_found=false。不要选择任何可点击控件。"
    )
    result, image_w, image_h = inspect(
        prompt, artifact_dir, "screen-off", consensus=True
    )
    if not result.get("target_found") or float(result.get("confidence") or 0) < 0.75:
        raise FlowError("无法可靠找到主屏幕空白区域，未执行息屏双击")
    x_norm = max(0, min(1000, int(result["x_norm"])))
    y_norm = max(0, min(1000, int(result["y_norm"])))
    x = round(x_norm * screen_w / 1000)
    y = round(y_norm * screen_h / 1000)
    fast_coordinate("doubletap", x, y)
    return {"requested": True, "method": "samsung_launcher_double_tap"}


def best_effort_finish(artifact_dir, screen_w=None, screen_h=None, clear_apps=True, power_off=True):
    result = {}
    try:
        if not screen_w or not screen_h:
            snap = observe()
            screen_w, screen_h = bounds_max(snap)
        if clear_apps and screen_w >= 720 and screen_h >= 1200:
            result["cleanup"] = clear_recent_apps(
                artifact_dir, screen_w, screen_h, label_prefix="final-recents"
            )
    except Exception as exc:
        result["cleanupError"] = str(exc)
    try:
        if power_off and screen_w and screen_h:
            result["screenOff"] = turn_screen_off(artifact_dir, screen_w, screen_h)
    except Exception as exc:
        result["screenOffError"] = str(exc)
    return result


def run_once(run_dir, deadline=None):
    ensure_window_open(deadline)
    screen_w, screen_h = prepare_display()
    conversation_prompt = (
        "当前目标：在微信主界面会话列表中找到名称完全为‘我去图书馆’的会话行，"
        "给出该会话整行中心的可点击位置。若当前不在会话列表，target_found=false。"
    )
    # WeChat may resume a previous page or the target conversation may be below
    # the current viewport. Navigate by observed page type instead of blindly
    # pressing Back (which would exit WeChat from the conversation list).
    conversation = None
    restarted_wechat = False
    for navigation_count in range(10):
        ensure_window_open(deadline)
        result, iw, ih = inspect(
            conversation_prompt, run_dir, f"conversation-{navigation_count}", consensus=True
        )
        if result.get("target_found") and float(result.get("confidence") or 0) >= 0.70:
            conversation = (result, iw, ih)
            break
        page = str(result.get("page") or "")
        if "会话列表" in page:
            if navigation_count < 3 or restarted_wechat:
                # The target is expected among the first few conversations.
                # Pull the list toward its top instead of browsing older rows.
                fast_coordinate(
                    "swipe",
                    round(screen_w * 0.50), round(screen_h * 0.30),
                    round(screen_w * 0.50), round(screen_h * 0.82),
                    520,
                )
                time.sleep(1.0)
            else:
                # WeChat can preserve a stale list offset across launches. A
                # bounded clean restart resets it without touching app data.
                clear_recent_apps(
                    run_dir, screen_w, screen_h, label_prefix="startup-recents"
                )
                invoke("mobile.ui.launch", {"package": WECHAT})
                restarted_wechat = True
                time.sleep(2.5)
        elif any(token in page for token in ("桌面", "主屏幕", "home_screen")):
            invoke("mobile.ui.launch", {"package": WECHAT})
            time.sleep(2.0)
        else:
            snap = observe()
            act(snap, {"type": "global_action", "name": "back"})
            time.sleep(1.2)
    if conversation is None:
        raise FlowError("微信会话列表中未找到‘我去图书馆’")
    seat_prompt = (
        "当前目标：在‘我去图书馆’页面底部菜单中找到文字为‘座位’的按钮，"
        "给出‘座位’按钮中心。不要点击‘考研商城’或‘更多’。"
    )
    seat = None
    for conversation_click_attempt in range(2):
        tap_visual(*conversation, screen_w, screen_h)
        time.sleep(3.0)
        ensure_window_open(deadline)
        try:
            seat = wait_target(
                seat_prompt, run_dir, f"seat-{conversation_click_attempt + 1}",
                screen_w, screen_h, checks=7, deadline=deadline
            )
            break
        except FlowError:
            if conversation_click_attempt > 0:
                raise
            # A visually close row can still be selected if display scaling
            # changed. Return to the list, capture again, and re-localize once.
            snap = observe()
            act(snap, {"type": "global_action", "name": "back"})
            time.sleep(1.5)
            relocated, iw, ih = inspect(
                conversation_prompt, run_dir, "conversation-relocate", consensus=True
            )
            if not relocated.get("target_found"):
                raise
            conversation = (relocated, iw, ih)
    if seat is None:
        raise FlowError("进入‘我去图书馆’后未找到座位按钮")
    tap_visual(*seat, screen_w, screen_h)
    time.sleep(4.0)
    ensure_window_open(deadline)

    # A known transient traceint modal can cover the seat page. Let the vision
    # model locate the exact Confirm button, click it, then wait for auto-refresh.
    dismiss_school_binding_popup(
        run_dir, "post-seat-binding-popup", screen_w, screen_h, deadline=deadline
    )

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
        "只有页面明确显示‘预约成功’、‘我的预约’、‘本人已选座’或其他能证明当前账号"
        "已经预约成功的状态，才允许success=true且target_found=false。"
        "‘已被预订’表示该座位已被占用或当前不可选，绝不代表当前账号预约成功；"
        "遇到它必须success=false且target_found=false，交由外层从完整流程重试。"
    )
    favorite = wait_target(
        favorite_prompt, run_dir, "favorite", screen_w, screen_h, checks=8,
        accept_success_without_target=True, deadline=deadline,
    )
    if favorite[0].get("success") and not favorite[0].get("target_found"):
        cleanup = clear_recent_apps(run_dir, screen_w, screen_h)
        ensure_window_open(deadline)
        wegolibrary = complete_wegolibrary_checkin()
        return {
            "screen": [screen_w, screen_h],
            "result": {
                **favorite[0],
                "success": True,
                "idempotent": True,
                "page": favorite[0].get("page") or "常用座位",
            },
            "cleanup": cleanup,
            "wegolibrary": wegolibrary,
        }
    verify_prompt = (
        "验证刚才点击常用座位卡片后的结果。若出现选座/预约后的新页面、成功提示、"
        "已选座状态、明确的下一层座位详情，或者跳转到新闻/推广/广告文章页，均说明"
        "‘选座 >’点击已经被接受，success=true；若仍停留原常用座位卡片页、出现错误/"
        "登录/权限提示，则success=false。此步骤不需要点击，target_found=false。"
    )
    verify = None
    for favorite_click_attempt in range(2):
        tap_visual(*favorite, screen_w, screen_h)
        time.sleep(4.0)
        ensure_window_open(deadline)
        verify, _, _ = inspect(
            verify_prompt, run_dir, f"verify-{favorite_click_attempt + 1}"
        )
        if verify.get("success"):
            break
        if favorite_click_attempt == 0:
            retry_prompt = (
                "上一次点击后页面未变化。仍在‘常用座位’区域中，只定位左侧标题为"
                "‘博学楼四楼半圆弧平7号’卡片内部的‘选座 >’可点击入口；"
                "这次坐标应落在文字右侧尖括号或该行可点击区域内，不要点右侧另一张卡片、"
                "点赞图标、上方‘去选座’或底部导航。只有明确显示‘预约成功’、‘我的预约’、"
                "‘本人已选座’等能证明当前账号已完成的状态，才允许success=true。"
                "‘已被预订’只表示座位被占或不可选，必须success=false。"
            )
            favorite = wait_target(
                retry_prompt, run_dir, "favorite-retry", screen_w, screen_h,
                checks=3, accept_success_without_target=True, deadline=deadline,
            )
            if favorite[0].get("success") and not favorite[0].get("target_found"):
                verify = favorite[0]
                break
    assert verify is not None
    if not verify.get("success"):
        raise FlowError(f"最终成功状态未确认：{verify.get('evidence') or verify.get('page')}")
    # The site may open a promotional article after accepting the selection.
    # Exit it with Android Back; this does not undo the accepted action.
    if any(token in str(verify.get("page") or "") for token in ("新闻", "广告", "推广", "文章")):
        current = observe()
        act(current, {"type": "global_action", "name": "back"})
        time.sleep(1.0)
    cleanup = clear_recent_apps(run_dir, screen_w, screen_h)
    ensure_window_open(deadline)
    wegolibrary = complete_wegolibrary_checkin()
    return {
        "screen": [screen_w, screen_h],
        "result": verify,
        "cleanup": cleanup,
        "wegolibrary": wegolibrary,
    }


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
    parser.add_argument("--until", type=parse_until)
    parser.add_argument("--diagnose-at", type=parse_until)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--finish-phone", action="store_true")
    parser.add_argument("--notify-channel")
    parser.add_argument("--notify-account")
    parser.add_argument("--notify-target")
    args = parser.parse_args()
    attempts = max(1, min(3, args.max_attempts))
    started = now_text()
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    base = ROOT / "artifacts" / f"checkin-{stamp}"
    base.mkdir(parents=True, exist_ok=False)
    errors = []
    preliminary_diagnosis = None
    diagnosis_stop = threading.Event()
    diagnosis_holder = {}

    def timed_diagnosis():
        if args.diagnose_at is None:
            return
        delay = max(0.0, (args.diagnose_at - datetime.now(TZ)).total_seconds())
        if diagnosis_stop.wait(delay):
            return
        diagnosis = diagnose_failures(base, list(errors), "recovery")
        diagnosis_holder["value"] = diagnosis
        if diagnosis_stop.is_set():
            return
        notice = (
            "⚠ 05:58–06:05 尚未完成签到，已进入 AI 诊断补救阶段，"
            "将继续完整重试至 06:10。\n"
            f"卡点：{diagnosis.get('stage') or 'unknown'}\n"
            f"判断：{diagnosis.get('diagnosis') or '暂无'}\n"
            f"后续：{diagnosis.get('next_action') or '继续完整流程'}"
        )
        notify_result = notify_wechat(
            notice, args.notify_channel, args.notify_account, args.notify_target
        )
        notify_path = base / "diagnosis-recovery-notify.json"
        notify_path.write_text(
            json.dumps(notify_result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(notify_path, 0o600)

    diagnosis_thread = threading.Thread(
        target=timed_diagnosis, name="checkin-ai-diagnosis", daemon=True
    )
    diagnosis_thread.start()
    try:
        attempt = 0
        while args.until is not None or attempt < attempts:
            ensure_window_open(args.until)
            attempt += 1
            attempt_dir = base / f"attempt-{attempt}"
            attempt_dir.mkdir()
            try:
                # Re-check on every transaction so a Node that reconnects
                # after 05:58 can still participate before the 06:05 cutoff.
                ensure_node()
                result = run_once(attempt_dir, deadline=args.until)
                ended = now_text()
                detail = {"status": "passed", "attempt": attempt, "startedAt": started,
                          "endedAt": ended, "artifacts": str(base), **result}
                write_run(detail)
                diagnosis_stop.set()
                print("✅ 手机自动签到完成")
                print("任务：签到")
                print("App：微信 / 我去图书馆 + WegoLibrary")
                print(f"时间：{ended}")
                print(f"结果：第 {attempt} 次尝试预约与到馆签到均已确认成功")
                if args.finish_phone:
                    finish = best_effort_finish(
                        attempt_dir, *(result.get("screen") or [None, None]), clear_apps=False
                    )
                    if finish.get("screenOffError"):
                        print(f"息屏提示：{finish['screenOffError']}")
                return 0
            except Exception as exc:
                errors.append(f"第{attempt}次：{exc}")
                retry_reset = {}
                if not isinstance(exc, WindowClosed) and "人工解锁" not in str(exc):
                    # Every retry is a fresh transaction. Clear any page/app
                    # left by a failed reservation or downstream check-in so
                    # the next attempt always begins again from WeChat.
                    retry_reset = best_effort_finish(
                        attempt_dir, clear_apps=True, power_off=False
                    )
                (attempt_dir / "attempt-error.json").write_text(
                    json.dumps({"attempt": attempt, "time": now_text(), "error": str(exc),
                                "retryReset": retry_reset},
                               ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.chmod(attempt_dir / "attempt-error.json", 0o600)
                if isinstance(exc, WindowClosed) or "人工解锁" in str(exc):
                    break
                if args.until is None and attempt >= attempts:
                    break
                ensure_window_open(args.until)
                retry_after = float(getattr(exc, "retry_after", 0) or 0)
                delay = max(args.retry_delay, retry_after) if retry_after else args.retry_delay
                time.sleep(max(0.5, min(90.0, delay)))
        raise FlowError("；".join(errors))
    except Exception as exc:
        diagnosis_stop.set()
        preliminary_diagnosis = diagnosis_holder.get("value")
        ended = now_text()
        final_diagnosis = diagnose_failures(base, errors or [str(exc)], "final")
        detail = {"status": "failed", "startedAt": started, "endedAt": ended,
                  "error": str(exc), "artifacts": str(base),
                  "preliminaryDiagnosis": preliminary_diagnosis,
                  "finalDiagnosis": final_diagnosis}
        write_run(detail)
        print("❌ 手机自动签到失败")
        print("任务：签到")
        print(f"时间：{ended}")
        print(f"原因：{exc}")
        print(f"AI诊断：{final_diagnosis.get('diagnosis') or '暂无明确结论'}")
        print(f"建议：{final_diagnosis.get('next_action') or '请检查最后截图'}")
        print(f"最后截图：{base}")
        if args.finish_phone:
            finish = best_effort_finish(base)
            if finish.get("cleanupError"):
                print(f"清理提示：{finish['cleanupError']}")
            if finish.get("screenOffError"):
                print(f"息屏提示：{finish['screenOffError']}")
        return 1


if __name__ == "__main__":
    raise SystemExit("Historical source archive; use offline tests.")
