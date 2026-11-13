#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { dispatchBoundGesture } from './android-bound-gesture.mjs';

const home = process.env.HOME;
const dist = path.join(home, ".local/openclaw/tools/node-v24.15.0/lib/node_modules/openclaw/dist");
const clientFile = fs.readdirSync(dist)
  .filter((name) => /^client-.*\.js$/.test(name))
  .map((name) => path.join(dist, name))
  .find((file) => fs.readFileSync(file, "utf8").includes("GatewayClient as t"));
if (!clientFile) throw new Error("OpenClaw GatewayClient runtime not found");
const { t: GatewayClient } = await import(pathToFileURL(clientFile).href);

const config = JSON.parse(fs.readFileSync(path.join(home, ".openclaw/openclaw.json"), "utf8"));
const token = config?.gateway?.auth?.token;
if (!token) throw new Error("Gateway token is unavailable");

const actionName = process.argv[2];
let action;
let directCommand = null;
let directParams = null;
let boundSnapshotId = null;
const captureFrame = actionName === "capture" && process.argv.length === 3;
const nodeStatus = actionName === "status" && process.argv.length === 3;
const invokeRpc = actionName === "rpc" && process.argv.length === 6;
let rpcCommand, rpcParams, rpcTimeout;
if (invokeRpc) {
  rpcCommand = process.argv[3];
  if (!["mobile.ui.observe", "mobile.ui.act", "mobile.ui.launch", "mobile.ui.screenshot"].includes(rpcCommand)) {
    throw new Error("RPC command not allowed by Android workflow");
  }
  rpcParams = JSON.parse(process.argv[4]);
  if (!rpcParams || Array.isArray(rpcParams) || typeof rpcParams !== "object") throw new Error("Expected RPC params object");
  rpcTimeout = Number(process.argv[5]);
  if (!Number.isFinite(rpcTimeout) || rpcTimeout < 1 || rpcTimeout > 70000) throw new Error("Invalid RPC timeout");
} else if (nodeStatus) {
  // Status is read-only and must also be able to report an offline node.
} else if ((actionName === "tap-bound" || actionName === "doubletap-bound") && process.argv.length === 6) {
  boundSnapshotId = process.argv[3];
  if (!boundSnapshotId || boundSnapshotId.length > 256) throw new Error("Invalid bound snapshotId");
  action = { type: "tap", x: Number(process.argv[4]), y: Number(process.argv[5]) };
} else if (captureFrame) {
  // Observe BEFORE capture, on the same connection. Later actions must keep
  // this snapshot identity rather than minting a new identity for old pixels.
} else if (actionName === "tap" && process.argv.length === 5) {
  action = { type: "tap", x: Number(process.argv[3]), y: Number(process.argv[4]) };
} else if (actionName === "doubletap" && process.argv.length === 5) {
  action = { type: "tap", x: Number(process.argv[3]), y: Number(process.argv[4]) };
} else if (actionName === "swipe" && process.argv.length === 8) {
  action = {
    type: "swipe",
    x1: Number(process.argv[3]), y1: Number(process.argv[4]),
    x2: Number(process.argv[5]), y2: Number(process.argv[6]),
    durationMs: Number(process.argv[7]),
  };
} else if (actionName === "global" && process.argv.length === 4) {
  action = { type: "global_action", name: process.argv[3] };
} else if (actionName === "notify" && process.argv.length === 3) {
  directCommand = "system.notify";
  directParams = {
    title: "OpenClaw 自动任务",
    body: "正在唤醒手机执行已授权的自动化任务",
    priority: "timesensitive",
  };
} else if (actionName === "observe" && process.argv.length === 3) {
  directCommand = "mobile.ui.observe";
  directParams = {};
} else {
  throw new Error("usage: openclaw-android-fast-act tap x y | doubletap x y | swipe x1 y1 x2 y2 durationMs | global name | notify | observe");
}
if (action && Object.values(action).some((v) => typeof v === "number" && !Number.isFinite(v))) {
  throw new Error("coordinates must be finite numbers");
}

let readyResolve;
let readyReject;
const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
const client = new GatewayClient({
  url: "ws://127.0.0.1:18789",
  token,
  clientName: "cli",
  clientDisplayName: "Android Fast Act",
  clientVersion: "1.0.0",
  platform: "linux",
  mode: "cli",
  role: "operator",
  scopes: ["operator.admin", "operator.read", "operator.write"],
  sharedStateMode: "read-only",
  onHelloOk: () => readyResolve(),
  onConnectError: (error) => readyReject(error),
});

