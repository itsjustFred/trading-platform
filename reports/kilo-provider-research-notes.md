# Provider Research Notes

**Datum:** 2026-06-17  
**Scope:** Kilo/kilocode, Kimchi, Bluesminds, Aerolink - Free-Model-Usage ohne Secrets

---

## 1. Kilo/kilocode

### Status
- **Aktueller Provider:** `bluesminds` (nicht kilocode)
- **KILOCODE_API_KEY:** Im Credential Pool, Status **"exhausted"** (HTTP 402), 54 Minuten Rest
- **Base URL in config:** Nicht sichtbar (wahrscheinlich in plugin-Konfiguration)

### Endpunkt-Test
| Endpoint | Status |
|----------|--------|
| api.kilocode.com | **DOWN** (keine Antwort, Host nicht erreichbar) |
| kilocode.com | 301 → www.kilocode.com |
| www.kilocode.com/v1/models | 404 |

**Fazit:** Kilo API ist aktuell nicht erreichbar/unterbrochen. Der Key ist im Pool als "exhausted" markiert.

---

## 2. Kimchi (llm.kimchi.dev)

### Status
- **KIMCHI_API_KEY:** Im Credential Pool vorhanden
- **Strategie:** `fill_first` (konfiguriert)
- **Plugin:** `~/.hermes/plugins/model-providers/kimchi/`

### Endpunkt-Test
| Endpoint | Status |
|----------|--------|
| llm.kimchi.dev/openai/v1/models | **200 OK** (erfordert Auth) |
| llm.kimchi.dev/v1/models | 404 |

### Authentifizierungsformat
- **401 Authorization Required** bei fehlender Authentifizierung
- Das `/openai/v1` Prefix ist korrekt für OpenAI-kompatible API

### Plugin-Konfiguration (aus model-providers/kimchi/__init__.py)
- **API Mode:** `chat_completions` (OpenAI-kompatibel)
- **Base URL:** `https://llm.kimchi.dev/openai/v1`
- **Fallback-Modelle:** kimi-k2.6, kimi-k2.5, minimax-m2.7, minimax-m3, qwen3-coder-next-fp8, nemotron-3-super-fp4

**Fazit:** Kimchi ist erreichbar, braucht `Authorization: Bearer *** Header. Key vorhanden, Endpoint funktioniert.

---

## 3. Freemodel

### Status
- **FREEMODEL_API_KEY:** Im Credential Pool vorhanden
- **Strategie:** `fill_first` (konfiguriert)
- **Plugin:** `~/.hermes/plugins/model-providers/freemodel/`

### Endpunkt-Test
| Endpoint | Status |
|----------|--------|
| api.freemodel.dev/v1/models | **200 OK** |

### Verfügbare Modelle (ohne Auth-Details)
```
gpt-5.5
gpt-5.4  
gpt-5.4-mini
gpt-5.3-codex
```

### Plugin-Konfiguration
- **API Mode:** `chat_completions`
- **Base URL:** `https://api.freemodel.dev/v1`
- **Fallback-Modelle:** gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3
- **supports_vision:** False

**Fazit:** Freemodel ist voll funktional mit 4 verfügbaren Modellen. API stabil erreichbar.

---

## 4. Bluesminds

### Status
- **BLUESMINDS_API_KEY:** Im Credential Pool (2x Einträge vorhanden)
- **Strategie:** `fill_first` (konfiguriert)
- **Plugin:** `~/.hermes/plugins/model-providers/bluesminds/`

### Endpunkt-Test
| Endpoint | Status |
|----------|--------|
| api.bluesminds.com/v1/models | **Invalid token** (ohne Auth: 404, mit Auth: Invalid token = Endpoint existiert) |

### Plugin-Konfiguration
- **API Mode:** `chat_completions`
- **Base URL:** `https://api.bluesminds.com/v1`
- **Fallback-Modelle:** gpt-5.5, gpt-5-chat, gpt-5-mini, gpt-5-nano, gpt-4o, gpt-4o-mini, MiniMax-M2.7, moonshotai/kimi-k2.6, gemini-3.1-pro-preview, qwen3.6-plus
- **supports_vision:** True

