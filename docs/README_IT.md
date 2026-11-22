# ZAPPING — Guida italiana

**ZAPPING** è un prototipo non ufficiale per mostrare i fotogrammi dei canali IPTV nelle schede «In onda ora» di Jellyfin Web. Non modifica il lettore video di Jellyfin e non è un plugin ufficiale.

## Come funziona

**ZAPPING-JELLYFIN** (`8099`) accetta le connessioni WebSocket dei browser. Ciascun browser trasmette l’elenco completo dei propri Preferiti. Il gestore calcola l’unione dei canali richiesti, crea **un processo Python indipendente per ciascun canale** e mantiene una **coda unica di ingresso** dei JPEG. Un distributore preleva i fotogrammi e li inoltra solo alle code dei client iscritti a quel canale. Il client non richiede periodicamente ogni immagine.

**ZAPPING-PROVIDERS** (`8110`, in ascolto di norma solo su `127.0.0.1`) non conosce le sessioni Jellyfin: riceve una richiesta `/frame?name=...`, risolve la sorgente HLS dalla playlist privata, acquisisce il segmento, esegue FFmpeg per un solo JPEG e restituisce il risultato. Supporta gli HLS AES-128 standard (con chiave disponibile), non DRM. Memorizza in SQLite le sorgenti verificate da una decodifica riuscita.

**`gestore_iptv.py`** (`8100`) avvia e sorveglia le due sezioni e gestisce i comandi amministrativi.

## Ciclo di un canale

```text
Processo del canale
  → HTTP: richiede frame al provider
  → JPEG valido oppure immagine di errore
  → consegna alla coda ingresso
  → sleep indipendente (3 s di default)
  → ripete

Distributore
  → preleva il frame
  → trova i client iscritti al canale
  → aggiunge il frame alle code dei destinatari
  → invio WebSocket automatico
```

Il timeout FFmpeg di 1 secondo non garantisce che una sorgente valida produca sempre un'immagine: apertura, lettura e carico macchina possono incidere. Ogni canale è indipendente dagli altri, ma CPU e I/O restano risorse condivise.

## Installazione

Leggere [DEPLOYMENT.md](DEPLOYMENT.md). Per una configurazione locale/LAN con Jellyfin e ZAPPING sulla stessa macchina:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python src/gestore_iptv.py \
  --host 192.168.1.20 \
  --origin http://192.168.1.20:8096 \
  --playlist /percorso/privato/canali.m3u8 \
  --ffmpeg ffmpeg
```

Usare il comando FFmpeg del container se FFmpeg risiede soltanto in Jellyfin Docker. Se è già presente un'installazione funzionante, **non sostituire il servizio senza backup**: questo archivio è destinato prima di tutto al repository GitHub.

## Preferiti, miniature e stato

I Preferiti sono specifici dell'utente. Il browser può inviare per esempio **62 canali**, ma «In onda ora» può mostrare **46 schede**: ciò dipende da quali programmi la pagina Jellyfin presenta. I canali richiesti sono l'unione dei Preferiti di tutti i client connessi; un canale richiesto da due utenti viene decodificato una sola volta.

- LED **ZAPPING**: segnala la ricezione di frame dal WebSocket, non l'avanzamento di ogni decodifica.
- Menu **Riavvia**: invia un comando al gestore; endpoint da proteggere.
- `PROVIDER ERROR`: errore di sorgente/accesso (per esempio HTTP 400/404).
- `PROVIDER TIMEOUT`: attesa di acquisizione o provider scaduta.
- `DECODE ERROR`: errore o timeout FFmpeg.

## Persistenza e privacy

Il database `src/zapping_providers/state/sources.sqlite3` nasce automaticamente e registra sorgenti funzionanti. Non inserire **playlist IPTV reali, database SQLite, token, credenziali, log o backup** in GitHub. L’archivio include soltanto sorgenti del programma, documenti e immagini diagnostiche generiche.

**Sicurezza:** il codice non autentica direttamente le sessioni Jellyfin. Non pubblicare le porte `8099`, `8100`, `8110` su Internet. Proteggere anche `/admin/restart` con isolamento di rete e, se necessario, un proxy autenticato.

Per dettagli: [Architettura](ARCHITECTURE.md), [Protocollo](PROTOCOL.md), [Sicurezza](SECURITY.md).
