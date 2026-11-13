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
import re
import fcntl
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
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
BOOKING_OPENS = None
BUSINESS_DEADLINE = None
SETTLEMENT_DEADLINE = None
RUN_CONTEXT = {}


class FlowError(RuntimeError):
    pass


class WindowClosed(FlowError):
    pass


class RestartFlow(FlowError):
    """A recovery decision requires restarting the complete transaction."""
    pass


class AlreadyRunning(FlowError):
    pass


@contextmanager
def single_run():
    ROOT.mkdir(parents=True, exist_ok=True)
    # Keep the inode: unlinking a held lock permits another process to lock a
    # replacement file. flock is released by the kernel even after a crash.
    with (ROOT / "checkin.lock").open("a+") as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("已有预约签到流程正在运行，本次不重复操控手机。") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def mark_stage(folder, stage, goal):
    RUN_CONTEXT.update(stage=stage, goal=goal, updatedAt=now_text())
    path = folder / "progress.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(RUN_CONTEXT, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


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


def bounded_timeout(seconds):
    ensure_window_open(BUSINESS_DEADLINE)
    if BUSINESS_DEADLINE is None:
        return seconds
    return min(seconds, max(.01, (BUSINESS_DEADLINE - datetime.now(TZ)).total_seconds()))


def wait_for_opening(deadline=None):
    """Prepare early, but never submit a new reservation before opening."""
    while BOOKING_OPENS is not None and datetime.now(TZ) < BOOKING_OPENS:
        ensure_window_open(deadline)
        remaining = (BOOKING_OPENS - datetime.now(TZ)).total_seconds()
        time.sleep(max(.05, min(10.0, remaining)))
    ensure_window_open(deadline)


def run_json(argv, timeout=55):
    proc = subprocess.run(argv, text=True, capture_output=True, timeout=bounded_timeout(timeout))
    if proc.returncode != 0:
        raise FlowError((proc.stderr or proc.stdout or "command failed").strip().splitlines()[-1])
    start = proc.stdout.find("{")
    if start < 0:
        raise FlowError("OpenClaw did not return JSON")
    return json.loads(proc.stdout[start:])


def invoke(command, params, timeout=55):
    if command in {"mobile.ui.launch", "mobile.ui.act"}:
        ensure_window_open(BUSINESS_DEADLINE)
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
    RUN_CONTEXT['nodeConnection'] = {
        'checkedAt': now_text(), 'connected': bool(node and node.get('connected')),
        **{k: (node or {}).get(k) for k in ('lastSeenAtMs','lastSeenReason','lastConnectedAtMs','approvalState')},
    }
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


class ActionNotSubmitted(FlowError):
    """Android explicitly rejected the gesture before execution."""


def fast_coordinate(*args):
    ensure_window_open(BUSINESS_DEADLINE)
    proc = subprocess.run(
        [str(NODE_BIN), str(FAST_ACT), *map(str, args)],
        text=True,
        capture_output=True,
        timeout=bounded_timeout(45),
    )
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError):
        payload = {}
    if payload.get('ok') is False and (payload.get('result') or {}).get('code') == 'target_stale':
        raise ActionNotSubmitted('target_stale：Android拒绝过期页面点击，未提交；必须重新截图定位')
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
    _, _, image_w, image_h = screenshot()
    if width < 720 or height < 1200 or abs((width / height)/(image_w / image_h)-1) > .04:
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
        raise FlowError("无法读取当前手机触控分辨率，停止使用历史分辨率猜坐标")
    return width, height


def provider():
    config = json.loads((Path.home() / ".openclaw/openclaw.json").read_text(encoding="utf-8"))
    item = config["models"]["providers"]["antigravity-gemini"]
    key = (Path.home() / ".config/cliproxyapi/openclaw-api-key").read_text(encoding="utf-8").strip()
    return item["baseUrl"].rstrip("/"), key


def _wegolibrary_request(path, token=None, data=None, form=False, timeout=25):
    """No attendance service is contacted by this archive."""
    ensure_window_open(BUSINESS_DEADLINE)
    raise WegoLibraryError("签到传输未启用：归档只支持隔离测试，请在馆内使用官方方式签到")


def checkin_success_text(value):
    text = str(value or '').strip()
    return text.startswith('签到成功') and not any(word in text for word in ('失败','未成功','未签到','不成功'))


def complete_wegolibrary_checkin(force=False, require_new_timestamp=True):
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
    if not force and previous_time.startswith(datetime.now(TZ).strftime("%Y-%m-%d")) and checkin_success_text(previous_result):
        return {"success": True, "message": previous_result, "idempotent": True}

    result = _wegolibrary_request("/api/checkin", token=token, data={})
    if result.get("success") is not True:
        raise WegoLibraryError(str(result.get("message") or "WegoLibrary签到未成功"), 60)

    verified = _wegolibrary_request("/api/status", token=token)
    verified_result = str(verified.get("last_checkin_result") or "")
    if (not checkin_success_text(verified_result) or
            not str(verified.get("last_checkin") or "").startswith(datetime.now(TZ).strftime("%Y-%m-%d"))):
        raise WegoLibraryError("WegoLibrary返回成功，但状态页未确认成功")
    if force and require_new_timestamp and previous_time and str(verified.get("last_checkin") or "") == previous_time:
        raise WegoLibraryError("新预约签到后状态时间未更新，不能复用旧预约的成功记录")
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


class VisionProviderError(FlowError):
    pass


def provider_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        detail = exc.read(8192).decode('utf-8', 'replace').lower()
        if 'validation_required' in detail or 'verify your account' in detail:
            reason = 'Google账号需要人工验证 VALIDATION_REQUIRED'
        elif exc.code in (401, 403):
            reason = '模型账号认证或权限异常'
        elif exc.code == 429:
            reason = '上游限流或额度不足'
        elif exc.code == 503:
            reason = '代理无可用模型凭据或上游暂不可用'
        else:
            reason = '模型接口错误'
        return VisionProviderError(f'视觉服务 HTTP {exc.code}：{reason}')
    return VisionProviderError('视觉服务连接失败：' + type(exc).__name__)


