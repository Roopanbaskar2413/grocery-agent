(function () {
  "use strict";
  var KEY = "grocery_chat_token";
  var token = "", lastId = 0, messages = [], busy = false, polling = false, seen = {};
  var $ = function (id) { return document.getElementById(id); };

  function load() { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } }
  function save(t) { try { localStorage.setItem(KEY, t); } catch (e) { /* private mode: the token lives in memory only */ } }
  function forget() { try { localStorage.removeItem(KEY); } catch (e) {} }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "Authorization": "Bearer " + token }, opts.headers || {});
    return fetch(path, opts);
  }

  function showLogin(msg) {
    token = "";
    $("login").style.display = "flex";
    $("log").style.display = "none"; $("send").style.display = "none";
    $("err").textContent = msg || "";
  }
  function showChat() {
    $("login").style.display = "none";
    $("log").style.display = "flex"; $("send").style.display = "flex";
  }

  function fmt(iso) {
    var d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function render() {
    var log = $("log"), atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    log.textContent = "";
    var lastIdx = messages.length - 1;
    messages.forEach(function (m, i) {
      var div = document.createElement("div");
      div.className = "msg " + (m.role === "user" ? "user" : "agent");
      div.appendChild(document.createTextNode(m.text));          // text only: model replies are never parsed as HTML
      var t = document.createElement("span"); t.className = "time"; t.textContent = fmt(m.at); div.appendChild(t);
      if (m.buttons && m.buttons.length) {
        var row = document.createElement("div"); row.className = "buttons";
        m.buttons.forEach(function (b, k) {
          var btn = document.createElement("button");
          btn.type = "button"; btn.textContent = b.title;
          if (k > 0) btn.className = "secondary";
          btn.disabled = i !== lastIdx || busy;                  // only the newest message can be answered by tapping
          btn.addEventListener("click", function () { tap(b.id); });
          row.appendChild(btn);
        });
        div.appendChild(row);
      }
      log.appendChild(div);
    });
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function poll() {
    if (!token || document.visibilityState === "hidden" || polling) return;
    polling = true;
    api("/chat/messages?after=" + lastId).then(function (r) {
      if (r.status === 401) { forget(); showLogin("That token was not accepted."); return null; }
      $("banner").style.display = "none";
      return r.json();
    }).then(function (data) {
      if (!data) return;
      $("who").textContent = data.name || "";
      var added = false;
      data.messages.forEach(function (m) {
        if (seen[m.id]) return;                        // two overlapping polls must never show a message twice
        seen[m.id] = true;
        messages.push(m);
        lastId = Math.max(lastId, m.id);
        added = true;
      });
      if (added) render();
    }).catch(function () { $("banner").style.display = "block"; }).then(function () { polling = false; });
  }

  function post(path, body) {
    busy = true; render();
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) {
        if (r.status === 429) { $("banner").textContent = "Slow down a little."; $("banner").style.display = "block"; }
        else if (r.status === 401) { forget(); showLogin("That token was not accepted."); }
        busy = false; poll();
      }).catch(function () { busy = false; $("banner").style.display = "block"; });
  }
  function tap(id) { post("/chat/tap", { button_id: id }); }

  function urlBase64ToUint8Array(base64) {
    var padding = "=".repeat((4 - base64.length % 4) % 4);
    var raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  // Push is a nice-to-have on top of the 2-second poll above, not a replacement for it: any
  // failure here (no permission, no server key configured, an unsupported browser) is silent.
  function setupPush() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
    navigator.serviceWorker.register("/chat/sw.js").then(function (reg) {
      return api("/chat/vapid-public-key").then(function (r) { return r.ok ? r.json() : null; });
    }).then(function (data) {
      if (!data || !data.key) return null;
      return navigator.serviceWorker.ready.then(function (reg) {
        return reg.pushManager.getSubscription().then(function (existing) {
          if (existing) return existing;
          return Notification.requestPermission().then(function (perm) {
            if (perm !== "granted") return null;
            return reg.pushManager.subscribe({ userVisibleOnly: true,
                                              applicationServerKey: urlBase64ToUint8Array(data.key) });
          });
        });
      });
    }).then(function (sub) {
      if (!sub) return;
      var k = sub.toJSON().keys;
      return api("/chat/push-subscribe", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: sub.endpoint, p256dh: k.p256dh, auth: k.auth }) });
    }).catch(function () { /* push is optional; the poll loop still works without it */ });
  }

  $("send").addEventListener("submit", function (e) {
    e.preventDefault();
    var box = $("text"), text = box.value.trim();
    if (!text) return;
    box.value = "";
    post("/chat/send", { text: text });
  });
  $("signin").addEventListener("click", function () {
    var t = $("token").value.trim();
    if (!t) return;
    token = t; save(t); $("token").value = ""; lastId = 0; messages = []; seen = {};
    showChat(); poll(); setupPush();
  });
  document.addEventListener("visibilitychange", poll);

  token = load();
  if (token) { showChat(); poll(); setupPush(); } else { showLogin(""); }
  setInterval(poll, 2000);
})();