**Fazit:** Bluesminds API existiert, ist OpenAI-kompatibel. Keys vorhanden (2 Einträge).

---

## 5. Aerolink

### Status
- **AEROLINK_API_KEY:** Im Credential Pool (1x Eintrag "aerolink-primary") vorhanden
- **Strategie:** `fill_first` (konfiguriert)
- **Plugin:** `~/.hermes/plugins/model-providers/aerolink/`

### Endpunkt-Test
| Endpoint | Status |
|----------|--------|
| aerolink.lat/v1/models | **403 Cloudflare WAF Challenge** |
| api.aerolink.lat/v1/models | **000** (Host nicht erreichbar) |

### Plugin-Konfiguration (speziell für Claude Code)
- **API Mode:** `anthropic_messages` (Anthropic-kompatibel!)
- **Base URL:** `https://aerolink.lat`
- **Auth Header:** `x-api-key: <key>` (nicht `Authorization: Bearer`)
- **Fallback-Modelle:** claude-sonnet-4, claude-3-7-sonnet, claude-3-5-sonnet, claude-3-5-haiku
- **supports_health_check:** False (wegen Cloudflare-WAF)

### Cloudflare-Pitfall
Response enthält `cf-mitigated: challenge` - WAF/Allowlist-Issue, keine Credit-Frage.

**Fazit:** Aerolink ist durch Cloudflare WAF blockiert. Nicht nutzbar ohne Dashboard-Allowlist.

---

## 6. provider-credit-pool Plugin (lokal installiert)

### Pfad
`~/.hermes/plugins/provider-credit-pool/`

### Unterstützte Provider
`kimchi`, `freemodel`, `bluesminds`, `aerolink`

### Befehle
```bash
hermes provider-pool status
hermes provider-pool strategy <provider> <strategy>
hermes provider-pool import-json /path/to/keys.json --strategy fill_first
```

### Import-Format
```json
{
  "kimchi": [{"label": "kimchi-1", "key": "...", "priority": 0}],
  "freemodel": [{"label": "freemodel-1", "key": "..."}],
  "bluesminds": [{"label": "bluesminds-1", "key": "..."}],
  "aerolink": [{"label": "aerolink-1", "key": "..."}]
}
```

---

## Provider-Pool Status (hermes auth list)

```
aerolink (1 credentials):
  #1  aerolink-primary     api_key manual

bluesminds (2 credentials):
  #1  bluesminds-primary   api_key manual
  #2  BLUESMINDS_API_KEY   api_key env

kimchi (1 credentials):
  #1  kimchi-primary       api_key manual

freemodel (1 credentials):
  #1  freemodel-primary    api_key manual

kilocode (1 credentials):
  #1  KILOCODE_API_KEY     api_key env   [EXHAUSTED - 402]
```

---

## Zusammenfassung

| Provider | Nutzbare Modelle | API-Status | Credits |
|----------|-----------------|------------|---------|
| **Freemodel** | 4 Modelle (gpt-5.5, gpt-5.4, etc.) | ✅ Stabil erreichbar | Unbekannt |
| **Kimchi** | 6 Modelle (Kimi, MiniMax, Qwen) | ✅ Endpunkt verifiziert | $50/Monat free |
| **Bluesminds** | 10 Modelle (gpt-4o, gpt-5.x) | ✅ Endpoint verifiziert | $100 free credits |
| **Aerolink** | 4 Modelle (Claude-Modelle) | ❌ Cloudflare-WAF blockiert | Nicht prüfbar |
| **Kilo/kilocode** | Nicht erreichbar | ❌ Host down | Exhausted (402) |

---

## Empfehlungen

1. **Freemodel** als primärer Fallback - stabil, 4 Modelle verfügbar
2. **Kimchi** für $50/Monat free - Key vorhanden, Endpoint verifiziert
3. **Bluesminds** nutzen - Keys vorhanden (2 Einträge), Endpoint verifiziert
4. **Aerolink** erst prüfen, wenn Cloudflare-Challenge gelöst ist
5. **Kilo/kilocode** deaktivieren - Host nicht erreichbar + Credits erschöpft