def model_preflight():
    base, key = provider()
    body = {'contents':[{'role':'user','parts':[{'text':'Reply OK'}]}]}
    req = urllib.request.Request(f'{base}/models/{MODEL}:generateContent',
        data=json.dumps(body).encode(), headers={'Content-Type':'application/json','x-goog-api-key':key}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=bounded_timeout(20)) as response:
            result = json.load(response)
        if not result.get('candidates'):
            raise VisionProviderError('模型预检无有效回复')
    except WindowClosed:
        raise
    except VisionProviderError:
        raise
    except Exception as exc:
        raise provider_error(exc) from None


def vision(raw, mime, instruction, request_timeout=90):
    base_url, key = provider()
    prompt = (
        "你是Android手机视觉定位器。仅根据本次真实截图像素判断，不使用历史坐标。"
        "截图覆盖完整手机屏幕。坐标必须用相对整张图的0到1000归一化整数。"
        "若目标不清楚或置信度不足，target_found必须为false，禁止猜测。"
        "适配深色/浅色模式、字体和网页缩放、图标或按钮颜色变化：依据文字含义、完整座位名称、"
        "所属卡片及当前真实页面结构定位，颜色和图标仅作辅证，左右位置不是身份依据。"
        "截图文字可能换行或增加空格，阅读完整名称；仅有图标且语义不确定时返回未找到。"
        "secure_lock仅指Android系统PIN/图形/密码/生物认证锁屏，不包括网页登录、学校绑定弹窗、黑屏或息屏。"
        "额外返回transient_auth_popup布尔值、dialog_text、confirm_text：当前网页弹窗提示‘尚未登录/尚未登陆/未登录/未登陆/请先登录/请先登陆/登录失效/登录过期’或‘您的学校需要您绑定账户’，且仅需点击‘确定/确认/好的’时为true。"
        "必须抄录当前可见弹窗文字和按钮，不得把真实账号密码或扫码登录表单当作确认弹窗。school_binding_popup字段兼容学校绑定弹窗。"
        "这是已知间歇弹窗，处理为点确定等待刷新，不需要人工绑定；存在时不得把背后的座位判为成功或可点击。"
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
            with urllib.request.urlopen(req, timeout=bounded_timeout(request_timeout)) as response:
                data = json.load(response)
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return parse_model_json(text)
        except WindowClosed:
            raise
        except urllib.error.HTTPError as exc:
            raise provider_error(exc) from None
        except Exception as exc:
            last = exc
            if attempt == 0:
                time.sleep(1.5)
    raise VisionProviderError(f"视觉模型请求失败：{type(last).__name__}")


def diagnose_failures(artifact_root, errors, label):
    """Ask the vision model to diagnose recent real screenshots and errors."""
    stable_before = time.time() - 0.5
    images = sorted(
        (item for item in artifact_root.rglob("*.jpg")
         if item.stat().st_mtime <= stable_before),
        key=lambda item: item.stat().st_mtime,
    )[-3:]
    parts = [{"text": (
        "你是无人值守Android预约任务故障诊断器。流程必须作为一个整体理解："
        "微信预约提交后立即调用签到适配器并由测试状态回执复核，最后才清后台；"
        "提交后若状态不明，只能优先核实签到，不能重新选座。根据最近真实截图与错误判断最可能卡点。"
        "本系统使用手机自身网络连接Android Node，不依赖USB或ADB，不要建议插USB。"
        "‘尚未登录/尚未登陆’等确认弹窗以及学校绑定确认弹窗，都先点确定/确认等待刷新后重新观察；不得直接要求人工登录。刷新前背景的已被预订字样不作为最终不可用结论。"
        "黑屏不证明安全锁屏；只有明确系统凭据界面才要求人工解锁。"
        "仅允许既定四楼和三楼半圆弧平7号；两者均被预订时等待并重新进入原流程，截止后报告无可用目标，禁止建议另选未经授权座位。"
        "不要建议绕过Android安全锁，不要输出任何凭据。严格输出JSON："
        "stage（node/wake/wechat/conversation/seat/reservation/cleanup/wegolibrary/unknown）、"
        "diagnosis、next_action、continue_safe（布尔值）、confidence（0到1）。\n"
        "最近错误：" + " | ".join(errors[-8:]) +
        "\n执行阶段记录（不要只根据收尾截图推断全部失败原因）：" + json.dumps(dict(RUN_CONTEXT),ensure_ascii=False)
    )}]
    for path in images:
        raw = path.read_bytes()
        parts.append({"inlineData": {
            "mimeType": "image/jpeg",
            "data": base64.b64encode(raw).decode("ascii"),
        }})
    base_url, key = provider()
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    request = urllib.request.Request(
        f"{base_url}/models/{MODEL}:generateContent",
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.load(response)
        text = "".join(
            part.get("text", "") for part in data["candidates"][0]["content"]["parts"]
        )
        diagnosis = parse_model_json(text)
    except Exception as exc:
        diagnosis = {
            "stage": "unknown",
            "diagnosis": f"模型诊断暂时不可用：{type(exc).__name__}",
            "next_action": "模型服务不可用，等待恢复；禁止盲点或重复提交预约。",
            "continue_safe": False,
            "confidence": 0,
        }
    output = artifact_root / f"diagnosis-{label}.json"
    output.write_text(
        json.dumps(diagnosis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(output, 0o600)
    return diagnosis


def notify_wechat(message, channel, account, target):
    if not (channel and account and target):
        return {"sent": False, "reason": "notification destination not configured"}
    try:
        proc = subprocess.run(
            [OPENCLAW, "message", "send", "--json", "--channel", channel,
             "--account", account, "--target", target, "--message", message],
            text=True, capture_output=True, timeout=45,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"sent": False, "reason": type(exc).__name__}
    if proc.returncode != 0:
        return {"sent": False, "reason": "message send failed"}
    return {"sent": True}


def merge_visual_votes(first, second):
    """Require two independent visual passes to agree before a coordinate click."""
    first = dict(first)
    popup_first = school_binding_popup_visible(first)
    popup_second = school_binding_popup_visible(second)
    first['school_binding_popup'] = popup_first or popup_second
    if popup_first != popup_second:
        first['target_found'] = False
        first['success'] = False
    first["success"] = first.get("success") is True and second.get("success") is True
    first["secure_lock"] = bool(first.get("secure_lock") or second.get("secure_lock"))
    if first.get('selection') or second.get('selection'):
        if (first.get("selection") != second.get("selection") or
                first.get("selection") not in {"primary", "backup", "owned", "none"}):
            first["target_found"] = False
            first["success"] = False
    first_found = first.get("target_found") is True
    second_found = second.get("target_found") is True
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
        if label.startswith('live-seat-'):
            if result.get('action') != review.get('action') or (result.get('selected_seat') or '') != (review.get('selected_seat') or ''):
                result['action'] = 'wait'
                result['target_found'] = False
                result['success'] = False
            result['verified_live'] = result.get('verified_live') is True and review.get('verified_live') is True
    if school_binding_popup_visible(result):
        result['secure_lock'] = False
        result['success'] = False
    safe = {k: v for k, v in result.items() if k not in {"token", "key"}}
    (artifact_dir / f"{label}.json").write_text(
        json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if result.get("secure_lock") and not school_binding_popup_visible(result):
        raise FlowError("手机需要人工解锁。")
    return result, image_w, image_h


def tap_visual(result, image_w, image_h, screen_w, screen_h):
    ensure_window_open(BUSINESS_DEADLINE)
    if result.get("target_found") is not True or float(result.get("confidence") or 0) < 0.70:
        raise FlowError(f"视觉目标未可靠找到：{result.get('evidence') or 'unknown'}")
    x_norm, y_norm = float(result["x_norm"]), float(result["y_norm"])
    if not all(math.isfinite(n) and 0 < n < 1000 for n in (x_norm, y_norm)):
        raise FlowError("视觉坐标无效或位于屏幕边界，要求重新定位")
    if min(image_w, image_h, screen_w, screen_h) <= 0 or abs((image_w/image_h)/(screen_w/screen_h)-1) > .04:
        raise FlowError("截图与触控坐标比例不一致，可能旋转、分屏或分辨率变化；重新观察")
    # Normalized coordinates avoid coupling the workflow to screenshot downscaling.
    x = round(x_norm * screen_w / 1000)
    y = round(y_norm * screen_h / 1000)
    fast_coordinate("tap", x, y)
    return {"xNorm": x_norm, "yNorm": y_norm, "screen": [screen_w, screen_h], "image": [image_w, image_h]}


def school_binding_popup_visible(result):
    """Legacy name; recognizes both transient login and school confirmation dialogs."""
    if result.get('transient_auth_popup') is True:
        return True
    if result.get('school_binding_popup') is True:
        return True
    page = str(result.get("page") or "")
    evidence = str(result.get("evidence") or "")
    dialog = str(result.get('dialog_text') or '')
    # Explicit negative flags and no transcribed dialog must not be overturned
    # by words quoted in a negative explanation (e.g. "无未登录弹窗").
    if (result.get('transient_auth_popup') is False and
            result.get('school_binding_popup') is False and not dialog.strip()):
        return False
    if not dialog.strip() and re.search(
            r'(?:无|未发现|没有发现|未见|没有|未出现|未检测到|未显示|不存在)[^。；;\n]{0,90}(?:弹窗|提示框|确认框|对话框)', evidence):
        return False
    combined = page + ' ' + evidence + ' ' + dialog
    login_markers = ('尚未登录','尚未登陆','未登录','未登陆','请先登录','请先登陆','登录失效','登录过期')
    if any(word in combined for word in login_markers):
        if not any(word in combined for word in ('不存在','未出现','未显示','未检测到','不是这个弹窗')):
            if any(word in combined for word in ('弹窗','提示框','确认框','对话框')) and any(
                    word in combined + str(result.get('confirm_text') or '') for word in ('确定','确认','好的')):
                return True
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
        "优先检查当前网页的间歇登录确认弹窗：‘尚未登录/尚未登陆/未登录/未登陆/请先登录/请先登陆/登录失效/登录过期’，"
        "以及‘您的学校需要您绑定账户’。这些提示只需点击确定/确认/好的并等待刷新，不是需要输入凭据的登录表单。"
        "匹配时transient_auth_popup=true,target_found=true，抄录dialog_text和confirm_text，返回该确认按钮中心坐标。"
        "另允许关闭纯信息系统通知/闭馆公告：仅当弹窗明确为公告且按钮文字为‘我知道了’或‘知道了’时，"
        "target_found=true，抄录dialog_text和confirm_text，定位该按钮中心；这不是登录弹窗，transient_auth_popup=false。"
        "关闭公告只表示移除遮挡，不代表当日开放或预约成功，必须重新观察页面。"
        "普通通知、广告或网络重试弹窗可选择明确的关闭/取消/我知道了/重试按钮，重新截图确认已消失后继续原目标。"
        "不得根据闭馆公告文字终止本流程。付款、退选、删除、账号授权、密码验证码弹窗禁止随意确认。"
        "颜色不作限制，禁止点击背景座位或其他授权按钮。真实密码/验证码/扫码登录页不能当作确认弹窗。"
        "弹窗消失但页面空白或刷新中时loading=true,success=false；只有弹窗消失且内容已加载才success=true。"
    )
    handled = False
    confirmations = 0
    for index in range(1, 9):
        ensure_window_open(deadline)
        result, image_w, image_h = inspect(
            prompt, artifact_dir, f"{label}-{index}", consensus=True
        )
        if result.get("target_found") and float(result.get("confidence") or 0) >= 0.80:
            if confirmations >= 3:
                raise FlowError("登录/绑定确认提示点击3次后仍未消失，未完成刷新")
            ensure_window_open(deadline)
            tap_visual(result, image_w, image_h, screen_w, screen_h)
            handled = True
            confirmations += 1
            # The page refreshes asynchronously after confirmation.
            time.sleep(5.0)
            continue
        if result.get("success") is True and result.get('loading') is not True:
            return {"handled": handled, "evidence": result.get("evidence")}
        if school_binding_popup_visible(result):
            raise FlowError("识别到登录/绑定确认提示，但未能可靠定位确认按钮")
        # Neither a confirmed modal nor a confirmed disappearance: reobserve
        # rather than silently allowing the next reservation click.
        if index < 8:
            time.sleep(bounded_timeout(3.0))
    raise FlowError("登录/绑定确认后页面未在有限等待内恢复，不认定座位不可用")


def wait_target(
    instruction, artifact_dir, label, screen_w, screen_h, checks=6,
    accept_success_without_target=False, deadline=None,
):
    last = None
    recovery_history = []
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
        if index in (2, 4) and index < checks:
            if label == 'favorite':
                return verify_live_seats(artifact_dir, screen_w, screen_h, deadline)
            recovery_history.append(recover_page(
                instruction, result.get("evidence") or label, artifact_dir,
                f"{label}-recovery-{index}", screen_w, screen_h,
                recovery_history, deadline,
            ))
            continue
        time.sleep(1.5 if result.get("loading") else 1.0)
    raise FlowError(f"找不到目标：{(last or {}).get('evidence') or label}")


FLOW_PURPOSE = (
    "业务链：唤醒手机→命令打开微信→会话列表找到‘我去图书馆’→进入公众号→点底部‘座位’"
    "→处理学校绑定提示并等待刷新→优先四楼半圆弧平7号，四楼被占且本人无预约才选三楼半圆弧平7号"
    "→保存提交记录→立即WegoLibrary登录、签到接口、当天结果复核→最后清理最近应用。"
    "‘已被预订’不能证明当前账号已预约；广告跳转只能证明点击响应。"
    "任一步未验证不得宣称整套成功。只允许上述两个座位，不改其他座位、账号、预约时间或系统配置。"
)

SEAT_SELECTION_PROMPT = (
    "先检查尚未登录/尚未登陆等确认弹窗。存在时transient_auth_popup=true,selection=none,success=false，先确认等待刷新，忽略背景座位状态。"
    "根据当前真实页面选择常用座位。主座位：左侧‘博学楼四楼半圆弧平7号’；"
    "唯一备用：右侧‘博学楼三楼半圆弧平7号’，不是二楼。先检查当前账号是否已预约。"
    "若页面明确证明本人已选座/预约成功，selection=owned,success=true,target_found=false，"
    "不要再预约另一座位。单独‘已被预订’或菜单名‘我的预约’均不能证明本人已预约。"
    "否则主座位存在‘选座 >’时selection=primary，只定位它内部该文字按钮。"
    "只有主座位明确为‘已被预订’且页面明确本人无预约（如‘您还没有选座哦’），"
    "或明确主座位属于别人，才允许改选备用；备用名称必须精确三楼且有‘选座 >’，"
    "此时selection=backup，只定位备用卡片内部‘选座 >’。"
    "若归属不明、两个都不可选或卡片名称不符，selection=none,success=false,target_found=false。"
    "不要点击点赞、整张卡片、其他楼层、取消预约或更换已有座位。"
    "在标准JSON额外返回selection(primary/backup/owned/none)、primary_status、ownership_evidence、"
    "selected_seat，evidence明确解释选择原因。找到按钮时success=false，目标找到不等于预约成功。"
)


def verify_live_seats(folder, width, height, deadline):
    """Read live details instead of treating favorite-card badges as final."""
    history = []
    for step in range(10):
        ensure_window_open(deadline)
        instruction = (
            '需要核验当前真实座位详情，首页常用座位水印不作为最终可用性结论。'
            '若当前在微信会话列表，先点我去图书馆；在公众号会话则点底部座位入口；其他任务不处理。'
            '仅允许博学楼四楼半圆弧平台7号（页面也写半圆弧平7号）和博学楼三楼半圆弧平台7号。优先四楼。'
            '不得选择明日预约、其他日期、其他楼层座位、取消现有预约、退选、付款、授权。'
            '根据当前截图返回action：navigate/scroll_down/scroll_up/wait/ready/owned/unavailable。'
            '先进入当天选座页面或指定座位详情，可点击去选座、底部选座、指定常用座位卡片、楼层/区域导航，'
            '选择楼层和区域不是提交座位；不得直接点未知编号。每次只返回一个动作及当前按钮中心，操作后会重新截图。'
            '任何可点击导航按钮定位可靠时必须target_found=true，confidence必须是0到1的小数；没有可靠按钮才false。'
            'navigate/wait/scroll等导航动作没有提交座位，selection必须省略，禁止返回空字符串或none。'
            '登录/绑定提示及信息公告先定位确定/取消/我知道了，action=navigate，dialog_text和confirm_text抄录真实内容。'
            '加载中action=wait；当前存在弹窗时不得定位其背后座位。'
            '若可直接提交指定座位，action=ready，target_found=true，selection=primary或backup，selected_seat完整写出座位身份；'
            '必须由当前详情标题、区域及编号一起证明是授权的7号，不能仅凭孤立数字7。'
            '若已明确是本人当天预约，action=owned,success=true,target_found=false。'
            'unavailable只允许实际进入当天实时详情且明确目标不可选；首页水印不能返回unavailable。'
            '返回verified_live布尔值、target_text、selected_seat、selection、evidence及标准JSON。'
            '不得执行预订动作的坐标导航：真正提交必须action=ready由外层处理。'
            '历史动作：'+json.dumps(history[-4:],ensure_ascii=False)
        )
        result, iw, ih = inspect(instruction, folder, f'live-seat-{step}', consensus=True)
        action = str(result.get('action') or 'wait')
        if school_binding_popup_visible(result):
            dismiss_school_binding_popup(folder,f'live-popup-{step}',width,height,deadline)
            history.append({'action':'confirm_popup'})
            continue
        if action == 'owned' and result.get('success') is True:
            return result, iw, ih
        if action == 'ready':
            seat = re.sub(r'\s+', '', str(result.get('selected_seat') or '')).replace('平台','平')
            allowed = {'博学楼四楼半圆弧平7号':'primary','博学楼三楼半圆弧平7号':'backup'}
            if seat not in allowed or result.get('selection') != allowed[seat]:
                raise FlowError('实时选座身份不完整，禁止提交其他座位')
            return result, iw, ih
        if action == 'unavailable' and result.get('verified_live') is True:
            raise RestartFlow('已进入实时详情核验授权座位暂不可用，等待重试：'+str(result.get('evidence') or ''))
        if action == 'navigate':
            text = str(result.get('target_text') or result.get('confirm_text') or '')
            if text in ('确定','确认','好的') and not any(word in str(result.get('dialog_text') or '') for word in ('登录','登陆','绑定','通知','网络','加载')):
                raise FlowError('确认按钮业务含义不明确，重新观察，不绕过提交记录')
            if any(word in text for word in ('明日','退选','取消预约','支付','授权','预订','预约确认','立即预约')):
                raise FlowError('实时导航目标涉及未授权提交或日期变更，重新定位')
            if not text or not any(word in text for word in ('选座','座位','我去图书馆','四楼','三楼','博学楼','半圆弧','确定','确认','我知道了','取消','关闭','刷新','重试','返回')):
                raise FlowError('实时导航目标未明确，重新定位')
            tap_visual(result, iw, ih, width, height)
            time.sleep(bounded_timeout(5 if result.get('dialog_text') else 2))
        elif action in ('scroll_down','scroll_up'):
            fast_coordinate('swipe',round(width*.5),round(height*(.75 if action=='scroll_down' else .35)),round(width*.5),round(height*(.35 if action=='scroll_down' else .75)),550)
            time.sleep(bounded_timeout(1.5))
        else:
            time.sleep(bounded_timeout(4))
        history.append({'action':action,'target':result.get('target_text'),'page':result.get('page')})
    raise RestartFlow('实时座位核验达到10步上限，保留截图并重新进入流程')


def recovery_plan(raw, mime, goal, error, history):
    return vision(raw, mime, FLOW_PURPOSE +
        "你是流程恢复决策器。根据当前真实截图、当前步骤目标、已失败动作判断最合适的一个动作。"
        "在原有JSON之外增加action、reason、expected、target_text。action只能为："
        "wait（加载/临时网络状态，等待5秒）；confirm_binding（尚未登录/尚未登陆等登录确认提示或学校绑定提示，点击确认后等待刷新）；"
        "scroll_down/scroll_up（向下/上浏览网页或列表）；back（离开错误页/广告）；"
        "relocalize（页面已变化，重新找原目标）；restart（清理最近应用，从微信预约完整重开）；"
        "manual（确实需要人工登录/解锁）。不要输出或执行代码，不要凭记忆输出坐标。"
        "manual必须额外提供manual_kind=login以及auth_evidence（当前真实登录页上的账号密码/验证码输入框等可见证据）；"
        "系统凭据锁屏使用secure_lock=true。座位均被占、目标缺失、加载失败不属于人工登录，选择restart或wait。"
        "不要点击预订、取消预约、改座、支付、发送消息或权限授权按钮。"
        "如果同一恢复动作刚失败，不要重复发呆，选择另一动作或restart。"
        "当前目标：" + goal + "；失败证据：" + str(error)[:1500] +
        "；本阶段已尝试：" + json.dumps(history[-3:], ensure_ascii=False), request_timeout=25)


def recover_page(goal, error, folder, label, width, height, history, deadline=None):
    """Bounded model-guided recovery. Normal flow must re-observe and verify afterward."""
    ensure_window_open(deadline)
    event = {"time": now_text(), "goal": goal, "error": str(error)[:1500]}
    path = folder / f"{label}-decision.json"
    try:
        raw, mime, _, _ = screenshot()
        (folder / f"{label}-before.jpg").write_bytes(raw)
        plan = recovery_plan(raw, mime, goal, error, history)
        action = str(plan.get("action") or "restart")
        allowed = {"wait", "confirm_binding", "scroll_down", "scroll_up", "back", "relocalize", "restart", "manual"}
        if action not in allowed or float(plan.get("confidence") or 0) < .75:
            action = "restart"
        if school_binding_popup_visible(plan):
            action = "confirm_binding"
        elif plan.get("secure_lock"):
            action = "manual"
        elif action == 'manual':
            # A business condition (e.g. both seats occupied) cannot be turned
            # into an authentication stop by an unconstrained model action.
            auth_evidence = str(plan.get('auth_evidence') or '')
            if (plan.get('manual_kind') != 'login' or not any(
                    word in auth_evidence for word in ('密码','验证码','扫码登录','登录表单'))):
                action = 'restart'
        if history and action == history[-1].get("action"):
            action = "restart"
        event.update(action=action, reason=str(plan.get("reason") or plan.get("evidence") or ""),
                     expected=str(plan.get("expected") or ""))
        ensure_window_open(deadline)
        if action == "manual":
            if plan.get('secure_lock'):
                raise FlowError("手机需要人工解锁。")
            raise FlowError("需要人工解锁或登录：检测到实际登录页，" + str(plan.get('auth_evidence') or ''))
        if action == "restart":
            raise RestartFlow("AI恢复选择从微信预约完整重开：" + event["reason"])
        if action == "confirm_binding":
            event["confirmation"] = dismiss_school_binding_popup(folder,label+'-confirm',width,height,deadline)
        elif action in {"scroll_down", "scroll_up"}:
            start, end = (.72, .35) if action == "scroll_down" else (.35, .72)
            fast_coordinate("swipe",round(width*.5),round(height*start),round(width*.5),round(height*end),500)
        elif action == "back":
            current = observe()
            ensure_window_open(deadline)
            act(current, {"type":"global_action","name":"back"})
        time.sleep(5 if action == "wait" else 1)
        ensure_window_open(deadline)
        after, _, _, _ = screenshot()
        (folder / f"{label}-after.jpg").write_bytes(after)
        event["outcome"] = "action_finished; next normal step must re-observe and verify goal"
        return event
    except (WindowClosed, RestartFlow):
        raise
    except Exception as exc:
        event["recovery_error"] = str(exc)
        if "人工解锁" in str(exc):
            raise
        raise RestartFlow("恢复动作不可用，完整重开：" + type(exc).__name__) from exc
    finally:
        path.write_text(json.dumps(event,ensure_ascii=False,indent=2),encoding="utf-8")
        os.chmod(path,0o600)


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
    mark_stage(run_dir, "wake", "唤醒手机并启动微信")
    screen_w, screen_h = prepare_display()
    mark_stage(run_dir, "conversation", "找到并进入我去图书馆")
    conversation_prompt = (
        "当前目标：在微信主界面会话列表中找到名称完全为‘我去图书馆’的会话行，"
        "给出该会话整行中心的可点击位置。若当前不在会话列表，target_found=false。"
    )
    # WeChat may resume a previous page or the target conversation may be below
    # the current viewport. Navigate by observed page type instead of blindly
    # pressing Back (which would exit WeChat from the conversation list).
    conversation = None
    restarted_wechat = False
    wake_retries = 0
    for navigation_count in range(10):
        ensure_window_open(deadline)
        result, iw, ih = inspect(
            conversation_prompt, run_dir, f"conversation-{navigation_count}", consensus=True
        )
        if school_binding_popup_visible(result):
            dismiss_school_binding_popup(run_dir, f'conversation-popup-{navigation_count}', screen_w, screen_h, deadline)
            continue
        if any(word in str(result.get('page') or '') for word in ('黑屏','息屏','熄屏','睡眠')):
            wake_retries += 1
            if wake_retries > 2:
                raise FlowError('手机显示未唤醒：两次唤醒后仍无可操作画面，不是会话不存在')
            screen_w, screen_h = prepare_display()
            continue
        if result.get("target_found") and float(result.get("confidence") or 0) >= 0.70:
            conversation = (result, iw, ih)
            break
        page = str(result.get("page") or "")
        if any(token in page.lower() for token in ('会话列表','聊天列表','消息列表','chat_list','conversation_list','wechat_home','微信首页')):
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
    mark_stage(run_dir, "seat_menu", "定位公众号底部座位入口")
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
    mark_stage(run_dir, "seat_page", "等待网页加载并处理学校绑定提示")
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

    # Any target measured before the opening wait would be stale; wait first,
    # then take fresh screenshots for the actual seat choice.
    mark_stage(run_dir, "opening_wait", "等待预约开放时间，随后重新截图选座")
    wait_for_opening(deadline)

    mark_stage(run_dir, "seat_selection", "确认本人预约状态，优先四楼，符合条件才用三楼备用")
    favorite_prompt = SEAT_SELECTION_PROMPT
    favorite = wait_target(
        favorite_prompt, run_dir, "favorite", screen_w, screen_h, checks=8,
        accept_success_without_target=True, deadline=deadline,
    )
    if favorite[0].get("success") and not favorite[0].get("target_found"):
        save_pending({'status':'owned_unconfirmed','date':datetime.now(TZ).strftime('%Y-%m-%d'),'submittedAt':now_text(),'seat':favorite[0].get('selected_seat')})
        result=settle_pending(run_dir)
        result['screen']=[screen_w,screen_h]
        return result
    mark_stage(run_dir,'reservation_submit','持久化提交意图，选座后优先调用签到接口')
    return submit_and_settle(favorite,run_dir,screen_w,screen_h,deadline)


def pending_reservation():
    path = ROOT / 'pending-reservation.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def save_pending(value):
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / 'pending-reservation.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    os.chmod(temp,0o600)
    os.replace(temp,path)


def settle_pending(run_dir):
    global BUSINESS_DEADLINE
    if SETTLEMENT_DEADLINE is not None:
        BUSINESS_DEADLINE = SETTLEMENT_DEADLINE
    ensure_window_open(BUSINESS_DEADLINE)
    pending = pending_reservation()
    if not pending or pending.get('status') == 'settled':
        raise FlowError('没有待核实的预约提交')
    if pending.get('date') != datetime.now(TZ).strftime('%Y-%m-%d'):
        raise FlowError('存在跨日未完成预约，需人工核对，禁止重新预约')
    mark_stage(run_dir,'checkin_pending','已可能提交预约：只重试签到，不再选座')
    outcome = complete_wegolibrary_checkin(force=True)
    save_pending({**pending,'status':'settled','settledAt':now_text(),'checkin':outcome})
    return {'result':{'success':True,'evidence':'签到接口及状态复核成功'},'wegolibrary':outcome,'selectedSeat':pending.get('seat')}


def submit_and_settle(favorite, run_dir, screen_w, screen_h, deadline):
    ensure_window_open(deadline)
    finish_limit = SETTLEMENT_DEADLINE or deadline
    if finish_limit and (finish_limit-datetime.now(TZ)).total_seconds() < 120:
        raise WindowClosed('剩余不足120秒，禁止新提交预约，避免预约后无时间签到')
    old = pending_reservation()
    if old and old.get('status') != 'settled':
        return settle_pending(run_dir)
    # Persist BEFORE the non-idempotent click. Even an RPC timeout may mean
    # the phone accepted it; never blindly issue a second reservation.
    for stale_attempt in range(3):
        ensure_window_open(deadline)
        save_pending({'status':'submission_uncertain','date':datetime.now(TZ).strftime('%Y-%m-%d'),'submittedAt':now_text(),'seat':favorite[0].get('selected_seat')})
        try:
            tap_visual(*favorite, screen_w, screen_h)
            break
        except ActionNotSubmitted:
            # Unlike RPC timeout, this is explicit proof no gesture ran.
            path = ROOT/'pending-reservation.json'
            archive = run_dir/f'rejected-submission-{stale_attempt}.json'
            path.replace(archive)
            if old:
                save_pending(old)
            if stale_attempt == 2:
                raise RestartFlow('页面连续变化，三次点击均被明确拒绝；重新进入流程，不转入空签到')
            dismiss_school_binding_popup(run_dir,f'stale-popup-{stale_attempt}',screen_w,screen_h,deadline)
            favorite = wait_target(SEAT_SELECTION_PROMPT,run_dir,f'stale-seat-{stale_attempt}',screen_w,screen_h,checks=3,accept_success_without_target=True,deadline=deadline)
            if favorite[0].get('success') and not favorite[0].get('target_found'):
                save_pending({'status':'owned_unconfirmed','date':datetime.now(TZ).strftime('%Y-%m-%d'),'submittedAt':now_text(),'seat':favorite[0].get('selected_seat')})
                return settle_pending(run_dir)
    time.sleep(bounded_timeout(2.0))
    result=settle_pending(run_dir)
    result['screen']=[screen_w,screen_h]
    return result


def write_run(detail):
    run_dir = ROOT / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"checkin-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(detail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    return path


def final_checkin_once(base):
    """One final API attempt, independent of phone state and booking deadline."""
    global BUSINESS_DEADLINE
    previous = BUSINESS_DEADLINE
    BUSINESS_DEADLINE = datetime.now(TZ) + timedelta(seconds=90)
    record = {'stage': 'final_checkin', 'startedAt': now_text()}
    try:
        # force bypasses the daily success cache: actually send one POST.
        result = complete_wegolibrary_checkin(force=True, require_new_timestamp=False)
        record.update(success=True, result=result)
        pending = pending_reservation()
        if pending and pending.get('date') == datetime.now(TZ).strftime('%Y-%m-%d'):
            save_pending({**pending, 'status': 'settled', 'settledAt': now_text(), 'checkin': result})
        print('✅ 最终追加签到确认成功：' + str(result.get('message', '')), flush=True)
    except Exception as exc:
        record.update(success=False, error=str(exc))
        print('⚠ 最终追加签到未确认成功：' + str(exc) + '；不再重试或重新预约。', flush=True)
    finally:
        BUSINESS_DEADLINE = previous
        record['endedAt'] = now_text()
        try:
            path = base/'final-checkin.json'
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            os.chmod(path, 0o600)
        except Exception as exc:
            print('最终签到记录保存失败：' + type(exc).__name__, flush=True)
    return record


def main():
    if (ROOT / 'SAFETY_HOLD').exists():
        print('自动预约已安全暂停：账号封禁及未签到事故尚待人工核实。禁止新预约。')
        return 1
    global BOOKING_OPENS, BUSINESS_DEADLINE, SETTLEMENT_DEADLINE
    parser = argparse.ArgumentParser(prog="android-signin")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--until", type=parse_until)
    parser.add_argument("--settle-until", type=parse_until)
    parser.add_argument("--diagnose-at", type=parse_until)
    parser.add_argument("--not-before", type=parse_until)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--finish-phone", action="store_true")
    parser.add_argument("--notify-channel")
    parser.add_argument("--notify-account")
    parser.add_argument("--notify-target")
    args = parser.parse_args()
    RUN_CONTEXT.clear()
    BOOKING_OPENS, BUSINESS_DEADLINE = args.not_before, args.until
    SETTLEMENT_DEADLINE = args.settle_until or args.until
    if args.until and SETTLEMENT_DEADLINE < args.until:
        parser.error('--settle-until must not precede --until')
    if args.until and args.not_before and args.until <= args.not_before:
        parser.error("--until must be later than --not-before")
    attempts = max(1, min(3, args.max_attempts))
    started = now_text()
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f")
    base = ROOT / "artifacts" / f"checkin-{stamp}"
    base.mkdir(parents=True, exist_ok=False)
    errors = []
    provider_unavailable = False
    recovery_history = []
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
            f"⚠ {args.diagnose_at.strftime('%H:%M')} 尚未完成签到，已进入 AI 诊断补救阶段，"
            f"新预约截止 {args.until.strftime('%H:%M') if args.until else '重试上限'}；已提交预约的签到补救截止 {SETTLEMENT_DEADLINE.strftime('%H:%M') if SETTLEMENT_DEADLINE else '重试上限'}。\n"
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
            pending = pending_reservation()
            BUSINESS_DEADLINE = SETTLEMENT_DEADLINE if pending and pending.get('status') != 'settled' else args.until
            ensure_window_open(BUSINESS_DEADLINE)
            attempt += 1
            attempt_dir = base / f"attempt-{attempt}"
            attempt_dir.mkdir()
            try:
                # Re-check on every transaction so a Node that reconnects
                # within the configured reservation window.
                mark_stage(attempt_dir, "node", "确认Android Node在线且具备所需能力")
                pending = pending_reservation()
                if pending and pending.get('status') != 'settled':
                    result = settle_pending(attempt_dir)
                else:
                    if provider_unavailable or attempt == 1:
                        mark_stage(attempt_dir, 'model_preflight', '先确认模型接口可用，再操作手机')
                        model_preflight()
                        provider_unavailable = False
                    ensure_node()
                    result = run_once(attempt_dir, deadline=args.until)
                ended = now_text()
                detail = {"status": "passed", "attempt": attempt, "startedAt": started,
                          "endedAt": ended, "artifacts": str(base), **result}
                diagnosis_stop.set()
                BUSINESS_DEADLINE = None
                print("✅ 手机自动签到完成")
                print("任务：签到")
                print("App：微信 / 我去图书馆 + WegoLibrary")
                print(f"时间：{ended}")
                print(f"结果：第 {attempt} 次尝试预约与到馆签到均已确认成功")
                if args.finish_phone:
                    try:
                        finish = best_effort_finish(
                            attempt_dir, *(result.get("screen") or [None, None]), clear_apps=True
                        )
                    except Exception as finish_error:
                        finish = {"screenOffError": type(finish_error).__name__}
                    if finish.get("screenOffError"):
                        print(f"息屏提示：{finish['screenOffError']}")
                    detail["finish"] = finish
                detail["finishedAt"] = now_text()
                # Business success is final. A disk/logging failure must never
                # restart a reservation that has already been confirmed.
                try:
                    write_run(detail)
                except Exception as record_error:
                    print(f"记录保存失败：{type(record_error).__name__}；预约签到已成功，不重复提交。")
                return 0
            except Exception as exc:
                failed_stage = dict(RUN_CONTEXT)
                errors.append(f"第{attempt}次：{exc}")
                retry_reset = {}
                recovery = None
                provider_failed = isinstance(exc, VisionProviderError)
                if provider_failed:
                    if not provider_unavailable:
                        notice = notify_wechat('⚠ 预约视觉服务异常：'+str(exc)+'。暂停手机点击，等待模型恢复；已有提交仍优先签到。', args.notify_channel, args.notify_account, args.notify_target)
                        (attempt_dir/'provider-alert.json').write_text(json.dumps(notice,ensure_ascii=False),encoding='utf-8')
                    provider_unavailable = True
                node_offline = str(exc) == 'Android Node 离线'
                pending = pending_reservation()
                needs_settlement = bool(pending and pending.get('status') != 'settled')
                if needs_settlement:
                    BUSINESS_DEADLINE = SETTLEMENT_DEADLINE
                    if isinstance(exc, WindowClosed) and SETTLEMENT_DEADLINE and datetime.now(TZ) < SETTLEMENT_DEADLINE:
                        exc = FlowError('新预约窗口已关闭，提交状态待核实，转入签到补救窗口')
                if node_offline and not any('Android Node 离线' in e for e in errors[:-1]):
                    delivery = notify_wechat('⚠ 手机节点离线，预约尚未开始。正在等待手机自行重连；请检查手机网络、VPN及OpenClaw后台。截止时间保持不变。', args.notify_channel, args.notify_account, args.notify_target)
                    notice_path = base / 'node-offline-notify.json'
                    try:
                        notice_path.write_text(json.dumps({'time':now_text(), **delivery},ensure_ascii=False),encoding='utf-8')
                        os.chmod(notice_path,0o600)
                    except OSError:
                        print('离线告警送达记录无法保存；继续等待重连')
                if (not provider_failed and not needs_settlement and not node_offline and not isinstance(exc, (WindowClosed, RestartFlow)) and
                        "人工解锁" not in str(exc)):
                    try:
                        ensure_window_open(args.until)
                        # Execution failures (RPC/API/stale UI) also receive a
                        # fresh-page diagnosis. Regardless of local recovery,
                        # the next transaction begins at WeChat reservation.
                        current = observe()
                        width, height = bounds_max(current)
                        if width >= 720 and height >= 1200:
                            recovery = recover_page(
                                "本轮失败，恢复可操作页面后从微信预约重新开始。" + FLOW_PURPOSE,
                                str(exc), attempt_dir, "transaction-recovery", width, height,
                                recovery_history, args.until,
                            )
                            recovery_history.append(recovery)
                    except WindowClosed as stopped:
                        exc = stopped
                    except Exception as recovery_error:
                        recovery = {"error": str(recovery_error)}
                        if "人工解锁" in str(recovery_error):
                            exc = recovery_error
                if not provider_failed and not needs_settlement and not node_offline and not isinstance(exc, WindowClosed) and "人工解锁" not in str(exc):
                    # Every retry is a fresh transaction. Clear any page/app
                    # left by a failed reservation or downstream check-in so
                    # the next attempt always begins again from WeChat.
                    retry_reset = best_effort_finish(
                        attempt_dir, clear_apps=True, power_off=False
                    )
                (attempt_dir / "attempt-error.json").write_text(
                    json.dumps({"attempt": attempt, "time": now_text(), "error": str(exc),
                                "retryReset": retry_reset, "recovery": recovery, "failedStage": failed_stage},
                               ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.chmod(attempt_dir / "attempt-error.json", 0o600)
                if isinstance(exc, WindowClosed) or "人工解锁" in str(exc):
                    break
                if args.until is None and attempt >= attempts:
                    break
                ensure_window_open(BUSINESS_DEADLINE)
                retry_after = float(getattr(exc, "retry_after", 0) or 0)
                delay = max(args.retry_delay, retry_after) if retry_after else args.retry_delay
                if node_offline:
                    delay = max(10.0, delay)
                if provider_failed:
                    delay = max(30.0, delay)
                time.sleep(bounded_timeout(max(0.5, min(90.0, delay))))
        raise FlowError("；".join(errors))
    except Exception as exc:
        diagnosis_stop.set()
        BUSINESS_DEADLINE = None
        preliminary_diagnosis = diagnosis_holder.get("value")
        ended = now_text()
        final_diagnosis = diagnose_failures(base, errors or [str(exc)], "final")
        detail = {"status": "failed", "startedAt": started, "endedAt": ended,
                  "error": str(exc), "artifacts": str(base), "failedStage": dict(RUN_CONTEXT),
                  "preliminaryDiagnosis": preliminary_diagnosis,
                  "finalDiagnosis": final_diagnosis}
        pending = pending_reservation()
        if pending and pending.get('status') != 'settled':
            detail['status'] = 'reservation_pending_checkin'
            detail['pendingReservation'] = pending
        write_run(detail)
        print("❌ 手机自动签到失败")
        pending = pending_reservation()
        if pending and pending.get('status') != 'settled':
            print('🚨 可能已预约但签到未确认：禁止重新选座，请立即核实预约并人工签到或联系图书馆。')
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
    finally:
        diagnosis_stop.set()
        final_checkin_once(base)


if __name__ == "__main__":
    raise SystemExit("Source archive: run the offline test suite instead.")
