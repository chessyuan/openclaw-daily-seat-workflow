#!/usr/bin/env python3
"""Visual Android workflow for the WeChat library-seat check-in."""

import argparse
import base64
import json
import io
import os
import subprocess
import sys
import threading
import time
import math
import re
import ssl
import fcntl
from concurrent.futures import ThreadPoolExecutor
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
RESERVATION_DEADLINE = None
RUN_CONTEXT = {}


class FlowError(RuntimeError):
    pass


class WindowClosed(FlowError):
    pass


class NoReservationAfterCutoff(WindowClosed):
    """Definitive no-booking proof: do not loop through settlement recovery."""
    pass


class RestartFlow(FlowError):
    """A recovery decision requires restarting the complete transaction."""
    pass


class SubmissionNeedsReview(FlowError):
    """Unknown post-click state cannot be resolved by blind booking or API loops."""
    pass


class CrossDaySubmission(SubmissionNeedsReview):
    """A prior-day unresolved transaction needs reconciliation, not a busy loop."""
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
    def __init__(self, message, retry_after=0, transient_read=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.transient_read = transient_read


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
        for output in (proc.stdout, proc.stderr):
            start = (output or '').find('{')
            if start >= 0:
                try:
                    failed = json.JSONDecoder().raw_decode(output[start:])[0]
                    detail = (failed.get('error') or {}).get('message')
                    if detail:
                        raise FlowError(str(detail))
                except (ValueError, TypeError, AttributeError):
                    pass
        raise FlowError((proc.stderr or proc.stdout or "command failed").strip().splitlines()[-1])
    start = proc.stdout.find("{")
    if start < 0:
        raise FlowError("OpenClaw did not return JSON")
    return json.loads(proc.stdout[start:])


def invoke(command, params, timeout=55):
    if command in {"mobile.ui.launch", "mobile.ui.act"}:
        ensure_window_open(BUSINESS_DEADLINE)
    if command not in {'mobile.ui.observe','mobile.ui.act','mobile.ui.launch','mobile.ui.screenshot'}:
        raise FlowError('Android workflow RPC command is not allowed')
    data = run_json([str(NODE_BIN), str(FAST_ACT), 'rpc', command,
        json.dumps(params, ensure_ascii=False, separators=(",", ":")),
        str(min(70000,max(1,round(timeout*1000))))], timeout=timeout)
    if data.get("ok") is not True:
        raise FlowError(f"{command} failed")
    return data.get("payload") or {}


def ensure_node():
    data = run_json([str(NODE_BIN),str(FAST_ACT),'status'], timeout=45)
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
    if result.get('code') == 'target_stale':
        raise ActionNotSubmitted('Android明确拒绝过期目标，未执行操作')
    if result.get("code") not in (None, "completed"):
        raise FlowError(f"Android action failed: {result.get('code')} {result.get('message') or ''}".strip())
    return result


class ActionNotSubmitted(FlowError):
    """Android explicitly rejected the gesture before execution."""


class DisplayGeometryChanged(ActionNotSubmitted):
    """Coordinate validation rejected the action before any tap was sent."""


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
    if payload.get('ok') is False and (payload.get('result') or {}).get('code') in {'target_stale','package_changed'}:
        if payload.get('completedGestures',0):
            raise FlowError('部分手势已完成但后续被拒绝，结果未确认；禁止按未执行盲目重放')
        raise ActionNotSubmitted('target_stale：Android拒绝过期页面点击，未提交；必须重新截图定位')
    if proc.returncode != 0:
        detail=(payload.get('error') or {}).get('message')
        if detail:
            raise FlowError(str(detail))
        raise FlowError((proc.stderr or proc.stdout or "fast coordinate action failed").strip().splitlines()[-1])
    result = json.loads(proc.stdout)
    if result.get("ok") is not True:
        raise FlowError(f"coordinate action failed: {result.get('result')}")
    return result


def screenshot():
    captured = run_json([str(NODE_BIN), str(FAST_ACT), 'capture'], timeout=55)
    payload = captured.get('frame') or {}
    if captured.get('ok') is not True or not captured.get('snapshotId'):
        raise FlowError('截图未绑定有效页面标识，禁止视觉点击')
    RUN_CONTEXT['visualFrame'] = {'snapshotId':captured['snapshotId'],
        'capturedAt':captured.get('capturedAt'), 'package':captured.get('package')}
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


def full_display_bounds(snapshot, image_w, image_h):
    """Use full-frame roots, not offscreen children or app content bounds."""
    if image_w <= 0 or image_h <= 0:
        return None
    candidates = []
    for node in snapshot.get('nodes', []):
        if node.get('parentRef') is not None:
            continue
        b = node.get('bounds')
        if isinstance(b, dict):
            b = [b.get(k, 0) for k in ('left', 'top', 'right', 'bottom')]
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            continue
        try:
            left, top, width, height = map(int, b)
        except (TypeError, ValueError):
            continue
        if left != 0 or top != 0 or min(width, height) < 480:
            continue
        if abs((width / height) / (image_w / image_h) - 1) > .01:
            continue
        candidates.append((width, height))
    return max(candidates, key=lambda x: x[0] * x[1]) if candidates else None


def remember_display(snapshot, image_w, image_h, dimensions, source):
    width, height = dimensions
    if width >= height:
        raise DisplayGeometryChanged('当前手机为横屏，需恢复竖屏后重新观察；未发送点击')
    RUN_CONTEXT['displayGeometry'] = {
        'width': width, 'height': height, 'image': [image_w, image_h],
        'source': source, 'observedPackage': snapshot.get('package'),
        'legacyBoundsMax': list(bounds_max(snapshot)), 'checkedAt': now_text(),
    }
    return width, height


def measure_current_display():
    """Preserve the page while calibrating against the system's full window."""
    current = observe()
    _, _, iw, ih = screenshot()
    dimensions = full_display_bounds(current, iw, ih)
    if dimensions:
        return remember_display(current, iw, ih, dimensions, 'current_full_window_root')
    original_package = current.get('package')
    opened = False
    try:
        fast_coordinate('global', 'notifications')
        opened = True
        time.sleep(bounded_timeout(.7))
        shade = observe()
        _, _, iw, ih = screenshot()
        dimensions = full_display_bounds(shade, iw, ih)
        if shade.get('package') != 'com.android.systemui' or not dimensions:
            raise DisplayGeometryChanged('系统全屏窗口与截图未匹配，不能可靠校准；未发送点击')
        measured = remember_display(shade, iw, ih, dimensions, 'systemui_full_window_root')
    finally:
        if opened:
            fast_coordinate('global', 'back')
            time.sleep(bounded_timeout(.6))
    restored = observe()
    if original_package and restored.get('package') != original_package:
        raise DisplayGeometryChanged('校准后前台页面发生变化，必须重新识别；未发送点击')
    return measured


def current_display_dimensions(width, height):
    geometry = RUN_CONTEXT.get('displayGeometry') or {}
    return int(geometry.get('width', width)), int(geometry.get('height', height))


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
        except WindowClosed:
            raise
        except Exception as exc:
            last_error = exc
            if launch_attempt == 0:
                if 'DEVICE_LOCKED' in str(exc):
                    # Samsung may report Keyguard while Recents is still
                    # covering an otherwise usable launcher. Home cannot
                    # bypass secure credentials, but can dismiss that overlay.
                    fast_coordinate('global','home')
                time.sleep(1.2)
    if last_error is not None:
        if 'DEVICE_LOCKED' in str(last_error):
            raw,mime,_,_=screenshot()
            prompt=('只判断当前真实画面。仅Android系统PIN/图形/密码/指纹凭据界面才secure_lock=true；'
                    '最近任务、桌面、黑屏或应用加载中不证明安全锁屏。不要输入或猜测任何凭据。'
                    'confidence代表状态判断把握，返回page、secure_lock、loading、confidence；不定位点击。')
            votes=parallel_visual_votes(raw,mime,[prompt,'独立复核当前是否真有系统凭据锁屏。'+prompt])
            if all(v.get('secure_lock') is True and float(v.get('confidence') or 0)>=.85 for v in votes):
                raise FlowError('手机需要人工解锁。真实画面已确认系统安全凭据界面。') from None
            current=observe()
            if not (current.get('package')==WECHAT and
                    all(v.get('secure_lock') is False and v.get('loading') is not True and
                        float(v.get('confidence') or 0)>=.85 for v in votes)):
                raise FlowError('微信唤醒尚未完成，画面未确认安全锁屏；保留现场，禁止盲点或猜测凭据。') from None
        else:
            raise FlowError(f"手机唤醒/本地解锁/启动微信失败：{last_error}")
    time.sleep(2.5)
    current = observe()
    _, _, image_w, image_h = screenshot()
    dimensions = full_display_bounds(current, image_w, image_h)
    if not dimensions:
        # Accessibility trees are intentionally empty inside WeChat. Measure
        # Android's actual gesture coordinate space on the launcher, then
        # return to WeChat. Screenshot dimensions are capped at 2048 and must
        # never be used as gesture dimensions on this 1440x3120 device.
        act(current, {"type": "global_action", "name": "home"})
        time.sleep(1.0)
        launcher = observe()
        _, _, image_w, image_h = screenshot()
        dimensions = full_display_bounds(launcher, image_w, image_h)
        if dimensions and dimensions[0] < dimensions[1]:
            remember_display(launcher, image_w, image_h, dimensions, 'launcher_full_window_root')
        elif not dimensions:
            dimensions = measure_current_display()
        # Samsung's launcher can rotate while WeChat returns to portrait.
        # Do not reject the intermediate launcher before relaunching WeChat.
        invoke("mobile.ui.launch", {"package": WECHAT})
        time.sleep(2.0)
    else:
        remember_display(current, image_w, image_h, dimensions, 'current_full_window_root')
    _, _, image_w, image_h = screenshot()
    width, height = dimensions
    if width >= height or abs((width / height) / (image_w / image_h) - 1) > .01:
        width, height = measure_current_display()
        _, _, image_w, image_h = screenshot()
    if width >= height or abs((width / height) / (image_w / image_h) - 1) > .01:
        raise DisplayGeometryChanged('启动微信后显示比例变化，需重新校准；未发送点击')
    return width, height


def provider():
    config = json.loads((Path.home() / ".openclaw/openclaw.json").read_text(encoding="utf-8"))
    item = config["models"]["providers"]["antigravity-gemini"]
    key = (Path.home() / ".config/cliproxyapi/openclaw-api-key").read_text(encoding="utf-8").strip()
    return item["baseUrl"].rstrip("/"), key


def _wegolibrary_request(path, token=None, data=None, form=False, timeout=25):
    # Only the read-only status endpoint may be replayed here. Never replay
    # login or check-in POSTs after an ambiguous transport failure.
    if path != '/api/status' or data is not None:
        return _wegolibrary_request_once(path, token, data, form, timeout)
    expires = time.monotonic() + timeout
    for attempt in range(2):
        remaining = expires - time.monotonic()
        if remaining <= 0:
            raise WegoLibraryError('WegoLibrary状态查询预算已用尽', 15)
        try:
            return _wegolibrary_request_once(path, token, data, form, min(12, remaining))
        except WegoLibraryError as exc:
            remaining = expires - time.monotonic()
            if attempt or not exc.transient_read or exc.retry_after > 1 or remaining < 2:
                raise
            # The business deadline still wins over this small local retry.
            time.sleep(bounded_timeout(1))
            ensure_window_open(BUSINESS_DEADLINE)
            RUN_CONTEXT['statusReadRetries'] = RUN_CONTEXT.get('statusReadRetries', 0) + 1


def _wegolibrary_request_once(path, token=None, data=None, form=False, timeout=25):
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
    receipt_path = ROOT/'checkin-verification.json'
    pending = pending_reservation() or {}
    transaction = str(pending.get('submittedAt') or datetime.now(TZ).strftime('%Y-%m-%d'))
    receipt = RUN_CONTEXT.get('checkinReceipt')
    if require_new_timestamp and receipt is None and receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text())
        except (OSError, ValueError):
            raise WegoLibraryError('签到结果核实记录不可读，禁止盲目重复提交', 15)
    if (require_new_timestamp and receipt and receipt.get('transaction') == transaction
            and receipt.get('date') == datetime.now(TZ).strftime('%Y-%m-%d')):
        if (checkin_success_text(previous_result) and previous_time.startswith(datetime.now(TZ).strftime('%Y-%m-%d'))
                and previous_time >= receipt['acceptedAt']
                and (not receipt.get('previousTime') or previous_time != receipt['previousTime'])):
            return {'success':True,'message':previous_result,'lastCheckin':previous_time,
                    'reconciledAcceptedRequest':True}
        raise WegoLibraryError('签到接口已接受，状态仍待核实；先查询结果，不重复提交', 15)
    if not force and previous_time.startswith(datetime.now(TZ).strftime("%Y-%m-%d")) and checkin_success_text(previous_result):
        return {"success": True, "message": previous_result, "idempotent": True}

    requested_at = now_text()
    result = _wegolibrary_request("/api/checkin", token=token, data={})
    if result.get("success") is not True:
        raise WegoLibraryError(str(result.get("message") or "WegoLibrary签到未成功"), 60)

    # Persist acceptance before the verification GET: a failed read must not
    # cause another POST on the next attempt. Final explicitly requested extra
    # check-in (require_new_timestamp=False) still sends its single POST.
    receipt = {'transaction':transaction,'date':datetime.now(TZ).strftime('%Y-%m-%d'),
               'acceptedAt':requested_at,'previousTime':previous_time}
    RUN_CONTEXT['checkinReceipt'] = receipt
    write_private_json(receipt_path, receipt)

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


