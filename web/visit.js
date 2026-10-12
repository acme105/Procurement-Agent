// Tells the server a few things only the browser knows (screen, time zone, hardware, connection) for the visit alert.
(() => {
  try {
    const c = navigator.connection || {};
    fetch('/api/visit/beacon', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, keepalive: true,
      body: JSON.stringify({
        screen: `${screen.width}x${screen.height}`, viewport: `${innerWidth}x${innerHeight}`, dpr: devicePixelRatio || 0,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '', languages: (navigator.languages || []).join(','),
        platform: navigator.platform || '', cores: navigator.hardwareConcurrency || 0, memory_gb: navigator.deviceMemory || 0,
        touch_points: navigator.maxTouchPoints || 0, connection: [c.effectiveType, c.downlink && c.downlink + 'Mbps', c.rtt && c.rtt + 'ms'].filter(Boolean).join(' '),
        cookies: navigator.cookieEnabled, webdriver: !!navigator.webdriver,
      }),
    }).catch(() => {});
  } catch (e) {}
})();
