// Vibeclo relay: a blind mailman between one Mac and its paired phones/browsers.
// Everything it forwards is end-to-end encrypted (X25519 + AES-GCM); it never has a key, stores no messages, and
// keeps only token hashes (who may join a room). One Durable Object per Mac ("room").
//
// Connect: wss://HOST/room/<roomId>?role=mac|dev&token=<token>
//  - mac: the first mac to connect claims the room (its token hash is kept); later only that token.
//  - dev: token must be a device token the mac allowed, or a one-time pairing code (5 min).
// Mac → relay control (JSON text): {op:"allow"|"revoke", hash} · {op:"pair", hash, exp} · {to, data}
// Dev → relay: {data} (forwarded to the mac as {from, data}) · pairing: {pub, data}
// Relay → dev: {op:"presence", mac:bool} · {data}
// Push (Mac → relay → APNs): {op:"push", token, env, kind: done|input|error, sid} · {op:"live", token, env, phase: working|done|error}.
// The relay writes the text from a fixed list — a Mac can't send content through Apple — and rate-limits each room.

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const m = url.pathname.match(/^\/room\/([A-Za-z0-9_-]{16,64})$/);
    if (!m) return new Response("CLO 2 relay", { status: url.pathname === "/" ? 200 : 404 });
    if (req.headers.get("Upgrade") !== "websocket") return new Response("websocket only", { status: 426 });
    return env.ROOM.get(env.ROOM.idFromName(m[1])).fetch(req);
  },
};

