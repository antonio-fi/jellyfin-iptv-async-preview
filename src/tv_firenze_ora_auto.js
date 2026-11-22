/* ZAPPING PUSH: il client invia i Preferiti all'apertura del WebSocket.
 * I fotogrammi JPEG vengono ricevuti spontaneamente, senza /state o /frame.
 * Non modifica i Preferiti, la playlist o il lettore Jellyfin.
 */
(() => {
  'use strict';
  if (window.__tvFirenzeOraAuto) return;
  window.__tvFirenzeOraAuto = true;

  // Override these globals before loading the script for non-default/proxy deployments.
  const managerBase = window.jellyfinZappingManagerBase ||
    (location.protocol + '//' + location.hostname + ':8100');
  const previewSocket = window.jellyfinZappingSocket ||
    ((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.hostname + ':8099/ws');
  const clientId = (window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + '-' + Math.random().toString(36).slice(2));
  const cardSelectors = '.card, .listItem';
  const channelSelectors = '[data-channelid]';
  const state = {
    channelNames: new Map(), cards: new Map(), favoriteNames: new Set(),
    latestFrames: new Map(),
    socket: null, socketNames: '', namesReady: false,
    namesPromise: null, lastNameAttempt: 0, lastReconnect: 0,
    led: null, ledTimer: null, running: false
  };

  function isLiveNowPage() {
    const route = location.hash || '';
    if (/^#\/list\?/i.test(route)) {
      const query = new URLSearchParams(route.split('?')[1] || '');
      return /^Programs$/i.test(query.get('type') || '') &&
        /^true$/i.test(query.get('IsAiring') || '');
    }
    if (!/^#\/livetv(?:\.html)?(?:\?|$)/i.test(route)) return false;
    const selected = document.querySelector('[role="tab"][aria-selected="true"], .emby-tab-button-active');
    const label = (selected?.textContent || '').trim().toLowerCase();
    if (/guida|guide|canali|channels/.test(label)) return false;
    if (/in onda|programmi|on now|programs/.test(label)) return true;
    const match = route.match(/[?&]tab=(\d+)/i);
    return !match || Number(match[1]) === 0;
  }

  function setLed(color) {
    if (state.led) {
      state.led.style.background = color;
      state.led.style.boxShadow = 'none';
    }
  }

  function blink() {
    setLed('#e5e5e5');
    clearTimeout(state.ledTimer);
    state.ledTimer = setTimeout(() => setLed('#777'), 260);
  }

  function ensureFrameIndicator() {
    const header = document.querySelector('.skinHeader');
    if (!header) return;
    let led = document.getElementById('tvf-frame-led');
    if (!led) {
      led = document.createElement('div');
      led.id = 'tvf-frame-led';
      led.setAttribute('role', 'status');
      led.title = 'ZAPPING: fotogrammi ricevuti e comandi';
      led.style.cssText = 'display:flex;align-items:center;gap:5px;white-space:nowrap;padding:3px 7px;border-radius:12px;background:rgba(12,15,20,.78);border:1px solid rgba(255,255,255,.18);color:#fff;font:500 11px Arial,sans-serif;pointer-events:auto;';
      led.innerHTML = '<span class="tvf-led-light" style="width:6px;height:6px;border-radius:50%;background:#777;display:inline-block;flex:none;"></span><span>ZAPPING</span>';
    }
    const headerRight = header.querySelector('.headerRight');
    if (headerRight) {
      if (led.parentElement !== headerRight) {
        led.style.position = 'static';
        led.style.margin = '0 8px 0 4px';
        headerRight.prepend(led);
      }
    } else if (led.parentElement !== header) {
      if (getComputedStyle(header).position === 'static') header.style.position = 'relative';
      led.style.position = 'absolute';
      led.style.top = '8px';
      led.style.left = '50%';
      led.style.transform = 'translateX(-50%)';
      header.append(led);
    }
    state.led = led.querySelector('.tvf-led-light');
    led.style.display = isLiveNowPage() ? 'flex' : 'none';
  }

  function ensureServerMenu() {
    const led = document.getElementById('tvf-frame-led');
    if (!led || document.getElementById('tvf-led-controls')) return;
    const button = document.createElement('button');
    button.id = 'tvf-led-controls';
    button.type = 'button';
    button.textContent = '▾';
    button.title = 'Comandi ZAPPING';
    button.style.cssText = 'border:0;background:transparent;color:white;font:700 12px Arial;cursor:pointer;padding:0 2px 0 3px;line-height:1;';
    const panel = document.createElement('div');
    panel.id = 'tvf-led-panel';
    panel.style.cssText = 'display:none;position:fixed;z-index:2147483647;background:#20242b;border:1px solid #777;border-radius:6px;padding:6px;min-width:155px;box-shadow:0 5px 16px #0008;';
    document.body.appendChild(panel);
    const positionPanel = () => {
      const rect = button.getBoundingClientRect();
      const width = Math.max(170, panel.offsetWidth);
      panel.style.left = Math.max(8, Math.min(window.innerWidth - width - 8, rect.right - width)) + 'px';
      panel.style.top = Math.min(window.innerHeight - panel.offsetHeight - 8, rect.bottom + 5) + 'px';
    };
    const action = document.createElement('button');
    action.type = 'button';
    action.textContent = '↻ Riavvia';
    action.style.cssText = 'display:block;width:100%;text-align:left;padding:9px;background:transparent;color:white;border:0;cursor:pointer;';
    action.addEventListener('click', async event => {
      event.stopPropagation();
      panel.style.display = 'none';
      action.disabled = true;
      try {
        const response = await fetch(managerBase + '/admin/restart', {
          method: 'POST', mode: 'cors', cache: 'no-store',
          headers: {'Content-Type': 'application/json'}, body: '{}'
        });
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.error || ('HTTP ' + response.status));
        }
        disconnectClient();
        state.lastReconnect = 0;
      } catch (error) {
        alert('Comando ZAPPING non riuscito: ' + error.message);
      } finally {
        action.disabled = false;
      }
    });
    panel.appendChild(action);
    button.addEventListener('click', event => {
      event.stopPropagation();
      const opening = panel.style.display === 'none';
      panel.style.display = opening ? 'block' : 'none';
      if (opening) positionPanel();
    });
    document.addEventListener('click', event => {
      if (!led.contains(event.target) && !panel.contains(event.target)) panel.style.display = 'none';
    });
    window.addEventListener('resize', () => {
      if (panel.style.display !== 'none') positionPanel();
    });
    led.appendChild(button);
  }

  function getChannelId(card) {
    const element = card.matches(channelSelectors) ? card :
      (card.querySelector(channelSelectors) || card.closest(channelSelectors));
    const id = element?.getAttribute('data-channelid') || '';
    return /^[a-f0-9]{32}$/i.test(id) ? id.toLowerCase() : '';
  }

  function imageHost(card) {
    return card.querySelector('.cardImageContainer') ||
      card.querySelector('.cardImage') ||
      card.querySelector('.cardPadder') ||
      card.querySelector('img')?.parentElement || null;
  }

  function ensureCard(card, id) {
    const host = imageHost(card);
    if (!host) return;
    if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
    let data = state.cards.get(id);
    if (!data || data.card !== card || data.host !== host || !data.badge.isConnected) {
      // Jellyfin può sostituire l'intera scheda: il frame rimane nella memoria
      // per canale e viene ridisegnato subito, senza attendere un nuovo JPEG.
      if (data?.objectUrl) URL.revokeObjectURL(data.objectUrl);
      const badge = document.createElement('div');
      badge.className = 'tvf-ora-auto-name';
      badge.style.cssText = 'position:absolute;top:0;left:0;z-index:12;max-width:92%;box-sizing:border-box;padding:4px 7px;border-radius:0 0 6px 0;background:rgba(10,12,18,.82);color:white;font:700 12px Arial,sans-serif;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;pointer-events:none;';
      badge.textContent = state.channelNames.get(id) || 'Canale TV';
      host.append(badge);
      data = {card, host, badge, image: null, objectUrl: null};
      state.cards.set(id, data);
    } else {
      data.badge.textContent = state.channelNames.get(id) || 'Canale TV';
      // Può essere sostituito soltanto il contenuto del contenitore immagine.
      if (data.image && !data.image.isConnected) {
        if (data.objectUrl) URL.revokeObjectURL(data.objectUrl);
        data.image = null;
        data.objectUrl = null;
      }
    }
    const name = state.channelNames.get(id);
    const latestFrame = state.latestFrames.get(name);
    if (latestFrame && !data.image) drawFrame(data, latestFrame);
  }

  function scanCards() {
    const found = new Map();
    for (const card of document.querySelectorAll(cardSelectors)) {
      const id = getChannelId(card);
      if (!id || !card.isConnected) continue;
      const style = getComputedStyle(card);
      if (style.display === 'none' || style.visibility === 'hidden' || !card.getClientRects().length) continue;
      found.set(id, card);
      ensureCard(card, id);
    }
    for (const [id, data] of state.cards) {
      if (!found.has(id)) {
        if (data.objectUrl) URL.revokeObjectURL(data.objectUrl);
        state.cards.delete(id);
      }
    }
  }

  function drawFrame(data, jpeg) {
    if (!data.host.isConnected) return;
    if (!data.image) {
      const overlay = document.createElement('div');
      overlay.className = 'tvf-ora-auto-frame';
      overlay.style.cssText = 'position:absolute;inset:0;z-index:6;pointer-events:none;background:#08090c;display:flex;align-items:center;justify-content:center;overflow:hidden;';
      const image = document.createElement('img');
      image.alt = 'Fotogramma della diretta';
      image.style.cssText = 'width:100%;height:100%;object-fit:contain;display:block;';
      overlay.append(image);
      data.host.insertBefore(overlay, data.badge);
      data.image = image;
    }
    const objectUrl = URL.createObjectURL(new Blob([jpeg], {type: 'image/jpeg'}));
    const oldUrl = data.objectUrl;
    data.objectUrl = objectUrl;
    data.image.onload = data.image.onerror = () => {
      if (oldUrl) URL.revokeObjectURL(oldUrl);
    };
    data.image.src = objectUrl;
  }

  function showFrame(name, jpeg) {
    // Un solo ultimo frame per canale: serve a ripristinare le schede Jellyfin
    // ricreate dinamicamente o arrivate dopo la trasmissione WebSocket.
    state.latestFrames.set(name, Uint8Array.from(jpeg));
    for (const [id, data] of state.cards) {
      if (state.channelNames.get(id) !== name || !data.host.isConnected) continue;
      drawFrame(data, jpeg);
    }
  }

  function subscribedNames() {
    // Il client registra la lista COMPLETA dei Preferiti Jellyfin.
    // Non dipende dalle schede visibili: i ridisegni di Jellyfin non devono
    // cancellare temporaneamente le sottoscrizioni e fermare i processi canale.
    return JSON.stringify([...state.favoriteNames].sort());
  }

  function sendSubscription() {
    if (!state.socket || state.socket.readyState !== WebSocket.OPEN || !state.namesReady) return;
    const names = subscribedNames();
    if (state.socketNames === names) return;
    state.socket.send(JSON.stringify({type: 'subscribe', client_id: clientId, names: JSON.parse(names)}));
    state.socketNames = names;
  }

  function disconnectClient() {
    const socket = state.socket;
    state.socket = null;
    state.socketNames = '';
    if (socket && socket.readyState !== WebSocket.CLOSED) socket.close();
    clearTimeout(state.ledTimer);
    setLed('#777');
  }

  function connectClient() {
    if (state.socket || !state.namesReady || !isLiveNowPage() || document.hidden) return;
    if (Date.now() - state.lastReconnect < 2500) return;
    state.lastReconnect = Date.now();
    const socket = new WebSocket(previewSocket);
    socket.binaryType = 'arraybuffer';
    state.socket = socket;
    socket.onopen = () => {
      if (state.socket !== socket) return;
      setLed('#777');
      sendSubscription();
    };
    socket.onmessage = event => {
      if (state.socket !== socket || !isLiveNowPage() || document.hidden) return;
      if (typeof event.data === 'string') return;
      const bytes = new Uint8Array(event.data);
      const terminator = bytes.indexOf(0);
      if (terminator < 1 || terminator > 300) return;
      const name = new TextDecoder().decode(bytes.subarray(0, terminator));
      if (!state.favoriteNames.has(name)) return;
      showFrame(name, bytes.subarray(terminator + 1));
      blink();
    };
    socket.onerror = () => setLed('#a85b5b');
    socket.onclose = () => {
      clearTimeout(state.ledTimer);
      if (state.socket === socket) {
        state.socket = null;
        state.socketNames = '';
        setLed('#a85b5b');
      }
    };
  }

  function refreshChannelNames() {
    if (!isLiveNowPage() || state.namesPromise || !window.ApiClient?.getLiveTvChannels) return;
    if (Date.now() - state.lastNameAttempt < 30000) return;
    state.lastNameAttempt = Date.now();
    state.namesPromise = Promise.resolve()
      .then(() => window.ApiClient.getLiveTvChannels({IsFavorite: true, Limit: 1000}))
      .then(result => {
        if (!isLiveNowPage()) return;
        const channelNames = new Map();
        const favoriteNames = new Set();
        for (const item of result.Items || []) {
          if (item.Id && item.Name) {
            channelNames.set(item.Id.toLowerCase(), item.Name);
            favoriteNames.add(item.Name);
          }
        }
        state.channelNames = channelNames;
        state.favoriteNames = favoriteNames;
        state.namesReady = true;
        for (const [id, data] of state.cards) {
          if (data.badge.isConnected) {
            data.badge.textContent = channelNames.get(id) || 'Canale TV';
            const latestFrame = state.latestFrames.get(channelNames.get(id));
            if (latestFrame && !data.image) drawFrame(data, latestFrame);
          }
        }
        sendSubscription();
        connectClient();
      })
      .catch(error => console.warn('[ZAPPING] Preferiti Jellyfin:', error))
      .finally(() => { state.namesPromise = null; });
  }

  function tick() {
    ensureFrameIndicator();
    ensureServerMenu();
    if (!isLiveNowPage() || document.hidden) {
      if (state.running) disconnectClient();
      state.running = false;
      return;
    }
    state.running = true;
    scanCards();
    sendSubscription();
    refreshChannelNames();
    connectClient();
  }

  window.addEventListener('pagehide', disconnectClient);
  window.addEventListener('hashchange', () => {
    disconnectClient();
    state.running = false;
    state.namesReady = false;
    state.lastNameAttempt = 0;
  });
  let scanTimer = null;
  new MutationObserver(() => {
    if (scanTimer || !isLiveNowPage()) return;
    scanTimer = setTimeout(() => { scanTimer = null; tick(); }, 220);
  }).observe(document.body, {childList: true, subtree: true});
  setInterval(tick, 800);
  tick();
})();
