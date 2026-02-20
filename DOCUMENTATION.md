# Gaia Orchestrator – Wiki Generator Service

A **Gaia Orchestrator** egy FastAPI-alapú mikroszerviz, amely ClickUp-ban hoz létre strukturált wiki oldalakat. Egy JSON payload alapján rekurzívan létrehozza a Doc oldalakat a ClickUp API-n (v2 + v3) keresztül.

---

## Tartalomjegyzék

1. [Architektúra áttekintés](#1-architektúra-áttekintés)
2. [Projekt struktúra](#2-projekt-struktúra)
3. [Kód részletes leírása](#3-kód-részletes-leírása)
4. [JSON Payload formátum](#4-json-payload-formátum)
5. [API Endpointok](#5-api-endpointok)
6. [ClickUp Automation + SuperAgent integráció](#6-clickup-automation--superagent-integráció)
7. [MCP Server (AI Agent integráció)](#7-mcp-server-ai-agent-integráció)
8. [OAuth 2.1 PKCE](#8-oauth-21-pkce)
9. [Heroku Deployment](#9-heroku-deployment)
10. [Környezeti változók](#10-környezeti-változók)
11. [Tesztelés](#11-tesztelés)
12. [Hibaelhárítás](#12-hibaelhárítás)

---

## 1. Architektúra áttekintés

```
┌─────────────────────┐
│  ClickUp SuperAgent │
│  (AI Agent)         │
└────────┬────────────┘
         │ Létrehoz egy taskot a wiki
         │ JSON payload-dal a leírásban
         ▼
┌─────────────────────┐
│  ClickUp Automation │
│  "When task created" │
│  → Webhook POST     │
└────────┬────────────┘
         │ POST /api/v1/webhook/clickup
         ▼
┌─────────────────────────────────────┐
│  Gaia Orchestrator (FastAPI)        │
│  Heroku: gaia-orchestrator-xxx.app  │
│                                     │
│  1. Webhook fogadás                 │
│  2. Task description lekérés        │
│     (ClickUp API v2)                │
│  3. JSON parse + normalizálás       │
│  4. Wiki oldalak létrehozása        │
│     (ClickUp API v3)                │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────┐
│  ClickUp Doc/Wiki   │
│  Struktúrált oldalak│
└─────────────────────┘
```

### Működési folyamat

1. A **SuperAgent** a felhasználói prompt alapján generál egy JSON payloadot, amit egy task leírásába ír.
2. A **ClickUp Automation** figyeli az új taskokat, és webhook-kal értesíti a Gaia Orchestratort.
3. A **Gaia Orchestrator** kiszedi a task ID-t a webhook body-ból, lekéri a task description-t a ClickUp API-ról.
4. A description-ből kiparse-olja a JSON payloadot (code blockból, plain textből, vagy rich textből).
5. Normalizálja a mező neveket (pl. `target_url` → `target.url`, `summary` → `content`).
6. Rekurzívan létrehozza az oldalakat a ClickUp Docs API v3-on keresztül.

---

## 2. Projekt struktúra

```
Gaia_orchestrator/
├── app/
│   ├── __init__.py          # Package init
│   ├── main.py              # FastAPI app entry point, CORS, router mount
│   ├── config.py            # Settings (env vars), Pydantic BaseSettings
│   ├── models.py            # Pydantic modellek (WikiPage, TargetLocation, stb.)
│   ├── routes.py            # API endpointok (REST + webhook)
│   ├── wiki_builder.py      # Rekurzív oldal-feltöltő, job management
│   ├── clickup_client.py    # Async ClickUp API kliens (httpx)
│   ├── mcp_server.py        # MCP szerver (JSON-RPC 2.0 / SSE)
│   └── oauth.py             # OAuth 2.1 PKCE szerver
├── Procfile                 # Heroku start command
├── runtime.txt              # Python verzió (3.12.4)
├── requirements.txt         # Python függőségek
└── .gitignore
```

---

## 3. Kód részletes leírása

### 3.1 `app/config.py` – Konfiguráció

A `Settings` osztály a `pydantic-settings` könyvtárat használja környezeti változók betöltésére. A `.env` fájlból vagy a rendszer env vars-ból olvas.

Főbb beállítások:
- `CLICKUP_API_KEY` – ClickUp Personal API Token (kötelező)
- `API_SECRET` – opcionális shared secret a REST API védelemre
- `UPLOAD_DELAY` – várakozás API hívások között (rate limit elkerülése, default: 1.2s)
- `API_RETRIES` – újrapróbálkozások száma hiba esetén (default: 5)
- `JWT_SECRET` – JWT token aláírás az OAuth/MCP-hez

### 3.2 `app/models.py` – Pydantic modellek

#### `WikiPage`
Egy wiki oldal a fában. Rekurzív struktúra: minden oldalnak lehet `children` listája.

```python
class WikiPage(BaseModel):
    title: str          # Oldal címe
    content: str = ""   # Markdown tartalom
    children: list[WikiPage] = []  # Gyerek oldalak
```

#### `TargetLocation`
Meghatározza, hová kerüljenek az oldalak a ClickUp-ban. A legegyszerűbb mód egy ClickUp URL megadása – az ID-k automatikusan kinyerhetők belőle.

Támogatott URL formátumok:
- **Doc page URL**: `https://app.clickup.com/{team_id}/docs/{doc_id}/{page_id}` → oldalak az adott page alá kerülnek
- **Doc root URL**: `https://app.clickup.com/{team_id}/docs/{doc_id}` → oldalak a doc tetejére kerülnek
- **Space URL**: `https://app.clickup.com/{team_id}/v/s/{space_id}` → új Doc jön létre

A URL-ből a `_CLICKUP_DOC_RE` és `_CLICKUP_SPACE_RE` regex minták nyerik ki az ID-kat. A `@model_validator` automatikusan parse-olja a URL-t és kitölti a hiányzó mezőket.

#### `WikiCreateRequest`
A fő request modell:
```python
class WikiCreateRequest(BaseModel):
    doc_name: str = "Wiki"        # Új doc neve (csak új doc esetén)
    target: TargetLocation         # Hová
    pages: list[WikiPage]          # Mit
```

### 3.3 `app/clickup_client.py` – ClickUp API kliens

Async HTTP kliens `httpx`-szel. Főbb jellemzők:

- **Retry logika** exponenciális backoff-fal (429, 500, 502, 503 hibakódokra)
- **v2 API** hívások: `get_teams()`, `get_task()`, `get_spaces()`
- **v3 API** hívások: `create_doc()`, `create_page()`, `get_doc()`

A `create_page()` metódus:
- Workspace ID + Doc ID + opcionális parent_page_id alapján hoz létre oldalt
- 90KB feletti tartalom automatikus truncálása
- Endpoint: `POST /api/v3/workspaces/{wid}/docs/{did}/pages`

### 3.4 `app/wiki_builder.py` – Wiki Builder (core logika)

Ez a modul tartalmazza a tényleges wiki-létrehozási logikát.

#### Job management
In-memory job store (`dict[str, JobStatusResponse]`), amely tárolja a futó és befejezett job-ok állapotát. A Heroku dyno újraindításakor ez elvész.

#### `run_wiki_creation()`
1. Generál egy egyedi `job_id`-t
2. Létrehoz egy `JobStatusResponse` objektumot `queued` státusszal
3. Elindít egy `asyncio.create_task()` háttérfeladatot
4. Azonnal visszaadja a `job_id`-t

#### `_execute_job()`
1. **Workspace ID feloldás** – ha nincs megadva, az API key-hez tartozó első workspace-t használja
2. **Doc létrehozás vagy meglévő használat** – ha van `doc_id`, meglévő docba szúr; ha nincs, `space_id` alapján újat hoz létre
3. **Rekurzív feltöltés** (`_upload_pages()`) – végigmegy a page fán, minden oldalhoz:
   - Létrehozza az oldalt a ClickUp API-n
   - A kapott `page_id`-t használja a gyerek oldalak `parent_page_id`-jéként
   - Delay az API hívások között (rate limit)

### 3.5 `app/routes.py` – API Endpointok

#### Webhook feldolgozás (`POST /api/v1/webhook/clickup`)

A webhook endpoint robusztus payload-kinyerést végez, 4 prioritási szinttel:

1. **Priority 1** – A body maga a wiki payload (Swagger/manuális teszt)
2. **Priority 2** – `payload` query paraméter
3. **Priority 3** – JSON keresés a nyers body szövegben
4. **Priority 4** – Task ID kinyerés a webhook body-ból → ClickUp API hívás → description parse

#### JSON kinyerés a task description-ből

A ClickUp task description rich text (Quill Delta) formátumban tárolja a szöveget, ami nem közvetlenül parse-olható JSON. Ezért a kód:

1. A ClickUp API-tól lekéri a task-ot, és 3 mezőt próbál sorban:
   - `text_content` (plain text – legmegbízhatóbb)
   - `description` (rich text)
   - `markdown_description`

2. Mindegyik mezőn a `_try_parse_wiki_json()` fut, amely:
   - **Code block extraction** – ` ```json ... ``` ` blokkokból kiemeli a JSON-t (legmegbízhatóbb)
   - **Direct parse** – megpróbálja `json.loads()`-szal az egész szöveget
   - **Substring extraction** – `find("{")` / `rfind("}")` között keresés
   - Mindegyik stratégia a nyers szövegen és egy "tisztított" verzión is fut (HTML tagek, entitások, smart quotes eltávolítása)

#### Payload normalizálás (`_normalize_wiki_payload()`)

A SuperAgent nem mindig a pontos mezőneveket használja. A normalizáló automatikusan átalakítja:

| Agent mező | Várt mező | Átalakítás |
|---|---|---|
| `target_url` (string) | `target: {"url": ...}` | String → object |
| `summary` | `content` | Átnevezés (rekurzív a pages-ben) |

### 3.6 `app/mcp_server.py` – MCP Server

A Model Context Protocol (MCP) szerver JSON-RPC 2.0 üzeneteket fogad SSE (Server-Sent Events) transporton keresztül. Ez lehetővé teszi, hogy a ClickUp AI Agents közvetlenül hívja a wiki-létrehozó eszközöket.

Elérhető tool-ok:
- **`create_wiki`** – Wiki létrehozás (URL + pages)
- **`check_wiki_status`** – Job állapot lekérdezés

Endpoint-ok:
- `GET /mcp/sse` – SSE kapcsolat
- `POST /mcp/messages/` – JSON-RPC üzenetek

A `BearerAuthMiddleware` JWT tokennel védi az MCP endpointokat (ha `JWT_SECRET` be van állítva).

### 3.7 `app/oauth.py` – OAuth 2.1 PKCE

A ClickUp MCP integráció OAuth 2.1 PKCE autentikációt igényel. Az implementáció:

- `/.well-known/oauth-authorization-server` – Discovery metadata
- `GET /oauth/authorize` – Consent page (HTML form)
- `POST /oauth/authorize` – Authorization code kibocsátás
- `POST /oauth/token` – Code → JWT token csere (PKCE ellenőrzéssel)
- `POST /oauth/register` – Dinamikus kliens regisztráció

A tokenek 8 órás JWT-k, HS256 algoritmussal aláírva.

---

## 4. JSON Payload formátum

### Közvetlen API hívás (`POST /api/v1/wiki`)

```json
{
  "doc_name": "CR002: ABC sorrendben a megrendelés",
  "target": {
    "url": "https://app.clickup.com/90151997238/docs/2kyqmktp-25455/2kyqmktp-18535"
  },
  "pages": [
    {
      "title": "CR002: ABC sorrendben a megrendelés",
      "content": "",
      "children": [
        {
          "title": "BR",
          "content": "Business Requirement placeholder.",
          "children": []
        },
        {
          "title": "FS",
          "content": "Full Functional Specification.",
          "children": []
        }
      ]
    }
  ]
}
```

### SuperAgent formátum (automatikusan normalizált)

Az agent által generált formátum is elfogadott – a szerviz automatikusan átalakítja:

```json
{
  "doc_name": "CR002: ABC sorrendben a megrendelés",
  "target_url": "https://app.clickup.com/90151997238/docs/2kyqmktp-25455/2kyqmktp-18535",
  "pages": [
    {
      "title": "BR",
      "summary": "Business Requirement placeholder.",
      "children": []
    }
  ]
}
```

### Fontos megjegyzések a struktúráról

- A `doc_name` mező **csak új Doc létrehozásakor** használatos (ha Space URL-t adunk meg).
- Ha **meglévő Doc-ba** szúrunk be (Doc URL), a `pages` tömb elemei **közvetlenül** a megadott parent page alá kerülnek.
- Ha szeretnél **wrapper page-t** (pl. a CR neve, alatta az aloldalak), azt a `pages` struktúrában kell definiálni a `children`-nel.

### Target URL variációk

| URL típus | Viselkedés |
|---|---|
| `https://app.clickup.com/{team}/docs/{doc_id}/{page_id}` | Oldalak a `page_id` alá kerülnek |
| `https://app.clickup.com/{team}/docs/{doc_id}` | Oldalak a doc tetejére kerülnek |
| `https://app.clickup.com/{team}/v/dc/{doc_id}/{page_id}` | Ugyanaz (régi URL formátum) |
| `https://app.clickup.com/{team}/v/s/{space_id}` | Új Doc jön létre a space-ben |

---

## 5. API Endpointok

### REST API (`/api/v1/`)

| Metódus | Endpoint | Leírás |
|---|---|---|
| `POST` | `/api/v1/wiki` | Wiki létrehozás JSON payloaddal |
| `GET` | `/api/v1/wiki/{job_id}` | Job állapot lekérdezés |
| `GET` | `/api/v1/wiki` | Összes job listázás |
| `GET` | `/api/v1/wiki/create` | Wiki létrehozás GET-tel (query params) |
| `POST` | `/api/v1/webhook/clickup` | ClickUp Automation webhook fogadás |
| `GET` | `/api/v1/health` | Health check |

### Swagger UI

A Swagger UI elérhető: `https://<host>/docs`

### Job állapotok

Egy wiki-létrehozás aszinkron – a POST azonnal visszaadja a `job_id`-t, amit a `GET /api/v1/wiki/{job_id}` endpointtal lehet pollingolni.

Állapotok: `queued` → `running` → `completed` / `failed`

---

## 6. ClickUp Automation + SuperAgent integráció

### SuperAgent konfiguráció

A SuperAgent feladata, hogy a felhasználói prompt alapján generálja a wiki JSON payload-ot, majd létrehozzon egy taskot, amelynek a description-jébe beírja a JSON-t.

#### Ajánlott SuperAgent prompt

```
Te egy wiki-struktúra generátor vagy. A felhasználó megmondja, milyen wiki struktúrát
szeretne létrehozni a ClickUp-ban, és te generálsz egy JSON payloadot.

A JSON formátum:
{
  "doc_name": "<wiki neve>",
  "target_url": "<ClickUp Doc URL ahová az oldalak kerülnek>",
  "pages": [
    {
      "title": "<főoldal neve>",
      "content": "<tartalom markdown-ban>",
      "children": [
        {
          "title": "<aloldal neve>",
          "content": "<tartalom>",
          "children": []
        }
      ]
    }
  ]
}

FONTOS:
- A JSON-t MINDIG code blockba (``` ```) tedd.
- Ha a felhasználó hierarchikus struktúrát kér, használd a children mezőt.
- A target_url a ClickUp doc URL, ahová az oldalak kerülnek.
- Ha wrapper/főoldalt is kér, azt a pages tömb első elemeként hozd létre,
  és az aloldalakat a children-jébe tedd.
```

### ClickUp Automation beállítás

1. **Menj a ClickUp Automations-be** (Space szinten)
2. **Trigger**: "When task is created" (vagy specific list-re szűrve)
3. **Action**: "Call webhook"
4. **Webhook URL**: `https://gaia-orchestrator-dff6cda3e813.herokuapp.com/api/v1/webhook/clickup`
5. **Method**: POST
6. **URL Parameters**: Nem szükséges (a task ID automatikusan benne van a webhook body-ban)

### Működési folyamat

```
Felhasználó → SuperAgent prompt → Agent generálja a JSON-t
    → Agent létrehoz egy taskot a JSON-nal a description-ben
        → ClickUp Automation trigger (task created)
            → Webhook POST a Gaia Orchestrator-nak
                → Orchestrator kiszedi a task_id-t a body-ból
                    → ClickUp API-ról lekéri a task description-t
                        → JSON parse (code block / plain text / rich text)
                            → Wiki oldalak létrehozása
```

### Webhook body struktúra (ClickUp Automation)

A ClickUp Automation webhook a következő formátumban küldi a body-t:

```json
{
  "auto_id": "...",
  "trigger_id": "...",
  "date": "2026-02-20T22:05:17.595Z",
  "payload": {
    "id": "86c8cefdg",          // ← task ID (ezt használjuk)
    "name": "Task neve",
    "content": "{\"ops\":[...]}" // ← Quill Delta (NEM közvetlenül parse-olható)
  }
}
```

A `payload.id` mezőből kapjuk a task ID-t, aztán a ClickUp API v2-ről a `GET /api/v2/task/{task_id}` végponttal lekérjük a task adatait, beleértve a `text_content` mezőt (plain text description).

---

## 7. MCP Server (AI Agent integráció)

A Model Context Protocol lehetővé teszi a ClickUp AI Agents közvetlen integrációját.

### Endpointok

- `GET /mcp/sse` – SSE kapcsolat a MCP kliensnek
- `POST /mcp/messages/` – JSON-RPC 2.0 üzenetek

### Elérhető tool-ok

#### `create_wiki`
```json
{
  "url": "https://app.clickup.com/.../docs/...",
  "doc_name": "Wiki neve",
  "pages": [
    {"title": "Oldal 1", "content": "# Tartalom", "children": []}
  ]
}
```

#### `check_wiki_status`
```json
{
  "job_id": "abc123def456"
}
```

### Autentikáció

Ha `JWT_SECRET` be van állítva, a MCP endpointok Bearer token-t igényelnek (az OAuth flow-ból kapott JWT).

Ha `JWT_SECRET` nincs beállítva → dev mód, nincs autentikáció.

---

## 8. OAuth 2.1 PKCE

Az OAuth flow a ClickUp MCP specifikáció szerint van implementálva.

### Discovery

```
GET /.well-known/oauth-authorization-server
```

### Flow

1. **Client Registration**: `POST /oauth/register` – kliens regisztrálás
2. **Authorization**: `GET /oauth/authorize` – consent page megjelenítés → `POST /oauth/authorize` – code kibocsátás
3. **Token Exchange**: `POST /oauth/token` – authorization code + PKCE verifier → JWT access token + refresh token

### Token jellemzők
- Típus: JWT (HS256)
- Érvényesség: 8 óra
- Refresh token: végtelen (amíg a szerver fut)

---

## 9. Heroku Deployment

### Fájlok

- **`Procfile`**: `web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`
- **`runtime.txt`**: `python-3.12.4`
- **`requirements.txt`**: Összes Python függőség

### Deployment lépések

1. **Heroku app létrehozás** (ha még nem létezik):
   ```bash
   heroku create gaia-orchestrator
   ```

2. **Környezeti változók beállítása**:
   ```bash
   heroku config:set CLICKUP_API_KEY=pk_xxx
   heroku config:set API_SECRET=my-secret       # opcionális
   heroku config:set JWT_SECRET=my-jwt-secret    # opcionális
   ```

3. **Git push** (GitHub-on keresztül):
   ```bash
   git push origin main
   ```
   Ha a Heroku app össze van kötve a GitHub repóval, automatikusan deploy-ol.

4. **Vagy közvetlen Heroku push**:
   ```bash
   heroku git:remote -a gaia-orchestrator
   git push heroku main
   ```

### Fontos tudnivalók

- **Free tier**: A Heroku eco dyno alvásba megy 30 perc inaktivitás után. Az első kérés ~10 másodpercet késhet.
- **In-memory store**: A job-ok in-memory vannak tárolva – dyno újraindításkor elvesznek.
- **Logok megtekintése**: `heroku logs --tail`

### Jelenlegi deployment

- **URL**: `https://gaia-orchestrator-dff6cda3e813.herokuapp.com`
- **Swagger**: `https://gaia-orchestrator-dff6cda3e813.herokuapp.com/docs`
- **GitHub repo**: `https://github.com/pentadotddot/goalz-gaia-orchestrator`

---

## 10. Környezeti változók

| Változó | Kötelező | Default | Leírás |
|---|---|---|---|
| `CLICKUP_API_KEY` | ✅ | – | ClickUp Personal API Token |
| `CLICKUP_API_BASE` | ❌ | `https://api.clickup.com` | ClickUp API base URL |
| `API_SECRET` | ❌ | – | Shared secret az `X-Api-Secret` headerhez |
| `JWT_SECRET` | ❌ | – | JWT aláírási kulcs (OAuth/MCP) |
| `UPLOAD_DELAY` | ❌ | `1.2` | Várakozás (mp) API hívások között |
| `MAX_CONTENT_SIZE` | ❌ | `90000` | Max oldal tartalom méret (byte) |
| `API_RETRIES` | ❌ | `5` | Újrapróbálkozások száma |
| `API_RETRY_BASE_DELAY` | ❌ | `3.0` | Exponenciális backoff alap (mp) |
| `LOG_LEVEL` | ❌ | `info` | Logging szint |
| `PORT` | ❌ | `8000` | Szerver port (Heroku automatikusan beállítja) |

---

## 11. Tesztelés

### Swagger UI-ból (POST /api/v1/wiki)

1. Nyisd meg: `https://gaia-orchestrator-dff6cda3e813.herokuapp.com/docs`
2. Kattints a `POST /api/v1/wiki` endpointra → "Try it out"
3. Illeszd be a JSON payloadot:

```json
{
  "doc_name": "Test Wiki",
  "target": {
    "url": "https://app.clickup.com/90151997238/docs/2kyqmktp-25455/2kyqmktp-18535"
  },
  "pages": [
    {
      "title": "Test Page",
      "content": "# Hello\n\nEz egy teszt oldal.",
      "children": [
        {
          "title": "Child Page",
          "content": "## Aloldal\n\nGyerek tartalom.",
          "children": []
        }
      ]
    }
  ]
}
```

4. Kattints "Execute" → kapsz egy `job_id`-t
5. `GET /api/v1/wiki/{job_id}` – pollingolhatod az állapotot

### Webhook tesztelés (cURL)

```bash
curl -X POST https://gaia-orchestrator-dff6cda3e813.herokuapp.com/api/v1/webhook/clickup \
  -H "Content-Type: application/json" \
  -d '{
    "doc_name": "Test Wiki",
    "target": {
      "url": "https://app.clickup.com/90151997238/docs/2kyqmktp-25455/2kyqmktp-18535"
    },
    "pages": [
      {"title": "Test", "content": "Hello", "children": []}
    ]
  }'
```

### ClickUp Automation tesztelés

1. Hozz létre egy taskot a megfelelő listában
2. A task description-jébe írd be a JSON-t (lehetőleg code blockba)
3. Az automation automatikusan triggerelődik
4. Nézd a Heroku logokat: `heroku logs --tail`

---

## 12. Hibaelhárítás

### Gyakori problémák

| Probléma | Ok | Megoldás |
|---|---|---|
| "Either target.doc_id or target.space_id must be provided" | A URL nem parseable vagy a `target_url` mező nincs normalizálva | Ellenőrizd az URL formátumot; a kód automatikusan normalizálja a `target_url`-t |
| 422 Unprocessable Entity a webhook-on | A FastAPI nem tudja parse-olni a body-t | A jelenlegi kód nyers body-t olvas, ez nem fordulhat elő |
| "task description did not contain valid wiki JSON" | A description rich text formátumú, nincs benne parse-olható JSON | Tedd code blockba a JSON-t a description-ben |
| Job not found (GET után) | Heroku dyno újraindult, in-memory store elveszett | Frissítsd az oldalt és próbáld újra |
| 401 Unauthorized (task fetch) | Az API key nem érvényes, vagy a task_id `{}` (template tag nem oldódott fel) | Ellenőrizd a `CLICKUP_API_KEY`-t; a kód kiszűri a `{}` task ID-kat |

### Heroku logok

```bash
heroku logs --tail -a gaia-orchestrator-dff6cda3e813
```

A logokban keresendő kulcsszavak:
- `Webhook received` – webhook beérkezett
- `found task_id=` – task ID sikeresen kinyerve
- `parsed wiki JSON from task field` – JSON sikeresen parse-olva
- `Job ... started` – wiki létrehozás elindult
- `Job ... finished` – wiki létrehozás befejezve
- `FAILED` – hiba történt egy oldal létrehozásakor