const sha = async (s) => {
  const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return btoa(String.fromCharCode(...new Uint8Array(h))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

const ALERTS = { done: "Claude is done", input: "Claude is waiting for you", error: "Claude ran into an error" };
const PUSH_PER_HOUR = 300;
// Abuse caps per room (one Mac): far above real use (a file through the relay moves ~4 MB/min), low enough that the
// relay can't be used as someone's free pipe. Counted in memory per minute (a hibernated room starts fresh — fine).
const ROOM_BYTES_PER_MIN = 64_000_000, ROOM_MSGS_PER_MIN = 6000, MAX_FRAME = 30_000_000, MAX_DEVICE_SOCKETS = 32;
let jwt = { value: "", at: 0 };

async function apnsJwt(env) {
  if (jwt.value && Date.now() - jwt.at < 40 * 60_000) return jwt.value;
  const der = Uint8Array.from(atob(env.APNS_KEY.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "")), (c) => c.charCodeAt(0));
  const key = await crypto.subtle.importKey("pkcs8", der, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const b64u = (x) => btoa(typeof x === "string" ? x : String.fromCharCode(...new Uint8Array(x))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const head = b64u(JSON.stringify({ alg: "ES256", kid: env.APNS_KEY_ID }));
  const claims = b64u(JSON.stringify({ iss: env.APNS_TEAM, iat: Math.floor(Date.now() / 1000) }));
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, new TextEncoder().encode(head + "." + claims));
  jwt = { value: `${head}.${claims}.${b64u(sig)}`, at: Date.now() };
  return jwt.value;
}

export class Room {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; this.win = { m: 0, b: 0, n: 0, told: false }; }

  // true while this room is within its per-minute budget; the first frame over it tells the sender once.
  budget(ws, bytes) {
    const minute = Math.floor(Date.now() / 60_000), w = this.win;
    if (w.m !== minute) { w.m = minute; w.b = 0; w.n = 0; w.told = false; }
    w.b += bytes; w.n++;
    if (w.b <= ROOM_BYTES_PER_MIN && w.n <= ROOM_MSGS_PER_MIN) return true;
    if (!w.told) { w.told = true; try { ws.send(JSON.stringify({ op: "throttled", retryIn: 60 - (Date.now() / 1000) % 60 | 0 })); } catch {} }
    return false;
  }

  // Mac asked for a push: build it here (fixed texts), send to APNs, tell the Mac how it went (stale tokens get dropped).
  async push(m, mac) {
    const s = this.ctx.storage, hour = Math.floor(Date.now() / 3_600_000);
    if (!/^[0-9a-f]{64,200}$/.test(m.token || "") || !this.env.APNS_KEY) return;
    const rl = (await s.get("rl")) || { h: hour, n: 0 };
    if (rl.h !== hour) { rl.h = hour; rl.n = 0; }
    if (rl.n >= PUSH_PER_HOUR) return mac.send(JSON.stringify({ op: "pushResult", token: m.token, status: 429 }));
    rl.n++; await s.put("rl", rl);
    let payload, type, topic;
    const now = Math.floor(Date.now() / 1000);
    if (m.op === "push") {
      if (!ALERTS[m.kind]) return;
      payload = { aps: { alert: { title: "Vibeclo", body: ALERTS[m.kind] }, sound: "default" } };
      if (/^[0-9a-f-]{36}$/.test(m.sid || "")) payload.sessionId = m.sid;   // a random id: lets the tap open the session
      type = "alert"; topic = "com.leo.clo2";
    } else {
      const working = m.phase === "working";
      const aps = { timestamp: now, event: working ? "update" : "end",
                    "content-state": { activity: working ? "Working…" : m.phase === "error" ? "Error" : "Done", working, title: "" } };
      if (!working) aps["dismissal-date"] = now + 15 * 60;
      payload = { aps }; type = "liveactivity"; topic = "com.leo.clo2.push-type.liveactivity";
    }
    const host = m.env === "sandbox" ? "https://api.sandbox.push.apple.com" : "https://api.push.apple.com";
    try {
      const r = await fetch(`${host}/3/device/${m.token}`, { method: "POST", body: JSON.stringify(payload), headers: {
        authorization: "bearer " + await apnsJwt(this.env), "apns-topic": topic, "apns-push-type": type, "apns-priority": "10" } });
      const reason = r.ok ? "" : ((await r.json().catch(() => ({}))).reason || "");
      mac.send(JSON.stringify({ op: "pushResult", token: m.token, status: r.status, reason }));
    } catch (e) { mac.send(JSON.stringify({ op: "pushResult", token: m.token, status: 0, reason: String(e).slice(0, 100) })); }
  }

  async fetch(req) {
    const url = new URL(req.url), role = url.searchParams.get("role"), token = url.searchParams.get("token") || "";
    if (token.length < 16) return new Response("bad token", { status: 403 });
    const hash = await sha(token), s = this.ctx.storage;
    let tag;
    if (role === "mac") {
      const owner = await s.get("mac");
      if (owner && owner !== hash) return new Response("room taken", { status: 403 });
      if (!owner) await s.put("mac", hash);
      for (const old of this.ctx.getWebSockets("mac")) old.close(4000, "replaced");
      tag = "mac";
    } else if (role === "dev") {
      const devs = (await s.get("devs")) || [], pairs = ((await s.get("pairs")) || []).filter((p) => p.exp > Date.now());
      if (this.ctx.getWebSockets().filter((w) => this.tag(w) !== "mac").length >= MAX_DEVICE_SOCKETS)
        return new Response("too many connections", { status: 429 });
      if (devs.includes(hash)) tag = "d:" + hash;
      else if (pairs.some((p) => p.hash === hash)) tag = "p:" + hash;
      else return new Response("not paired", { status: 403 });
    } else return new Response("bad role", { status: 400 });

    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server, [tag]);
    if (tag === "mac") this.presence(true);
    else server.send(JSON.stringify({ op: "presence", mac: !!this.liveMac() }));
    return new Response(null, { status: 101, webSocket: client });
  }

  tag(ws) { return this.ctx.getTags(ws)[0]; }

  presence(on) {
    const msg = JSON.stringify({ op: "presence", mac: on });
    for (const ws of this.ctx.getWebSockets()) if (this.tag(ws) !== "mac") try { ws.send(msg); } catch {}
  }

  async webSocketMessage(ws, raw) {
    let m; try { m = JSON.parse(raw); } catch { return; }
    const tag = this.tag(ws), s = this.ctx.storage;
    if (tag === "mac") {
      // Heartbeat: answered on the socket frames are actually routed to — if that isn't the sender, the bridge
      // hears nothing back and reconnects (it went deaf before: presence "online", every relayed frame lost).
      if (m.op === "echo") { const live = this.liveMac(); if (live) live.send(JSON.stringify({ op: "echo", n: m.n })); return; }
      if (m.op === "allow" || m.op === "revoke") {
        const devs = new Set((await s.get("devs")) || []);
        m.op === "allow" ? devs.add(m.hash) : devs.delete(m.hash);
        await s.put("devs", [...devs]);
        if (m.op === "revoke") for (const d of this.ctx.getWebSockets("d:" + m.hash)) d.close(4001, "revoked");
      } else if (m.op === "pair") {
        const pairs = ((await s.get("pairs")) || []).filter((p) => p.exp > Date.now() && p.hash !== m.hash);
        if (m.exp > Date.now()) pairs.push({ hash: m.hash, exp: Math.min(m.exp, Date.now() + 10 * 60_000) });
        await s.put("pairs", pairs);
        if (!(m.exp > Date.now())) for (const d of this.ctx.getWebSockets("p:" + m.hash)) d.close(4002, "pairing closed");
      } else if (m.op === "reset") {   // new Mac identity: forget every device
        await s.put("devs", []); await s.put("pairs", []);
        for (const d of this.ctx.getWebSockets()) if (this.tag(d) !== "mac") d.close(4001, "reset");
      } else if (m.op === "push" || m.op === "live") {
        await this.push(m, ws);
      } else if (m.to && typeof m.data === "string" && m.data.length < MAX_FRAME) {
        if (!this.budget(ws, m.data.length)) return;
        for (const d of this.ctx.getWebSockets(m.to)) try { d.send(JSON.stringify({ data: m.data })); } catch {}
      }
      return;
    }
    // A device (or a phone mid-pairing): everything goes to the mac, tagged with who sent it.
    const mac = this.liveMac();
    if (!mac) return ws.send(JSON.stringify({ op: "presence", mac: false }));
    if (typeof m.data === "string" && m.data.length < MAX_FRAME && this.budget(ws, m.data.length)) mac.send(JSON.stringify({ from: tag, pub: m.pub, data: m.data }));
  }

  // A replaced mac socket can sit in CLOSING for a long time (its peer vanished without a close frame) and is still listed:
  // always use the newest one that's actually open, or frames go to a dead socket while presence says "online".
  liveMac() { return this.ctx.getWebSockets("mac").filter((w) => w.readyState === 1).pop(); }

  webSocketClose(ws) { if (this.tag(ws) === "mac" && !this.ctx.getWebSockets("mac").some((w) => w !== ws && w.readyState === 1)) this.presence(false); }
  webSocketError(ws) { this.webSocketClose(ws); }
}