def vision(raw, mime, instruction, request_timeout=45):
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
    step_end = time.monotonic() + request_timeout
    for attempt in range(2):
        try:
            remaining = step_end - time.monotonic()
            if remaining <= .05:
                break
            with urllib.request.urlopen(req, timeout=bounded_timeout(remaining)) as response:
                data = json.load(response)
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return parse_model_json(text)
        except WindowClosed:
            raise
        except urllib.error.HTTPError as exc:
            raise provider_error(exc) from None
        except Exception as exc:
            last = exc
            if attempt == 0 and step_end-time.monotonic() > 2:
                time.sleep(bounded_timeout(1.0))
    raise VisionProviderError(f"视觉模型请求失败：{type(last).__name__}")


def parallel_visual_votes(raw, mime, instructions):
    """Keep two successful votes. Retry only transient failed inference, never actions.

    All votes use identical bytes. 18s first calls plus one bounded retry each
    fit a shared 40s wall budget; a successful vote is never thrown away merely
    because the other request timed out. Final pixel freshness is still required.
    """
    expires=time.monotonic()+40
    results=[None,None];failed=[]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(vision,raw,mime,prompt,18) for prompt in instructions]
        for index,future in enumerate(futures):
            try:results[index]=future.result()
            except VisionProviderError as exc:
                if not any(tag in str(exc) for tag in ('TimeoutError','URLError','ConnectionResetError','HTTP 502','HTTP 503','HTTP 504')):
                    raise
                failed.append(index)
        remaining=expires-time.monotonic()
        if failed:
            if remaining<3:raise VisionProviderError('双重视觉复核总时限已到，未授权点击')
            RUN_CONTEXT['pairedVisionRetries']=int(RUN_CONTEXT.get('pairedVisionRetries',0))+len(failed)
            retries={index:executor.submit(vision,raw,mime,instructions[index],min(18,remaining)) for index in failed}
            for index,future in retries.items():results[index]=future.result()
    return results


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
        with urllib.request.urlopen(request, timeout=45) as response:
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
    result = _notify_wechat_once(message, channel, account, target)
    try:
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader(
            'android_notification_outbox', str(Path(__file__).with_name('android-notification-outbox.py')))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module.track(result, message, channel, account, target)
    except Exception as exc:
        # Notification bookkeeping must not alter reservation/check-in success.
        return {**result, 'outboxError': type(exc).__name__}


def _notify_wechat_once(message, channel, account, target):
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
        # Report only known error categories; CLI logs may contain sensitive
        # routing/auth details and must never be copied into task artifacts.
        combined = proc.stdout + proc.stderr
        if "prepare failed" in combined:
            reason = "wechat_upstream_prepare_rejected"
        elif "STALE_TOKEN" in combined or "ret=-14" in combined:
            reason = "wechat_auth_expired"
        elif "timeout" in combined.lower():
            reason = "delivery_timeout_unknown_do_not_replay"
        else:
            reason = "message_send_failed"
        return {"sent": False, "reason": reason}
    result = None
    for offset, char in enumerate(proc.stdout):
        if char != "{":
            continue
        try:
            parsed, _ = json.JSONDecoder().raw_decode(proc.stdout[offset:])
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("action") == "send":
            result = parsed
            break
    payload = (result or {}).get("payload", {})
    if (payload.get("deliveryStatus") != "sent" or
            not (result or {}).get("messageId")):
        return {"sent": False, "reason": "delivery_not_confirmed"}
    return {"sent": True, "accepted_by": "wechat_api", "message_id": result["messageId"]}


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
        if (first.get('selection') in {'primary','backup','owned'} and
                canonical_seat_name(first.get('selected_seat')) != canonical_seat_name(second.get('selected_seat'))):
            first['target_found'] = False
            first['success'] = False
            first['seatIdentityMismatch'] = True
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


