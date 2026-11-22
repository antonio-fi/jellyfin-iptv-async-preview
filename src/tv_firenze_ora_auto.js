/* Experimental Jellyfin 10.11.11 Live TV / On Now previews.
 * Acquisizione indipendente su 8099; mouse = sola visualizzazione del buffer.
 * Non modifica il filtro dei preferiti, le API o il lettore Jellyfin.
 */
(() => {
  'use strict';
  if (window.__tvFirenzeOraAuto) return;
  window.__tvFirenzeOraAuto = true;

  // Configure a reverse proxy for HTTPS Jellyfin deployments.
  // The service listens on port 8099 on the same hostname by default.
  const previewBase = window.jellyfinPreviewBase || (location.protocol + '//' + location.hostname + ':8099');
  const cardSelectors = '.card, .listItem';
  const channelSelectors = '[data-channelid]';
  const state = {
    channelNames: new Map(), cards: new Map(), frameVersions: new Map(),
    displayedVersions: new Map(), pendingImages: new Set(),
    focusedId: '', hoverTimer: null, running: false,
    lastHeartbeat: 0, lastScan: 0, lastPoll: 0,
    lastNames: '', busy: false, lastFocusSent: '', namesPromise: null, lastNameAttempt: 0,
    displayedCount: 0, serverFrameCount: null, serverSamples: [],
    lastStatus: 0, ledTimer: null, led: null, ledCount: null, ledRate: null, ledDisplayed: null, ledNetwork: null, ledCache: null, ledHealth: null
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

  // Diagnostica: aggiornamenti rilevati dallo stato del server, non dal caricamento DOM.
  // Un cambiamento di versione per canale = almeno un nuovo JPEG disponibile.
  function ensureFrameIndicator() {
    const header = document.querySelector('.skinHeader');
    if (!header) return;
    let led = document.getElementById('tvf-frame-led');
    if (!led) {
      led = document.createElement('div');
      led.id = 'tvf-frame-led';
      led.setAttribute('role', 'status');
      led.title = 'Il LED lampeggia quando il server riceve nuove risposte HTTP complete dalle sorgenti IPTV. Frequenza rilevata negli ultimi 10 secondi.';
      led.style.cssText = 'display:flex;align-items:center;gap:7px;white-space:nowrap;padding:5px 10px;border-radius:15px;background:rgba(12,15,20,.78);border:1px solid rgba(255,255,255,.24);color:#fff;font:600 12px Arial,sans-serif;pointer-events:none;';
      led.innerHTML = '<span class="tvf-led-light" style="width:9px;height:9px;border-radius:50%;background:#666;display:inline-block;flex:none;transition:background .12s,box-shadow .12s;"></span><span>RISPOSTE IPTV</span><span class="tvf-led-rate">0/10s</span><span class="tvf-led-health" style="opacity:.9"></span>';
    }
    const headerRight = header.querySelector('.headerRight');
    if (headerRight) {
      if (led.parentElement !== headerRight) {
        led.style.position = 'static';
        led.style.transform = 'none';
        led.style.margin = '0 12px 0 4px';
        headerRight.prepend(led);
      }
    } else if (led.parentElement !== header) {
      if (getComputedStyle(header).position === 'static') header.style.position = 'relative';
      led.style.position = 'absolute';
      led.style.top = '8px';
      led.style.left = '50%';
      led.style.transform = 'translateX(-50%)';
      led.style.margin = '0';
      led.style.zIndex = '30';
      header.append(led);
    }
    state.led = led.querySelector('.tvf-led-light');
    state.ledCount = led.querySelector('.tvf-led-count');
    state.ledNetwork = led.querySelector('.tvf-led-network');
    state.ledCache = led.querySelector('.tvf-led-cache');
    state.ledHealth = led.querySelector('.tvf-led-health');
    state.ledRate = led.querySelector('.tvf-led-rate');
    state.ledDisplayed = led.querySelector('.tvf-led-displayed');
    led.style.display = isLiveNowPage() ? 'flex' : 'none';
  }

  // LED e frequenza: misurano esclusivamente le risposte HTTP reali
  // ricevute dal comunicatore multisessione, non JPEG o cache.
  async function pollServerStats() {
    try {
      const response = await fetch(previewBase + '/status', { mode: 'cors', cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const payload = await response.json();
      if (!Number.isSafeInteger(payload.http_responses) || payload.http_responses < 0) {
        throw new Error('Campo http_responses mancante');
      }
      if (state.ledHealth) {
        const errors = Object.keys(payload.errors || {}).length;
        state.ledHealth.textContent = !payload.active ? ' · INATTIVO' : errors ? ' · ⚠' + errors : '';
        state.ledHealth.style.color = !payload.active || errors ? '#ffcf71' : '#fff';
      }
      const now = Date.now();
      const count = payload.http_responses;
      if (state.serverFrameCount === null || count < state.serverFrameCount) {
        state.serverSamples = [{ now, count }];
      } else if (count > state.serverFrameCount) {
        if (state.led) {
          state.led.style.background = '#40e376';
          state.led.style.boxShadow = '0 0 8px 2px rgba(64,227,118,.6)';
          clearTimeout(state.ledTimer);
          state.ledTimer = setTimeout(() => {
            if (!state.led) return;
            state.led.style.background = '#666';
            state.led.style.boxShadow = 'none';
          }, 350);
        }
        state.serverSamples.push({ now, count });
      }
      state.serverFrameCount = count;
      updateFrameRate();
    } catch {
      if (state.ledRate) state.ledRate.textContent = 'offline';
      if (state.ledHealth) state.ledHealth.textContent = ' · SERVIZIO NON RAGGIUNGIBILE';
      if (state.led) {
        state.led.style.background = '#e66';
        state.led.style.boxShadow = 'none';
      }
    }
  }

  function updateFrameRate() {
    const now = Date.now();
    const samples = state.serverSamples;
    while (samples.length > 1 && samples[1].now < now - 10000) samples.shift();
    if (state.ledRate && state.serverFrameCount !== null) {
      const first = samples[0];
      const delta = first ? Math.max(0, state.serverFrameCount - first.count) : 0;
      state.ledRate.textContent = String(delta) + '/10s';
    }
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
    if (!data || data.card !== card || !data.badge.isConnected) {
      const badge = document.createElement('div');
      badge.className = 'tvf-ora-auto-name';
      badge.style.cssText = 'position:absolute;top:0;left:0;z-index:12;max-width:92%;box-sizing:border-box;padding:4px 7px;border-radius:0 0 6px 0;background:rgba(10,12,18,.82);color:white;font:700 12px Arial,sans-serif;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;pointer-events:none;';
      badge.textContent = state.channelNames.get(id) || 'Canale TV';
      host.append(badge);
      data = { card, host, badge, image: null };
      state.cards.set(id, data);
      state.displayedVersions.delete(id);
    } else {
      data.badge.textContent = state.channelNames.get(id) || 'Canale TV';
    }
  }

  function showFrame(id, version) {
    const data = state.cards.get(id);
    const name = state.channelNames.get(id);
    if (!data?.host?.isConnected || !name || state.pendingImages.has(id)) return;
    if (state.displayedVersions.get(id) === version) return;
    state.pendingImages.add(id);
    const image = new Image();
    image.onload = () => {
      state.pendingImages.delete(id);
      const latest = state.cards.get(id);
      if (!latest || latest !== data || !latest.host.isConnected || !isLiveNowPage()) return;
      if (!latest.image) {
        const overlay = document.createElement('div');
        overlay.className = 'tvf-ora-auto-frame';
        overlay.style.cssText = 'position:absolute;inset:0;z-index:6;pointer-events:none;background:#08090c;display:flex;align-items:center;justify-content:center;overflow:hidden;';
        const displayed = document.createElement('img');
        displayed.alt = 'Fotogramma della diretta';
        displayed.style.cssText = 'width:100%;height:100%;object-fit:contain;display:block;';
        overlay.append(displayed);
        latest.host.insertBefore(overlay, latest.badge);
        latest.image = displayed;
      }
      latest.image.src = image.src;
      if (state.displayedVersions.get(id) !== version) {
        state.displayedCount++;

      }
      state.displayedVersions.set(id, version);
    };
    image.onerror = () => state.pendingImages.delete(id);
    image.src = previewBase + '/frame?name=' + encodeURIComponent(name) + '&v=' + version;
  }

  // La richiesta dei nomi NON deve bloccare la prima scansione del DOM,
  // le statistiche o il caricamento della pagina di Jellyfin.
  function refreshChannelNames() {
    if (!isLiveNowPage() || state.namesPromise || !window.ApiClient?.getLiveTvChannels) return;
    if (Date.now() - state.lastNameAttempt < 10000) return;
    state.lastNameAttempt = Date.now();
    state.namesPromise = Promise.resolve()
      .then(() => window.ApiClient.getLiveTvChannels({ IsFavorite: true, Limit: 1000 }))
      .then(result => {
        if (!isLiveNowPage()) return;
        for (const item of result.Items || []) {
          if (item.Id && item.Name) state.channelNames.set(item.Id.toLowerCase(), item.Name);
        }
        // Inizia una nuova sincronizzazione, anche se i riquadri erano
        // già stati creati quando i nomi non erano ancora disponibili.
        state.lastHeartbeat = 0;
        state.lastPoll = 0;
        for (const [id, data] of state.cards) {
          if (data.badge.isConnected) {
            data.badge.textContent = state.channelNames.get(id) || 'Canale TV';
          }
        }
        tick();
      })
      .catch(error => console.warn('[Jellyfin Async Preview] Nomi canali non disponibili:', error))
      .finally(() => { state.namesPromise = null; });
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
    for (const id of state.cards.keys()) {
      if (!found.has(id)) {
        state.cards.delete(id);
        state.displayedVersions.delete(id);
      }
    }
  }

  async function heartbeat() {
    const names = [...state.cards.keys()]
      .map(id => state.channelNames.get(id)).filter(Boolean);
    if (!names.length) return;
    try {
      const response = await fetch(previewBase + '/channels', {
        method: 'POST', mode: 'cors', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ names })
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
    } catch (error) {
      console.warn('[Jellyfin Async Preview] Servizio 8099:', error.message);
    }
  }

  async function pollFrames() {
    try {
      const response = await fetch(previewBase + '/state', { mode: 'cors', cache: 'no-store' });
      if (!response.ok) return;
      const payload = await response.json();

      for (const [name, version] of Object.entries(payload.frames || {})) {
        for (const [id, knownName] of state.channelNames) {
          if (knownName !== name || !state.cards.has(id)) continue;
          state.frameVersions.set(id, version);
          // Prima immagine automatica. Aggiornamenti successivi SOLO con mouse sopra.
          if (!state.displayedVersions.has(id) || state.focusedId === id) showFrame(id, version);
        }
      }
    } catch { /* Mantieni l'immagine originale se il servizio non risponde. */ }
  }

  async function notifyFocus(name) {
    if (state.lastFocusSent === name) return;
    state.lastFocusSent = name;
    try {
      await fetch(previewBase + '/focus', {
        method: 'POST', mode: 'cors', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
      });
    } catch { /* Le miniature gia acquisite rimangono visibili. */ }
  }

  function selectCard(id) {
    clearTimeout(state.hoverTimer);
    if (!id || !state.cards.has(id)) {
      state.focusedId = '';
      notifyFocus('');
      return;
    }
    state.hoverTimer = setTimeout(() => {
      state.focusedId = id;
      notifyFocus(state.channelNames.get(id) || '');
      const version = state.frameVersions.get(id);
      if (version) showFrame(id, version);
    }, 500);
  }

  document.addEventListener('pointerover', event => {
    if (event.pointerType === 'touch' || !isLiveNowPage()) return;
    const card = event.target instanceof Element ? event.target.closest(cardSelectors) : null;
    if (!card) return;
    const id = getChannelId(card);
    if (id && (state.focusedId !== id)) selectCard(id);
  }, { capture: true, passive: true });
  document.addEventListener('pointerout', event => {
    if (event.pointerType === 'touch') return;
    const card = event.target instanceof Element ? event.target.closest(cardSelectors) : null;
    if (!card || card.contains(event.relatedTarget)) return;
    const id = getChannelId(card);
    if (id && (state.focusedId === id || state.hoverTimer)) selectCard('');
  }, { capture: true, passive: true });

  function tick() {
    if (state.busy) return;
    state.busy = true;
    try {
      ensureFrameIndicator();
      updateFrameRate();
      if (!isLiveNowPage() || document.hidden) {
        state.running = false;
        state.focusedId = '';
        clearTimeout(state.hoverTimer);
        if (state.lastFocusSent) notifyFocus('');
        state.cards.clear();
        return;
      }
      if (!state.running) {
        state.running = true;
        state.lastHeartbeat = 0;
        state.lastPoll = 0;
        state.lastStatus = 0;
      }
      // Prima individua i riquadri già presenti: niente await di rete.
      scanCards();
      if (!state.channelNames.size) refreshChannelNames();
      // Le tre operazioni sono indipendenti: una risposta lenta non blocca
      // né la scansione successiva né l'aggiornamento delle altre due.
      if (Date.now() - state.lastHeartbeat > 5000) {
        state.lastHeartbeat = Date.now();
        void heartbeat();
      }
      if (Date.now() - state.lastStatus > 1000) {
        state.lastStatus = Date.now();
        void pollServerStats();
      }
      if (Date.now() - state.lastPoll > 1200) {
        state.lastPoll = Date.now();
        void pollFrames();
      }
    } finally { state.busy = false; }
  }

  window.addEventListener('hashchange', () => {
    state.running = false;
    state.focusedId = '';
    if (state.lastFocusSent) notifyFocus('');
    state.lastHeartbeat = 0;
    state.lastPoll = 0;
    state.lastStatus = 0;
  });
  // Il primo caricamento SPA puo arrivare dopo il primo tick.
  // L'osservatore attiva la scansione appena compaiono le schede.
  let scanTimer = null;
  new MutationObserver(() => {
    if (scanTimer || !isLiveNowPage()) return;
    scanTimer = setTimeout(() => { scanTimer = null; tick(); }, 220);
  }).observe(document.body, { childList: true, subtree: true });
  setInterval(tick, 800);
  tick();
})();
