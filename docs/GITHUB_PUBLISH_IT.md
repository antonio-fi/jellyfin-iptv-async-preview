# Aggiornare il repository GitHub esistente

Questo ZIP è **un archivio dei sorgenti**: non è necessario (né opportuno) mettere online la directory di produzione di `auxqnap`.

## Prima di pubblicare

1. Eseguire il backup o creare un branch del repository esistente.
2. Estrarre l'archivio in una **copia locale del repository GitHub** (la radice dell'archivio contiene direttamente `README.md`, `src/`, `docs/`, `tests/` ecc.).
3. Verificare che i vecchi `src/server_auto.py` e `src/tv_firenze_ora_auto.js` siano stati sostituiti da quelli aggiornati e che siano presenti tutte le nuove directory.
4. Conservare l'eventuale storico del repository (directory `.git`) **senza includerla nel nuovo ZIP**.
5. Rimuovere dal repository i vecchi artefatti non più usati; non cancellare file personalizzati senza verificarli.
6. Controllare che non vengano aggiunti playlist reali, file SQLite, cache, log, credenziali o backup.

## Pubblicazione con git

Dalla copia locale del repository esistente:

```bash
git status --short
git diff --stat
git add -A
git diff --cached --name-only
git diff --cached --stat
# Controllare i nomi dei file e il contenuto prima di continuare.
git commit -m "Update ZAPPING push architecture and documentation"
git push
```

`git add -A` può anche includere eliminazioni e file non desiderati: **controllare lo staging**; se contiene dati privati, annullare lo staging prima del commit. Il comando `git push` usa il remoto e il branch già configurati nel repository locale; non inventare un nuovo remoto.

## Pubblicazione via sito GitHub

Caricare i file *estratti* dall'archivio nel repository esistente. Per un aggiornamento con molte nuove cartelle e file già presenti, l'uso di Git locale è generalmente più facile perché registra in modo esplicito aggiunte, sostituzioni e rimozioni. Non caricare l'intero `.zip` come unico file al posto dei sorgenti.

## Nota sulle versioni

Questa versione GitHub è una **copia portabile dei sorgenti ZAPPING**: sono stati rimossi gli indirizzi IP privati hardcoded e i percorsi personali, e il client usa per default l'hostname Jellyfin corrente. Non sovrascrive automaticamente l'installazione funzionante sul server. Prima di utilizzare il codice pubblicato in produzione, adattare `--host`, `--origin`, `--playlist` e l'eventuale comando FFmpeg come spiegato in [DEPLOYMENT.md](DEPLOYMENT.md).
