// Service worker for the in-app chat's push notifications. Shows whatever the server sent;
// never fetches or caches anything (no offline mode), so there is nothing here that can go stale.
self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  var title = data.title || "Grocery agent";
  event.waitUntil(self.registration.showNotification(title, { body: data.body || "", tag: "grocery-agent" }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({ type: "window" }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if ("focus" in list[i]) return list[i].focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow("/chat");
  }));
});
