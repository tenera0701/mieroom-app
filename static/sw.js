/* ミエルーム サービスワーカー：Web Push（アプリ・タブを閉じていても通知が届く） */
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; }
  catch (_) { d = { body: (e.data && e.data.text()) || '' }; }
  const title = d.title || 'ミエルーム チャット';
  const opts = {
    body: d.body || '新着メッセージがあります',
    tag: d.tag || 'mieroom-chat',
    renotify: true,
    icon: '/static/logo_notext.png',
    badge: '/static/logo_notext.png',
    vibrate: [120, 60, 120],
    data: { url: d.url || '/chat' }
  };
  e.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/chat';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
      for (const c of cs) {
        if (c.url.indexOf(url) !== -1 && 'focus' in c) return c.focus();
      }
      for (const c of cs) {
        if ('focus' in c) { if (c.navigate) c.navigate(url); return c.focus(); }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