const timer = setTimeout(() => readyReject(new Error("Gateway connect timeout")), 12000);
client.start();
try {
  await ready;
  clearTimeout(timer);
  const listed = await client.request("node.list", {}, { timeoutMs: 10000 });
  const nodes = Array.isArray(listed?.nodes) ? listed.nodes : [];
  if (nodeStatus) {
    process.stdout.write(JSON.stringify({ ok: true, nodes: nodes.map((n) => ({
      displayName: n.displayName, connected: n.connected, commands: n.commands,
      lastSeenAtMs: n.lastSeenAtMs, lastSeenReason: n.lastSeenReason,
      lastConnectedAtMs: n.lastConnectedAtMs, approvalState: n.approvalState,
    })) }) + "\n");
  } else {
  const node = nodes.find((item) => item?.displayName === "my-android-phone");
  if (!node?.nodeId || node.connected !== true) throw new Error("my-android-phone is offline");
  const call = async (command, params, timeoutMs = 20000) => client.request("node.invoke", {
    nodeId: node.nodeId,
    command,
    params,
    idempotencyKey: crypto.randomUUID(),
    timeoutMs,
  }, { timeoutMs: timeoutMs + 10000 });
  if (invokeRpc) {
    const response = await call(rpcCommand, rpcParams, rpcTimeout);
    const payload = response?.payload ?? response;
    const ok = response?.ok !== false && !response?.error;
    process.stdout.write(JSON.stringify({ ok, payload, ...(response?.error ? {error:response.error} : {}) }) + "\n");
    if (!ok) process.exitCode = 1;
  } else if (captureFrame) {
    const observed = await call("mobile.ui.observe", {});
    const snapshot = observed?.payload ?? observed;
    if (!snapshot?.snapshotId) throw new Error("observe returned no snapshotId");
    const captured = await call("mobile.ui.screenshot", { maxDimension: 2048, quality: 88 });
    const frame = captured?.payload ?? captured;
    if (!frame?.base64 || !frame.width || !frame.height) throw new Error("Invalid screenshot response");
    process.stdout.write(JSON.stringify({ ok: true, frame, snapshotId: snapshot.snapshotId,
      capturedAt: new Date().toISOString(), package: snapshot.package }) + "\n");
  } else if (boundSnapshotId) {
    // Absolutely no re-observe/retry here: target_stale must reach the workflow.
    const output = await dispatchBoundGesture(call, actionName, boundSnapshotId, action);
    process.stdout.write(JSON.stringify(output) + "\n");
    if (!output.ok) process.exitCode = 1;
  } else {
  let acted = directCommand
    ? await call(directCommand, directParams)
    : null;
  if (!directCommand) {
    // Animated Android/WeChat surfaces can invalidate every snapshot for more
    // than a second after wake or navigation. Re-observe with bounded backoff
    // instead of exhausting all retries in a few hundred milliseconds.
    for (let attempt = 0; attempt < 24; attempt += 1) {
        const observed = await call("mobile.ui.observe", {});
        const snapshot = observed?.payload ?? observed;
        if (!snapshot?.snapshotId) throw new Error("observe returned no snapshotId");
        if (actionName === 'swipe' && snapshot.package !== 'com.tencent.mm') {
          acted = {payload:{code:'package_changed',message:'Expected WeChat before workflow scroll'}};
          break;
        }
        acted = await call("mobile.ui.act", { snapshotId: snapshot.snapshotId, action });
        const attemptResult = acted?.payload ?? acted;
        if (attemptResult?.code !== "target_stale") break;
        // A new snapshot cannot validate a coordinate from an old image.
        // Return stale to the visual workflow so it captures and locates again.
        if (actionName === "tap" || actionName === "swipe") break;
        await new Promise((resolve) => setTimeout(resolve, Math.min(500, 120 + attempt * 25)));
    }
    if (actionName === "doubletap") {
      await new Promise((resolve) => setTimeout(resolve, 90));
      for (let attempt = 0; attempt < 24; attempt += 1) {
        const observed = await call("mobile.ui.observe", {});
        const snapshot = observed?.payload ?? observed;
        if (!snapshot?.snapshotId) throw new Error("observe returned no snapshotId");
        acted = await call("mobile.ui.act", { snapshotId: snapshot.snapshotId, action });
        const attemptResult = acted?.payload ?? acted;
        if (attemptResult?.code !== "target_stale") break;
        await new Promise((resolve) => setTimeout(resolve, Math.min(500, 120 + attempt * 25)));
      }
    }
  }
  const result = acted?.payload ?? acted;
  const ok = directCommand
    ? (!result?.code || result.code === "completed")
    : result?.code === "completed";
  process.stdout.write(JSON.stringify({ ok, result }) + "\n");
  if (!ok) process.exitCode = 1;
  }
  }
} catch (error) {
  // Keep diagnostics useful without copying arbitrary SDK errors/config data.
  const message = String(error?.message ?? "");
  const known = ["SERVICE_DISABLED", "DEVICE_LOCKED", "APP_NOT_FOUND", "target_stale", "offline", "timeout", "unauthorized"]
    .find((text) => message.toLowerCase().includes(text.toLowerCase()));
  process.stdout.write(JSON.stringify({ok:false,error:{message:known === "offline" ? "Android Node 离线" :
    (known ? `Android Gateway RPC: ${known}` : "Android Gateway RPC failed")}}) + "\n");
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  await client.stopAndWait({ timeoutMs: 3000 }).catch(() => {});
}
