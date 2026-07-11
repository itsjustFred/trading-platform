# Migration Guide: Data-Directory auf zweite Disk

> Durchgeführt: 2026-02-26
> Von: `~/data/` → `/mnt/block-volume-data/`

---

## Was wurde gemacht

### 1. Ownership der zweiten Disk anpassen

```bash
sudo chown -R ubuntu:ubuntu /mnt/block-volume-data/
```

Die Disk war als `root:root` gemountet. Ohne `chown` hätte der ubuntu-User keine Schreibrechte.

### 2. Daten kopieren

```bash
rsync -av ~/data/ /mnt/block-volume-data/
```

Kopiert alle Dateien mit Verzeichnisstruktur. rsync statt cp wegen:
- Fortschrittsanzeige
- Prüfsummen-Verifikation
- Kann bei Abbruch fortgesetzt werden

### 3. Checksummen verifizieren

```bash
rsync -aHAXn --delete --checksum ~/data/ /mnt/block-volume-data/
```

`rsync` vergleicht relative Dateinamen und Inhalte per Checksumme, ohne unsicheres `xargs`-Parsing. Der Befehl läuft im Dry-Run (`-n`); er listet nur Unterschiede. Die Verifikation ist erfolgreich, wenn `rsync` **keine** Differenzen ausgibt (kein Output, Exit-Status 0).

### 4. Altes Verzeichnis durch Symlink ersetzen

```bash
# 1. Sicherstellen, dass das Ziel gemountet ist - sonst abbrechen
if ! mountpoint -q /mnt/block-volume-data; then
  echo "FEHLER: /mnt/block-volume-data ist kein aktiver Mount. Abbruch." >&2
  exit 1
fi

# 2. Originalverzeichnis sichern statt sofort loeschen
if [ -e ~/data ] && [ ! -L ~/data ]; then
  mv ~/data ~/data.rollback
fi

# 3. Symlink setzen
ln -s /mnt/block-volume-data ~/data
```

Ergebnis: `~/data/` → `/mnt/block-volume-data/`

> **Rollback:** Das Original liegt unter `~/data.rollback`. Erst nach erfolgreicher Post-Reboot-Verifikation (Schritt 5) darf dieses Verzeichnis gelöscht werden:
```bash
rm -rf ~/data.rollback
```

Alle bestehenden Pfade (Runner CLI, Viewer, catalog_builder, etc.) funktionieren weiterhin, da sie `~/data/` verwenden.

### 5. Verifizierung

- ✅ `catalog.json` lesbar via Symlink (97 OHLCV-Dateien)
- ✅ Backtest-Ergebnisse in `~/data/backtests/` erreichbar
- ✅ Validierungs-Report lesbar

---

## Aktuelle Struktur

```
~/data → /mnt/block-volume-data/     (Symlink)

/mnt/block-volume-data/
├── catalog.json                      (68 KB)
├── validation_report.json            (278 KB)
├── backtests/                        (Backtest-Ergebnisse)
├── ohlcv/                            (97 Parquet-Dateien, ~18 MB)
│   ├── dukascopy/forex/XAUUSD/
│   └── yfinance/equity/US/*/
├── fundamentals/
├── tick/
└── universes/
```

Disk: 98 GB verfügbar (`/dev/sdb`, ext4)

---

## Rollback (Rückgängig machen)

Falls die zweite Disk Probleme macht:

```bash
# 1. Symlink entfernen
rm ~/data

# 2. Daten zurückkopieren
mkdir ~/data
rsync -av /mnt/block-volume-data/ ~/data/

# 3. Fertig — alle Pfade funktionieren wieder lokal
```

---

## Hinweise für zukünftige Server-Migration

1. **Symlink-Ansatz beibehalten**: Alle Tools verwenden `~/data/` oder `$TRADING_DATA_DIR`. Solange der Symlink korrekt zeigt, ist der physische Speicherort egal.

2. **Vor Migration prüfen**:
   - `df -h /mnt/block-volume-data/` — Genug Platz auf Ziel?
   - `ls -la ~/data` — Ist es ein Symlink oder echtes Verzeichnis?

3. **rsync ist idempotent**: Kann mehrfach ausgeführt werden, kopiert nur geänderte Dateien.

4. **Backup vor Disk-Änderungen**:
   ```bash
   tar czf ~/data-backup-$(date +%Y%m%d).tar.gz -C /mnt/block-volume-data .
   ```

5. **Environment Variable**: Der Runner verwendet `TRADING_DATA_DIR` (Default: `~/data/`). Bei Bedarf kann der Pfad auch ohne Symlink geändert werden:
   ```bash
   export TRADING_DATA_DIR=/mnt/block-volume-data
   ```

6. **Mount beim Reboot prüfen**: `/mnt/block-volume-data/` muss in `/etc/fstab` eingetragen sein, sonst ist die Disk nach Neustart nicht verfügbar.
   ```bash
   grep block-volume /etc/fstab
   ```