def inspect(instruction, artifact_dir, label, consensus=False, page_only=False, consensus_fields=()):
    started=time.monotonic()
    raw, mime, image_w, image_h = screenshot()
    captured=time.monotonic()
    frame = dict(RUN_CONTEXT.get('visualFrame') or {})
    (artifact_dir / f"{label}.jpg").write_bytes(raw)
    review_instruction = "这是独立复核，不参考另一模型的坐标。重新从完整截图定位目标。" + instruction
    if consensus and not label.startswith('live-seat-'):
        # Same target contract, same pixels, independent parallel calls. This
        # avoids spending twice the inference latency on every harmless menu.
        result,review=parallel_visual_votes(raw,mime,[instruction,review_instruction])
    else:
        result = vision(raw, mime, instruction)
        if consensus:
            target = {k: result.get(k) for k in ('action','target_text','selected_seat','selection')}
            review_instruction += (
                '本轮规划已选择以下语义目标（不提供坐标）：'+json.dumps(target,ensure_ascii=False)+
                '。仅独立核验这同一个目标是否安全、可见并定位其中心；禁止改选另一个入口。'
                '目标不存在或不安全则target_found=false；不要给其他按钮坐标。'
            )
            review = vision(raw, mime, review_instruction)
    if consensus:
        (artifact_dir / f"{label}-review.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if page_only:
            # State-only observations never authorize a click. A model may
            # return target_found=true but null coordinates when no target was
            # requested; compare state/success without treating that as a tap.
            result = {**result, 'target_found': False, 'coordinateFreeObservation': True}
            review = {**review, 'target_found': False}
        first_evidence = result.get('evidence')
        result = merge_visual_votes(result, review)
        for field in consensus_fields:
            if result.get(field) != review.get(field):
                result[field] = 'unknown'
                result['success'] = False
            if min(float(result.get('confidence') or 0), float(review.get('confidence') or 0)) < .85:
                result[field] = 'unknown'
                result['success'] = False
        if ('reservation_state' in consensus_fields and result.get('reservation_state') == 'owned'
                and canonical_seat_name(result.get('selected_seat')) != canonical_seat_name(review.get('selected_seat'))):
            result['reservation_state'] = 'unknown'
            result['success'] = False
        if (page_only and 'reservation_state' in consensus_fields
                and result.get('reservation_state') in {'owned','unowned_home','login_popup','advertisement','loading'}
                and result.get('reservation_state') == review.get('reservation_state')):
            result['evidence'] = (f"双重只读状态复核一致（未授权点击）：{result['reservation_state']}；"
                                  f"首轮：{first_evidence}；复核：{review.get('evidence')}")
        if label.startswith('live-seat-'):
            if (result.get('action') != review.get('action') or
                    canonical_seat_name(result.get('selected_seat')) != canonical_seat_name(review.get('selected_seat')) or
                    (result.get('action') == 'navigate' and
                     re.sub(r'\s+','',str(result.get('target_text') or '')) !=
                     re.sub(r'\s+','',str(review.get('target_text') or '')))):
                result['action'] = 'wait'
                result['target_found'] = False
                result['success'] = False
            result['verified_live'] = result.get('verified_live') is True and review.get('verified_live') is True
    if school_binding_popup_visible(result):
        result['secure_lock'] = False
        result['success'] = False
    result['_visualSnapshotId'] = frame.get('snapshotId')
    result['_visualImagePath'] = str(artifact_dir / f'{label}.jpg')
    result['_visualInstruction'] = instruction
    result['_timing']={'captureSeconds':round(captured-started,3),
        'inferenceSeconds':round(time.monotonic()-captured,3)}
    safe = {k: v for k, v in result.items() if k not in {"token", "key"}}
    (artifact_dir / f"{label}.json").write_text(
        json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if result.get("secure_lock") and not school_binding_popup_visible(result):
        raise FlowError("手机需要人工解锁。")
    return result, image_w, image_h


def compare_visual_frames(original, current, x_norm, y_norm, banner=None):
    """CPU-only pixel checks; ignore status-clock noise, not dialog/target changes."""
    from PIL import Image, ImageChops, ImageStat
    with Image.open(io.BytesIO(original)) as image: before=image.convert('RGB')
    with Image.open(io.BytesIO(current)) as image: after=image.convert('RGB')
    if before.size != after.size:
        raise DisplayGeometryChanged('点击前截图分辨率改变，未执行点击')
    width,height=before.size
    def metric(box):
        diff=ImageChops.difference(before.crop(box),after.crop(box))
        bands=diff.split();maximum=ImageChops.lighter(ImageChops.lighter(bands[0],bands[1]),bands[2])
        histogram=maximum.histogram()
        return {'changedFraction':sum(histogram[28:])/max(1,sum(histogram)),
                'meanDifference':sum(ImageStat.Stat(diff).mean)/3}
    # Only a separately dual-model-verified advertising rectangle may be
    # excluded. Never weaken the independent target patch check below.
    body_before,body_after=before.copy(),after.copy()
    if banner is not None:
        from PIL import ImageDraw
        box=tuple(round(v*(width if i%2==0 else height)/1000) for i,v in enumerate(banner))
        ImageDraw.Draw(before).rectangle(box,fill='black')
        ImageDraw.Draw(after).rectangle(box,fill='black')
    body=metric((0,round(height*.045),width,round(height*.98)))
    before,after=body_before,body_after
    x=round(x_norm*width/1000);y=round(y_norm*height/1000)
    dx=max(24,round(width*.07));dy=max(24,round(height*.045))
    # Top-left webpage controls sit close to notification icons. Status-bar
    # changes must not masquerade as changes to the actual close button.
    local=metric((max(0,x-dx),max(round(height*.045),y-dy),
                  min(width,x+dx),min(round(height*.98),y+dy)))
    return {'body':body,'target':local,'match':body['changedFraction']<.02 and
            body['meanDifference']<3 and local['changedFraction']<.008 and local['meanDifference']<1.5}


def verify_advertising_rebind(result, original, current, image_w, image_h):
    """A changed carousel is not a stale seat. Recheck semantics, not just pixels.

    No action is sent here. Both fresh visual passes must approve the same
    target and a bounded, unrelated top-page advertising strip. Everything
    outside that strip, AND the target patch, must still match twice.
    """
    intent=result.get('_visualInstruction')
    if not intent:
        raise ActionNotSubmitted('画面改变且缺少原目标意图，未执行点击')
    expected={k:result.get(k) for k in ('page','selection','selected_seat','dialog_text','confirm_text')}
    prompt=(
        '这是点击前的最新完整截图复核，不是重新规划。原任务：'+intent+
        '\n只允许定位同一个原目标，不允许选择其它目标。原目标语义：'+json.dumps(expected,ensure_ascii=False)+
        '\n原目标中心(整图0..1000)：'+str([result['x_norm'],result['y_norm']])+
        '。独立确认当前页面、弹窗、预约状态及同一目标仍可操作；不能因背景座位可见而忽略弹窗。'
        '额外严格返回rebind_safe布尔值、advertising_rect_norm=[left,top,right,bottom]：'
        '只标出页面上半部独立、纯宣传广告轮播横幅的完整矩形；不含导航、学校/账号、状态、'
        '通知、弹窗、任何座位或操作控件。没有明确广告、页面状态有疑问或原目标不安全则'
        'rebind_safe=false且target_found=false。禁止把任意变化区域说成广告。'
        '同一目标必须target_found=true且返回最新坐标；原来有selection/selected_seat则必须原样核实返回。'
        '原目标是确认弹窗时必须抄录同一dialog_text和confirm_text。'
    )
    started=time.monotonic()
    votes=parallel_visual_votes(current,'image/jpeg',[prompt,'独立安全复核，不参考另一份结论。'+prompt])
    rectangles=[]
    for vote in votes:
        try:
            values=[vote['confidence'],vote['x_norm'],vote['y_norm']]
            if any(isinstance(v,bool) for v in values):raise ValueError('boolean coordinates')
            confidence,vx,vy=map(float,values)
            if not all(math.isfinite(v) for v in (confidence,vx,vy)):raise ValueError('nonfinite coordinates')
        except (KeyError,TypeError,ValueError):
            raise ActionNotSubmitted('广告复核置信度或坐标无效，未执行点击') from None
        if (vote.get('rebind_safe') is not True or vote.get('target_found') is not True or
                not .90<=confidence<=1 or vote.get('secure_lock') or vote.get('loading')):
            raise ActionNotSubmitted('广告变化复核未确认原目标安全，未执行点击')
        if math.hypot(vx-float(result['x_norm']),vy-float(result['y_norm']))>20:
            raise ActionNotSubmitted('广告变化同时目标发生移动，需重新定位；未执行点击')
        if school_binding_popup_visible(vote)!=school_binding_popup_visible(result):
            raise ActionNotSubmitted('点击前弹窗状态变化，未执行点击')
        for key in ('selection','selected_seat'):
            if result.get(key):
                left,right=result[key],vote.get(key)
                if key=='selected_seat':left,right=canonical_seat_name(left),canonical_seat_name(right)
                if left!=right:
                    raise ActionNotSubmitted('广告复核目标身份不同，未执行点击')
        if school_binding_popup_visible(result):
            for key in ('dialog_text','confirm_text'):
                if not result.get(key) or re.sub(r'\s+','',str(vote.get(key) or ''))!=re.sub(r'\s+','',str(result[key])):
                    raise ActionNotSubmitted('确认弹窗文字变化或不明确，未执行点击')
        rect=vote.get('advertising_rect_norm')
        if (not isinstance(rect,list) or len(rect)!=4 or
                not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in rect)):
            raise ActionNotSubmitted('广告边界无效，未执行点击')
        l,t,r,b=rect
        if not (0<=l<r<=1000 and 100<=t<b<=450 and b-t<=230 and (r-l)*(b-t)<=200000):
            raise ActionNotSubmitted('广告范围超出安全上限，未执行点击')
        rectangles.append(rect)
    # Intersection, not union: both independent passes must approve each pixel.
    banner=[max(r[0] for r in rectangles),max(r[1] for r in rectangles),
            min(r[2] for r in rectangles),min(r[3] for r in rectangles)]
    x,y=float(result['x_norm']),float(result['y_norm'])
    if (banner[2]<=banner[0] or banner[3]<=banner[1] or
            (banner[0]-70<=x<=banner[2]+70 and banner[1]-45<=y<=banner[3]+45)):
        raise ActionNotSubmitted('广告区域触及点击目标，未执行点击')
    first_check=compare_visual_frames(original,current,x,y,banner=banner)
    if not first_check['match']:
        raise ActionNotSubmitted('广告以外页面也发生变化，未执行点击')
    # Inference takes seconds. Bind only to a NEW capture after inference; keep
    # the carousel exemption narrow and reject any new modal/layout/seat change.
    fresh,_,width,height=screenshot()
    if (width,height)!=(image_w,image_h):
        raise DisplayGeometryChanged('广告复核后屏幕尺寸变化，未执行点击')
    final_check=compare_visual_frames(current,fresh,x,y,banner=banner)
    record={'verifiedAt':now_text(),'seconds':round(time.monotonic()-started,3),
            'banner':banner,'initialCheck':first_check,'finalCheck':final_check,
            'votes':votes,'accepted':final_check['match']}
    path=Path(result['_visualImagePath'])
    path.with_name(path.stem+'-banner-recheck.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
    path.with_name(path.stem+'-banner-current.jpg').write_bytes(current)
    path.with_name(path.stem+'-banner-final.jpg').write_bytes(fresh)
    if not final_check['match']:
        raise ActionNotSubmitted('广告复核推理期间页面再次发生实质变化，未执行点击')
    RUN_CONTEXT['lastFrameValidation']={**final_check,'verifiedAt':now_text(),
        'reason':'dual_verified_advertising_only','banner':banner}
    snapshot_id=(RUN_CONTEXT.get('visualFrame') or {}).get('snapshotId')
    if not snapshot_id:
        raise ActionNotSubmitted('广告复核截图缺少页面标识，未执行点击')
    return snapshot_id


def validate_visual_frame(result, image_w, image_h):
    path=Path(str(result.get('_visualImagePath') or '')).resolve()
    if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
        raise ActionNotSubmitted('缺少可信原始截图，未执行点击')
    original=path.read_bytes()
    current,_,width,height=screenshot()
    if (width,height)!=(image_w,image_h):
        raise DisplayGeometryChanged('点击前画面尺寸改变，需重新校准定位；未执行点击')
    check=compare_visual_frames(original,current,float(result['x_norm']),float(result['y_norm']))
    RUN_CONTEXT['lastFrameValidation']={**check,'verifiedAt':now_text()}
    path.with_name(path.stem+'-preclick.jpg').write_bytes(current)
    if not check['match']:
        if (check['target']['changedFraction']<.008 and check['target']['meanDifference']<1.5
                and result.get('_visualInstruction')):
            return verify_advertising_rebind(result, original, current, image_w, image_h)
        raise ActionNotSubmitted('推理期间页面或目标区域改变，必须重新截图定位；未执行点击')
    snapshot_id=(RUN_CONTEXT.get('visualFrame') or {}).get('snapshotId')
    if not snapshot_id:
        raise ActionNotSubmitted('点击前截图未绑定页面标识，未执行点击')
    return snapshot_id


def tap_visual(result, image_w, image_h, screen_w, screen_h, gesture='tap-bound'):
    if gesture not in {'tap-bound','doubletap-bound'}:
        raise FlowError('视觉动作不在允许列表中')
    ensure_window_open(BUSINESS_DEADLINE)
    if result.get("target_found") is not True or float(result.get("confidence") or 0) < 0.70:
        raise FlowError(f"视觉目标未可靠找到：{result.get('evidence') or 'unknown'}")
    x_norm, y_norm = float(result["x_norm"]), float(result["y_norm"])
    if not all(math.isfinite(n) and 0 < n < 1000 for n in (x_norm, y_norm)):
        raise FlowError("视觉坐标无效或位于屏幕边界，要求重新定位")
    screen_w, screen_h = current_display_dimensions(screen_w, screen_h)
    if min(image_w, image_h, screen_w, screen_h) <= 0 or abs((image_w/image_h)/(screen_w/screen_h)-1) > .04:
        RUN_CONTEXT['displayGeometryMismatch'] = {
            'screen': [screen_w, screen_h], 'image': [image_w, image_h], 'checkedAt': now_text(),
        }
        raise DisplayGeometryChanged("截图与触控坐标比例不一致，需重新校准并重新观察；未发送点击")
    # Normalized coordinates avoid coupling the workflow to screenshot downscaling.
    x = round(x_norm * screen_w / 1000)
    y = round(y_norm * screen_h / 1000)
    snapshot_id = result.get('_visualSnapshotId')
    if not snapshot_id:
        raise ActionNotSubmitted('视觉结果缺少截图页面标识，重新截图定位；未发送点击')
    # WebViews can emit accessibility epochs while pixels stay unchanged.
    # Rebind ONLY after both full-page and target-patch pixel verification.
    for frame_attempt in range(2):
        snapshot_id = validate_visual_frame(result, image_w, image_h)
        try:
            fast_coordinate(gesture, snapshot_id, x, y)
            break
        except ActionNotSubmitted:
            # Only explicit rejection (no gesture) can retry, and only after
            # another full-page + target-patch comparison. RPC timeout cannot.
            if frame_attempt == 1:
                raise
    return {"xNorm": x_norm, "yNorm": y_norm, "screen": [screen_w, screen_h], "image": [image_w, image_h]}


def tap_navigation(target, instruction, folder, label, screen_w, screen_h, deadline=None):
    """Only for non-submitting navigation. Never retry an uncertain booking."""
    for attempt in range(3):
        ensure_window_open(deadline)
        try:
            return tap_visual(*target, screen_w, screen_h)
        except ActionNotSubmitted as rejected:
            if attempt == 2:
                raise RestartFlow('导航连续三次被明确拒绝，保留现场后再恢复')
            if isinstance(rejected, DisplayGeometryChanged):
                screen_w, screen_h = measure_current_display()
            time.sleep(bounded_timeout(.8))
            target = inspect(instruction,folder,f'{label}-stale-{attempt}',consensus=True)


def refresh_seat_page(folder, screen_w, screen_h, deadline=None, label='seat-refresh'):
    """Use native Back for a confirmed WebView, then observe its destination."""
    prompt = (
        '只做重新打开图书馆网页的导航。page字段必须严格取以下英文枚举之一，不要用其它页面名称：'
        'official_conversation：截图确为我去图书馆公众号会话且底部有文字完全为座位的菜单；'
        '只定位这个座位菜单。library_webview：截图顶部明确web.traceint.com且有关闭网页的X；'
        '只定位顶部关闭网页的X，不选底部返回、不选座、不退座。'
        'login_popup：已知尚未登录/绑定账户的确定提示，此时target_found=false。'
        'loading：页面正在加载，此时target_found=false。unknown：其余页面或无法可靠确定。'
        '不得因为任务要求就假设当前是公众号会话。网页底部的首页/选座/发现/我的不是公众号座位菜单。'
        '不得点击消息中的查看详情、签到、预约、退座、取消预约、删除或任何业务提交按钮。'
    )
    steps = []
    for index in range(6):
        ensure_window_open(deadline)
        target = inspect(prompt, folder, f'{label}-route-{index}', consensus=True,
                         consensus_fields=('page',))
        result = target[0]
        route = result.get('page')
        result['refresh_route'] = route
        if school_binding_popup_visible(result) or route == 'login_popup':
            dismiss_school_binding_popup(folder, f'{label}-popup-{index}', screen_w, screen_h, deadline)
            continue
        if route == 'loading':
            time.sleep(bounded_timeout(2))
            continue
        if (route not in {'official_conversation', 'library_webview'} or
                result.get('target_found') is not True or result.get('loading') or
                isinstance(result.get('confidence'), bool) or
                not .85 <= float(result.get('confidence') or 0) <= 1):
            raise RestartFlow('刷新导航无法确认当前页面，保留截图；不再假设返回后就是公众号会话')
        step = {'index': index, 'route': route, 'startedAt': now_text(),
                'action': 'native_back' if route == 'library_webview' else 'seat_menu',
                'outcome': 'unconfirmed'}
        steps.append(step)
        try:
            if route == 'library_webview':
                # The changing carousel lies inside the old close-button
                # pixel patch. Back has no image coordinate and cannot submit
                # a seat. Bind to a fresh native snapshot in WeChat, and do
                # not assume the next page is the official conversation.
                current = observe()
                if current.get('package') != WECHAT:
                    raise RestartFlow('刷新前前台已离开微信，未发送返回；需重新观察')
                ensure_window_open(deadline)
                act(current, {'type': 'global_action', 'name': 'back'})
            else:
                tap_visual(*target, screen_w, screen_h)
            step['outcome'] = 'completed'
        except ActionNotSubmitted as rejected:
            step.update(outcome='not_submitted', reason=str(rejected)[:300])
            # A stale frame may now be a different route. Reclassify as well
            # as relocating, rather than carrying the old route forward.
            if isinstance(rejected, DisplayGeometryChanged):
                screen_w, screen_h = measure_current_display()
            time.sleep(bounded_timeout(.8))
            continue
        except Exception as exc:
            # An unknown outcome is never converted to an explicit rejection.
            step['reason'] = type(exc).__name__
            raise
        finally:
            step['endedAt'] = now_text()
            write_private_json(folder/f'{label}-navigation.json', {'steps': steps})
        time.sleep(bounded_timeout(1 if route == 'library_webview' else 3))
        if route == 'official_conversation':
            dismiss_school_binding_popup(folder, label+'-popup', screen_w, screen_h, deadline)
            scroll_seat_page(screen_w, screen_h, deadline, folder, label+'-scroll')
            return
    raise RestartFlow('网页刷新导航六次内未完成，停止局部循环并保留现场')


def wait_and_refresh_opening(folder,width,height,deadline):
    prepared_before_opening = BOOKING_OPENS is not None and datetime.now(TZ) < BOOKING_OPENS
    wait_for_opening(deadline)
    if prepared_before_opening:
        mark_stage(folder,'opening_refresh','开放后重新拉取服务端页面，不能只重截开放前页面')
        refresh_seat_page(folder,width,height,deadline,label='opening-refresh')


def scroll_seat_page(width,height,deadline,folder,label):
    width, height = current_display_dimensions(width, height)
    for attempt in range(3):
        ensure_window_open(deadline)
        try:
            fast_coordinate('swipe',round(width*.5),round(height*.78),round(width*.5),round(height*.25),650)
            time.sleep(bounded_timeout(1))
            return
        except ActionNotSubmitted:
            # Reobserve rather than reusing a gesture against an unrelated page.
            state,_,_ = inspect('确认当前仍在web.traceint.com图书馆首页，非弹窗、广告或其它页面。'
                              '符合时success=true，不需要定位按钮。',folder,f'{label}-{attempt}',consensus=True,page_only=True)
            if not state.get('success') or school_binding_popup_visible(state):
                raise RestartFlow('滚动被拒绝且当前页面变化，需要恢复原页面')
            time.sleep(bounded_timeout(.8))
    raise RestartFlow('页面滚动三次被拒绝，终止局部重试')


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
        "这是弹窗状态检查：内容正常加载且没有任何遮挡弹窗时，必须success=true,target_found=false。"
        "未指定要点击的目标、没有按钮需要点击，不是失败；不得因此把success设为false。"
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
            try:
                tap_visual(result, image_w, image_h, screen_w, screen_h)
            except ActionNotSubmitted:
                # Explicit rejection means no confirmation happened. Reobserve
                # the dialog locally; do not exit the page or reset WeChat.
                time.sleep(bounded_timeout(.8))
                continue
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
                refresh_seat_page(artifact_dir,screen_w,screen_h,deadline,label='favorite-refresh')
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


def canonical_seat_name(value):
    text = re.sub(r'\s+', '', str(value or '')).replace('平台','平').replace('场馆','')
    return re.sub(r'[（(][34][）)]$', '', text)


def ambiguous_seat_navigation(result):
    """A favorite's select button may submit, never treat it as navigation."""
    text = re.sub(r'\s+', '', str(result.get('target_text') or result.get('confirm_text') or ''))
    if result.get('selected_seat') or canonical_seat_name(text) in (
            '博学楼四楼半圆弧平7号', '博学楼三楼半圆弧平7号'):
        return True
    # Only explicitly named list/tab entrances are navigation. Bare 选座,
    # including alternate chevrons, must use the booking contract first.
    return '选座' in text and text not in {'去选座', '底部选座'}


def verify_live_seats(folder, width, height, deadline):
    width, height = current_display_dimensions(width, height)
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
            '首页没有可用常用座位按钮时，唯一优先导航目标为去选座；若未显示它才定位底部选座。'
            '首页常用座位内的选座 > 按钮可能直接完成预约，绝不是navigate：'
            '有可用常用座位时按下述常用座位规则返回action=ready，由外层先保存提交意图后点击并立即签到。'
            '只有去选座或底部选座确认为列表入口时才可navigate，target_text分别完整写为去选座或底部选座。'
            '禁止点击标为已被预订的整张常用座位卡片，该卡片不保证进入详情。'
            '先进入当天选座页面或指定座位详情，可点击去选座、底部选座、楼层/区域导航，'
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
            '常用座位规则：'+SEAT_SELECTION_PROMPT+
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
            seat = canonical_seat_name(result.get('selected_seat'))
            allowed = {'博学楼四楼半圆弧平7号':'primary','博学楼三楼半圆弧平7号':'backup'}
            if seat not in allowed or result.get('selection') != allowed[seat]:
                raise FlowError('实时选座身份不完整，禁止提交其他座位')
            return result, iw, ih
        if action == 'unavailable' and result.get('verified_live') is True:
            raise RestartFlow('已进入实时详情核验授权座位暂不可用，等待重试：'+str(result.get('evidence') or ''))
        if action == 'navigate':
            text = str(result.get('target_text') or result.get('confirm_text') or '')
            if ambiguous_seat_navigation(result):
                # Reobserve with the exact main booking contract. Never cast a
                # navigation vote into permission to submit using its old frame.
                checked = inspect(SEAT_SELECTION_PROMPT, folder,
                                  f'live-booking-target-{step}', consensus=True)
                favorite = checked[0]
                if school_binding_popup_visible(favorite):
                    dismiss_school_binding_popup(folder,f'live-booking-popup-{step}',width,height,deadline)
                elif (favorite.get('success') is True and not favorite.get('target_found')
                      and favorite.get('loading') is not True):
                    return checked
                elif (favorite.get('target_found') is True and not favorite.get('success')
                      and favorite.get('loading') is not True
                      and float(favorite.get('confidence') or 0) >= .85
                      and {'博学楼四楼半圆弧平7号':'primary','博学楼三楼半圆弧平7号':'backup'}.get(
                          canonical_seat_name(favorite.get('selected_seat'))) == favorite.get('selection')
                      and favorite.get('selection') in {'primary','backup'}):
                    return checked
                history.append({'action':'reobserve_same_page','reason':'疑似提交入口未获独立预约验证；未发送点击'})
                continue
            if text in ('确定','确认','好的') and not any(word in str(result.get('dialog_text') or '') for word in ('登录','登陆','绑定','通知','网络','加载')):
                raise FlowError('确认按钮业务含义不明确，重新观察，不绕过提交记录')
            if any(word in text for word in ('明日','退选','取消预约','支付','授权','预订','预约确认','立即预约')):
                raise FlowError('实时导航目标涉及未授权提交或日期变更，重新定位')
            if not text or not any(word in text for word in ('选座','座位','我去图书馆','四楼','三楼','博学楼','半圆弧','确定','确认','我知道了','取消','关闭','刷新','重试','返回')):
                raise FlowError('实时导航目标未明确，重新定位')
            if not result.get('target_found') or float(result.get('confidence') or 0) < .70:
                history.append({'action':'reobserve_same_page','reason':'目标复核不一致'})
                time.sleep(bounded_timeout(1))
                continue
            try:
                tap_visual(result, iw, ih, width, height)
            except ActionNotSubmitted:
                history.append({'action':'reobserve_same_page','reason':'明确未执行，页面已变化'})
                time.sleep(bounded_timeout(.8))
                continue
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
    width, height = current_display_dimensions(width, height)
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
    screen_w, screen_h = current_display_dimensions(screen_w, screen_h)
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
            try:
                tap_visual(result, image_w, image_h, screen_w, screen_h)
            except ActionNotSubmitted as rejected:
                # Explicit no-gesture rejection: reobserve Recents locally,
                # not another business transaction or an unbound old click.
                if isinstance(rejected, DisplayGeometryChanged):
                    screen_w,screen_h=measure_current_display()
                time.sleep(bounded_timeout(.5))
                continue
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
    tap_visual(result,image_w,image_h,screen_w,screen_h,gesture='doubletap-bound')
    return {"requested": True, "gestureCompleted": True, "powerStateVerified": False,
            "method": "samsung_launcher_double_tap"}


def best_effort_finish(artifact_dir, screen_w=None, screen_h=None, clear_apps=True, power_off=True):
    global BUSINESS_DEADLINE
    result = {}
    finish_deadline=BUSINESS_DEADLINE
    try:
        if power_off and finish_deadline is not None:
            BUSINESS_DEADLINE=min(finish_deadline,datetime.now(TZ)+timedelta(seconds=35))
        if not screen_w or not screen_h:
            snap = observe()
            screen_w, screen_h = bounds_max(snap)
        if clear_apps and screen_w >= 720 and screen_h >= 1200:
            result["cleanup"] = clear_recent_apps(
                artifact_dir, screen_w, screen_h, label_prefix="final-recents"
            )
    except Exception as exc:
        result["cleanupError"] = str(exc)
    finally:
        BUSINESS_DEADLINE=finish_deadline
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
        "额外返回entry_route，只允许以下枚举：conversation_list（微信会话列表）；"
        "official_conversation（名称明确为我去图书馆的公众号会话且底部可见座位菜单）；"
        "library_webview（顶部明确显示web.traceint.com域名的图书馆网页）；other（其余或不确定）。"
        "不要根据历史记忆、相似图标或网页正文猜域名；弹窗存在仍需如实标记。"
        "本轮confidence也代表页面分类把握，准确判断不在会话列表并不等于低置信度；"
        "entry_route明确可靠时仍给出真实的分类置信度，不能仅因target_found=false就填0。"
    )
    # WeChat may resume a previous page or the target conversation may be below
    # the current viewport. Navigate by observed page type instead of blindly
    # pressing Back (which would exit WeChat from the conversation list).
    conversation = None
    page_prepared = False
    restarted_wechat = False
    wake_retries = 0
    for navigation_count in range(10):
        ensure_window_open(deadline)
        result, iw, ih = inspect(
            conversation_prompt, run_dir, f"conversation-{navigation_count}", consensus=True,
            consensus_fields=('entry_route',)
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
        if (result.get('entry_route') in {'official_conversation','library_webview'}
                and result.get('loading') is not True):
            # Both independent passes agreed on an exact known entry route.
            # Refresh through observed X/menu navigation; never reuse stale
            # availability, and never Back all the way to the chat list.
            mark_stage(run_dir,'entry_reuse','复用已打开的图书馆入口，重新加载真实服务端页面')
            refresh_seat_page(run_dir,screen_w,screen_h,deadline,label='entry-reuse')
            RUN_CONTEXT['navigationReuse']={'route':result['entry_route'],'checkedAt':now_text()}
            page_prepared=True
            break
        page = str(result.get("page") or "")
        observed_package=(RUN_CONTEXT.get('visualFrame') or {}).get('package')
        if observed_package and observed_package != WECHAT:
            # A user or external deep-link may foreground another app. Never
            # Back/swipe that unknown app; resume our explicitly allowed app.
            invoke('mobile.ui.launch',{'package':WECHAT})
            time.sleep(bounded_timeout(2))
            continue
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
    if conversation is None and not page_prepared:
        raise FlowError("微信会话列表中未找到‘我去图书馆’")
    if not page_prepared:
        open_seat_page_from_conversation(conversation,conversation_prompt,run_dir,screen_w,screen_h,deadline)

    # Any target measured before the opening wait would be stale; wait first,
    # then take fresh screenshots for the actual seat choice.
    mark_stage(run_dir, "opening_wait", "等待预约开放时间，随后重新截图选座")
    wait_and_refresh_opening(run_dir,screen_w,screen_h,deadline)

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


def open_seat_page_from_conversation(conversation,conversation_prompt,run_dir,screen_w,screen_h,deadline):
    """Original cold-entry route, kept separate from verified warm re-entry."""
    mark_stage(run_dir, "seat_menu", "定位公众号底部座位入口")
    seat_prompt = (
        "当前目标：在‘我去图书馆’页面底部菜单中找到文字为‘座位’的按钮，"
        "给出‘座位’按钮中心。不要点击‘考研商城’或‘更多’。"
    )
    seat = None
    for conversation_click_attempt in range(2):
        tap_navigation(conversation,conversation_prompt,run_dir,'conversation-click',screen_w,screen_h,deadline)
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
    tap_navigation(seat,seat_prompt,run_dir,'seat-menu-click',screen_w,screen_h,deadline)
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
    scroll_seat_page(screen_w,screen_h,deadline,run_dir,'seat-scroll')



def pending_reservation():
    path = ROOT / 'pending-reservation.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def write_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w',encoding='utf-8') as stream:
        os.chmod(temp,0o600)
        json.dump(value,stream,ensure_ascii=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)


def save_pending(value):
    write_private_json(ROOT/'pending-reservation.json',value)


SUBMISSION_STATE_PROMPT = (
    '只观察点击选座后的真实页面，不执行任何操作。额外返回reservation_state，严格为：'
    'owned（当前本人学习中/在座/待签到，且显示完整座位；单独已被预订不算）；'
    'unowned_home（完整web.traceint.com首页明确显示您还没有选座哦，没有任何本人预约/在座信息且没有弹窗遮挡）；'
    'login_popup（已知尚未登录/绑定账户的确定提示）；'
    'advertisement（明显广告跳转，非选座确认或学习页面）；'
    'loading（正在加载）；unknown（其他情况，包括需要进一步确认预约的页面）。'
    '同时返回ownership_evidence和selected_seat。owned才success=true。'
    '不得把需要确认的预约页面当作无预约首页，不得按历史截图猜测。'
)


def inspect_submission_state(folder, label):
    result, _, _ = inspect(SUBMISSION_STATE_PROMPT, folder, label,
                           consensus=True, page_only=True, consensus_fields=('reservation_state',))
    if school_binding_popup_visible(result):
        result['reservation_state'] = 'login_popup'
    if result.get('reservation_state') == 'owned':
        allowed = {'博学楼四楼半圆弧平7号', '博学楼三楼半圆弧平7号'}
        if result.get('success') is not True or canonical_seat_name(result.get('selected_seat')) not in allowed:
            result['reservation_state'] = 'unknown'
    if result.get('reservation_state') == 'unowned_home':
        evidence = str(result.get('ownership_evidence') or '') + str(result.get('evidence') or '')
        if '您还没有选座' not in evidence:
            result['reservation_state'] = 'unknown'
    return result


def reconcile_submission(run_dir, pending):
    """No booking here: inspect after definitive no-seat rejection, never blind replay."""
    mark_stage(run_dir, 'reservation_reconcile', '签到返回无选座：重新观察点击后页面，先处理弹窗，不重复空签到')
    ensure_node()
    width, height = current_display_dimensions(0, 0)
    if not width or not height:
        width, height = measure_current_display()
    state = None
    popup_refreshed = False
    for index in range(3):
        ensure_window_open(BUSINESS_DEADLINE)
        state = inspect_submission_state(run_dir, f'post-submit-{index}')
        kind = state.get('reservation_state')
        if kind == 'owned':
            save_pending({**pending, 'status': 'owned_unconfirmed', 'ownershipVerifiedAt': now_text(),
                          'seat': state.get('selected_seat'), 'noSeatRejected': False})
            return 'owned'
        if kind == 'login_popup':
            handled = dismiss_school_binding_popup(run_dir, f'post-submit-popup-{index}', width, height,
                                                    deadline=BUSINESS_DEADLINE)
            # This handler returns only after fresh screenshots confirm the
            # modal disappeared and asynchronous page loading completed.
            popup_refreshed = bool(handled and handled.get('handled'))
            continue
        if kind == 'advertisement':
            fast_coordinate('global', 'back')
            time.sleep(bounded_timeout(3))
            continue
        if kind == 'unowned_home':
            # Two independent models on two fresh screenshots after either
            # confirmed modal auto-refresh or an explicit page reopen. Never
            # discard a pending click based on an API timeout alone.
            time.sleep(bounded_timeout(3))
            if not popup_refreshed:
                refresh_seat_page(run_dir, width, height, BUSINESS_DEADLINE, label='submission-proof-refresh')
            verified = inspect_submission_state(run_dir, 'post-submit-fresh-proof')
            if verified.get('reservation_state') == 'owned':
                save_pending({**pending, 'status': 'owned_unconfirmed', 'ownershipVerifiedAt': now_text(),
                              'seat': verified.get('selected_seat'), 'noSeatRejected': False})
                return 'owned'
            if verified.get('reservation_state') != 'unowned_home':
                state = verified
                continue
            if pending.get('status') == 'owned_unconfirmed' or pending.get('ownershipVerifiedAt'):
                RUN_CONTEXT['submissionReviewRequired'] = True
                RUN_CONTEXT['submissionReviewScreenshot'] = str(run_dir/'post-submit-fresh-proof.jpg')
                raise SubmissionNeedsReview('此前已有本人预约证据，现与无预约页面矛盾；保留记录，禁止重订，需要核对截图')
            submitted = datetime.strptime(pending['submittedAt'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
            if (datetime.now(TZ) - submitted).total_seconds() < 20:
                time.sleep(bounded_timeout(5))
                continue
            if RESERVATION_DEADLINE is None or datetime.now(TZ) >= RESERVATION_DEADLINE:
                raise NoReservationAfterCutoff('双重复核为本人未选座且新预约窗口已关闭；停止重复复核，执行最后一次签到后结束')
            # Preserve negative evidence before removing active pending state.
            archive = run_dir / 'submission-proven-not-completed.json'
            archive.write_text(json.dumps({'pending': pending, 'first': state, 'fresh': verified,
                                            'refreshMethod': 'confirmed_modal_auto_refresh' if popup_refreshed else 'observed_page_reopen',
                                            'verifiedAt': now_text()}, ensure_ascii=False, indent=2))
            os.chmod(archive, 0o600)
            if int(RUN_CONTEXT.get('verifiedResubmissionCount', 0)) >= 2:
                RUN_CONTEXT['submissionReviewRequired'] = True
                RUN_CONTEXT['submissionReviewScreenshot'] = str(run_dir/'post-submit-fresh-proof.jpg')
                raise SubmissionNeedsReview('两次已验证未提交的恢复仍未完成预约，停止循环并保留现场')
            (ROOT/'pending-reservation.json').unlink()
            RUN_CONTEXT['reservationRestartReady'] = True
            raise RestartFlow('签到接口无选座，刷新前后双重视觉均确认本人未预约；返回完整预约流程')
        if kind == 'loading':
            time.sleep(bounded_timeout(5))
            continue
        break
    RUN_CONTEXT['submissionReviewRequired'] = True
    RUN_CONTEXT['submissionReviewScreenshot'] = str(run_dir / f'post-submit-{index}.jpg')
    reviews = int(pending.get('unknownReviews', 0)) + 1
    save_pending({**pending, 'unknownReviews': reviews, 'lastReviewAt': now_text(),
                  'reviewScreenshot': RUN_CONTEXT['submissionReviewScreenshot']})
    if reviews >= 2:
        raise SubmissionNeedsReview('两轮视觉复核仍不能确认点击结果，保留截图并停止空循环，需要核对当前页面')
    raise WegoLibraryError('点击选座后的状态仍不明确，已保存截图；暂停空签到，禁止猜测重新选座', 30)


def resume_verified_reservation(run_dir, deadline):
    """Restart the complete booking+check-in transaction on a verified fresh page."""
    ensure_window_open(deadline)
    RUN_CONTEXT.pop('reservationRestartReady', None)
    RUN_CONTEXT['verifiedResubmissionCount'] = int(RUN_CONTEXT.get('verifiedResubmissionCount', 0)) + 1
    mark_stage(run_dir, 'reservation_retry', '已核实未预约：重新识别授权座位，预约后立即签到，避免重复冷启动微信')
    width, height = current_display_dimensions(0, 0)
    if not width or not height:
        width, height = measure_current_display()
    target = wait_target(SEAT_SELECTION_PROMPT, run_dir, 'reconciled-favorite', width, height,
                         checks=3, accept_success_without_target=True, deadline=deadline)
    if target[0].get('success') is True and not target[0].get('target_found'):
        save_pending({'status':'owned_unconfirmed', 'date':datetime.now(TZ).strftime('%Y-%m-%d'),
                      'submittedAt':now_text(), 'seat':target[0].get('selected_seat')})
        return settle_pending(run_dir)
    seat = canonical_seat_name(target[0].get('selected_seat'))
    allowed = {'博学楼四楼半圆弧平7号':'primary', '博学楼三楼半圆弧平7号':'backup'}
    if seat not in allowed or target[0].get('selection') != allowed[seat]:
        raise RestartFlow('恢复时目标座位身份不完整，重新进入微信核验')
    return submit_and_settle(target, run_dir, width, height, deadline)


def settle_pending(run_dir):
    global BUSINESS_DEADLINE
    if SETTLEMENT_DEADLINE is not None:
        BUSINESS_DEADLINE = SETTLEMENT_DEADLINE
    ensure_window_open(BUSINESS_DEADLINE)
    pending = pending_reservation()
    if not pending or pending.get('status') == 'settled':
        raise FlowError('没有待核实的预约提交')
    if pending.get('date') != datetime.now(TZ).strftime('%Y-%m-%d'):
        raise CrossDaySubmission('存在跨日未完成预约：停止重复循环，保留记录并要求核对；禁止新预约')
    if pending.get('noSeatRejected'):
        reconcile_submission(run_dir, pending)
        pending = pending_reservation()
    mark_stage(run_dir,'checkin_pending','已可能提交预约：只重试签到，不再选座')
    try:
        outcome = complete_wegolibrary_checkin(force=True)
    except WegoLibraryError as exc:
        if '你没有选座或没有专座' not in str(exc):
            raise
        pending = {**pending, 'noSeatRejected': True, 'lastNoSeatAt': now_text()}
        save_pending(pending)
        reconcile_submission(run_dir, pending)
        raise WegoLibraryError('页面已确认本人预约，签到接口尚未同步；保留预约，稍后再次核对签到',
                               max(15, float(getattr(exc, 'retry_after', 0) or 0))) from exc
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
        choice = favorite[0]
        allowed = {'博学楼四楼半圆弧平7号':'primary','博学楼三楼半圆弧平7号':'backup'}
        if (canonical_seat_name(choice.get('selected_seat')) not in allowed or
                allowed.get(canonical_seat_name(choice.get('selected_seat'))) != choice.get('selection') or
                choice.get('seatIdentityMismatch') or choice.get('success') is True or
                choice.get('target_found') is not True or school_binding_popup_visible(choice)):
            raise RestartFlow('提交前座位身份/分支/页面校验失败：禁止提交未经授权或不确定的目标，重新观察')
        save_pending({'status':'submission_uncertain','date':datetime.now(TZ).strftime('%Y-%m-%d'),'submittedAt':now_text(),'seat':favorite[0].get('selected_seat')})
        try:
            tap_visual(*favorite, screen_w, screen_h)
            break
        except (ActionNotSubmitted,VisionProviderError,WindowClosed) as rejected:
            # Unlike RPC timeout, this is explicit proof no gesture ran.
            path = ROOT/'pending-reservation.json'
            archive = run_dir/f'rejected-submission-{stale_attempt}.json'
            path.replace(archive)
            write_private_json(archive,{**json.loads(archive.read_text()),
                'rejectionReason':str(rejected),'rejectedAt':now_text(),
                'frameValidation':RUN_CONTEXT.get('lastFrameValidation')})
            if old:
                save_pending(old)
            if isinstance(rejected,(VisionProviderError,WindowClosed)):
                # These classes can only arise in the visual/window preflight
                # before fast_coordinate dispatches. Do not mislabel a failed
                # preflight as an uncertain booking and enter empty check-in.
                raise
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


def finish_with_budget(folder, *args, **kwargs):
    global BUSINESS_DEADLINE
    previous = BUSINESS_DEADLINE
    BUSINESS_DEADLINE = datetime.now(TZ) + timedelta(seconds=75)
    try:
        return best_effort_finish(folder, *args, **kwargs)
    except Exception as exc:
        return {'cleanupError': type(exc).__name__}
    finally:
        BUSINESS_DEADLINE = previous


def notify_review_image(path, args):
    """One requested diagnostic image, never credentials or unrelated files."""
    try:
        image = Path(path).resolve()
        if not image.is_relative_to(ROOT.resolve()) or not image.is_file():
            return {'sent': False, 'reason': 'no_review_image'}
        if not (args.notify_channel and args.notify_account and args.notify_target):
            return {'sent': False, 'reason': 'destination_missing'}
        proc = subprocess.run([OPENCLAW, 'message', 'send', '--json',
            '--channel', args.notify_channel, '--account', args.notify_account,
            '--target', args.notify_target, '--message',
            '预约点击后的页面无法可靠确认，已停止重复操作。请看截图告诉我应点击哪个按钮。',
            '--media', str(image)], text=True, capture_output=True, timeout=35)
        # Unknown send/timeout is not replayed automatically.
        if proc.returncode == 0:
            for offset, char in enumerate(proc.stdout):
                if char != '{':
                    continue
                try:
                    result, _ = json.JSONDecoder().raw_decode(proc.stdout[offset:])
                except ValueError:
                    continue
                if isinstance(result, dict) and result.get('action') == 'send':
                    sent = result.get('payload', {}).get('deliveryStatus') == 'sent' and bool(result.get('messageId'))
                    return {'sent': sent, 'accepted_by': 'wechat_api' if sent else None}
        return {'sent': False, 'reason': 'delivery_not_confirmed'}
    except Exception as exc:
        return {'sent': False, 'reason': type(exc).__name__}


def main():
    if (ROOT / 'SAFETY_HOLD').exists():
        print('自动预约已安全暂停：账号封禁及未签到事故尚待人工核实。禁止新预约。')
        return 1
    global BOOKING_OPENS, BUSINESS_DEADLINE, SETTLEMENT_DEADLINE, RESERVATION_DEADLINE
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
    RESERVATION_DEADLINE = args.until
    SETTLEMENT_DEADLINE = args.settle_until or args.until
    if args.until and SETTLEMENT_DEADLINE < args.until:
        parser.error('--settle-until must not precede --until')
    if args.until and args.not_before and args.until <= args.not_before:
        parser.error("--until must be later than --not-before")
    if SETTLEMENT_DEADLINE is not None and datetime.now(TZ) >= SETTLEMENT_DEADLINE:
        print('执行窗口已过，跳过调度器的过期重试；不操作手机、不提交预约或签到。')
        return 0
    attempts = max(1, min(3, args.max_attempts))
    started = now_text()
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f")
    base = ROOT / "artifacts" / f"checkin-{stamp}"
    base.mkdir(parents=True, exist_ok=False)
    errors = []
    display_failures = 0
    provider_unavailable = False
    recovery_history = []
    preliminary_diagnosis = None
    diagnosis_stop = threading.Event()
    diagnosis_holder = {}
    final_done = False

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
                    if RUN_CONTEXT.get('reservationRestartReady'):
                        result = resume_verified_reservation(attempt_dir, args.until)
                    else:
                        result = run_once(attempt_dir, deadline=args.until)
                ended = now_text()
                detail = {"status": "passed", "attempt": attempt, "startedAt": started,
                          "endedAt": ended, "artifacts": str(base), **result}
                diagnosis_stop.set()
                BUSINESS_DEADLINE = None
                # Mandatory final API attempt must precede cosmetic cleanup.
                final_checkin_once(base)
                final_done = True
                print("✅ 手机自动签到完成")
                print("任务：签到")
                print("App：微信 / 我去图书馆 + WegoLibrary")
                print(f"时间：{ended}")
                print(f"结果：第 {attempt} 次尝试预约与到馆签到均已确认成功")
                if args.finish_phone:
                    try:
                        finish = finish_with_budget(
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
                display_failed = isinstance(exc, DisplayGeometryChanged)
                if display_failed:
                    display_failures += 1
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
                    if (isinstance(exc, WindowClosed) and not isinstance(exc, NoReservationAfterCutoff)
                            and SETTLEMENT_DEADLINE and datetime.now(TZ) < SETTLEMENT_DEADLINE):
                        exc = FlowError('新预约窗口已关闭，提交状态待核实，转入签到补救窗口')
                if node_offline and not any('Android Node 离线' in e for e in errors[:-1]):
                    delivery = notify_wechat('⚠ 手机节点离线，预约尚未开始。正在等待手机自行重连；请检查手机网络、VPN及OpenClaw后台。截止时间保持不变。', args.notify_channel, args.notify_account, args.notify_target)
                    notice_path = base / 'node-offline-notify.json'
                    try:
                        notice_path.write_text(json.dumps({'time':now_text(), **delivery},ensure_ascii=False),encoding='utf-8')
                        os.chmod(notice_path,0o600)
                    except OSError:
                        print('离线告警送达记录无法保存；继续等待重连')
                if (not provider_failed and not display_failed and not needs_settlement and not node_offline and not isinstance(exc, (WindowClosed, RestartFlow)) and
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
                if not provider_failed and not display_failed and not needs_settlement and not node_offline and not isinstance(exc, WindowClosed) and "人工解锁" not in str(exc) and not RUN_CONTEXT.get('reservationRestartReady'):
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
                if isinstance(exc, SubmissionNeedsReview):
                    image_result = notify_review_image(RUN_CONTEXT.get('submissionReviewScreenshot', ''), args)
                    (attempt_dir/'review-image-delivery.json').write_text(json.dumps(image_result))
                    break
                if isinstance(exc, WindowClosed) or "人工解锁" in str(exc):
                    break
                if display_failed and display_failures >= 2:
                    break  # known invalid geometry is not repaired by 13 blind restarts
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
        final_result = final_checkin_once(base)
        final_done = True
        preliminary_diagnosis = diagnosis_holder.get("value")
        ended = now_text()
        if final_result.get('success') is True:
            try:
                write_run({'status':'passed', 'startedAt':started, 'endedAt':ended,
                           'artifacts':str(base), 'recoveredBy':'final_checkin', 'result':final_result})
            except Exception as record_error:
                print('最终签到已成功，日志保存失败：'+type(record_error).__name__+'；不重复预约。')
            print('✅ 最终签到接口及状态复核成功；不重新预约。')
            if args.finish_phone:
                finish_with_budget(base)
            return 0
        # Do not spend up to 90s on a new model call after the business window;
        # that delayed cleanup until cron's 1200s hard kill on 2026-10-20.
        expired = args.until is not None and datetime.now(TZ) >= args.until
        final_diagnosis = (preliminary_diagnosis or {
            'stage': RUN_CONTEXT.get('stage', 'unknown'),
            'diagnosis': '执行窗口已结束；点击后的预约状态与签到结果未形成已确认闭环。',
            'next_action': '保留提交记录和现场截图，不再新预约；核实本人预约及签到状态。',
            'continue_safe': False, 'confidence': 1,
        }) if expired else diagnose_failures(base, errors or [str(exc)], 'final')
        detail = {"status": "failed", "startedAt": started, "endedAt": ended,
                  "error": str(exc), "artifacts": str(base), "failedStage": dict(RUN_CONTEXT),
                  "preliminaryDiagnosis": preliminary_diagnosis,
                  "finalDiagnosis": final_diagnosis}
        pending = pending_reservation()
        if pending and pending.get('status') != 'settled':
            detail['status'] = 'reservation_pending_checkin'
            detail['pendingReservation'] = pending
        try:
            write_run(detail)
        except Exception as record_error:
            print('失败记录保存失败：'+type(record_error).__name__+'；仍继续通知与清理，不重复预约。')
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
            finish = finish_with_budget(base)
            if finish.get("cleanupError"):
                print(f"清理提示：{finish['cleanupError']}")
            if finish.get("screenOffError"):
                print(f"息屏提示：{finish['screenOffError']}")
        return 1
    finally:
        diagnosis_stop.set()
        if not final_done:
            final_checkin_once(base)


if __name__ == "__main__":
    raise SystemExit("Source archive: run the offline test suite instead.")
