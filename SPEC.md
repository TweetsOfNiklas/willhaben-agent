# willhaben Agent – Handoff Spec

Self-contained brief for an LLM/agent continuing this project on the home server. Read this first, then
`README.md` and `docs/willhaben-api.md`. State as of 2026-10-09 (deployed; task 1 done, waiting on tokens + login).

Setup happens in the **web UI**, not in `.env`: Settings → Claude ("Sign in with Claude"), Settings → Home Assistant
(OAuth connect) and Settings → willhaben account (login in the remote browser view). Every environment variable in
`.env.example` is an optional override; only `DASHBOARD_PASSWORD` is strongly recommended.

## 1. Goal

A self-hosted agent for **willhaben.at** (Austrian classifieds) for one private user (the owner):

1. **Search agents** – own polling of willhaben searches, faster than willhaben's e-mail alerts; notify new hits and price drops.
2. **Auto-reactivate** own expired ads.
3. **Chats** – read willhaben conversations, prepare reply drafts with Claude, send only after the user clicks.
4. **Create ads faster** – notes + photos → Claude writes title/description/category; price hint from comparable ads.
5. **Web dashboard** to manage everything, plus an **MCP server** so Claude Desktop/Code can use the app.

## 2. Fixed decisions (do not change without asking the owner)

| Topic | Decision |
|---|---|
| Claude backend | **Option B: `claude -p` subprocess with the Max subscription token** (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`). **No API key.** An Anthropic-SDK backend is only a fallback if B stops working. |
| Stack | Python 3.12+, FastAPI + Jinja2 + HTMX (Pico CSS), SQLModel/SQLite, APScheduler 3, httpx, Playwright Chromium, MCP SDK 2.x (`mcp.server.mcpserver.MCPServer`) |
| Hosting | Docker on the home server. Must use a **home IP**; willhaben blocks datacenter/VPN IPs. |
| Notifications | Home Assistant (`HA_URL`): REST events `willhaben_<kind>` + optional `notify.<service>` push |
| Login | Persistent Playwright profile, headed Chromium on Xvfb, one-time login via **Settings → willhaben account**, which opens the session page (`/session`) with a **remote browser view** (screenshot polling + click/type forwarding, same port/auth as the dashboard; changed on the owner's request 2026-10-09 because noVNC on 6080 didn't work for him). The top nav has no Session tab anymore. noVNC stays as an optional link. |
| UI language | UI is **English**; source strings are English, wrapped in `_()` in templates and `t()` in Python, German translation via `app/i18n` (catalog `app/i18n/de/*.json`). Setting `ui_lang` = `auto` (browser Accept-Language) \| `en` \| `de`; HA pushes use the last language a browser asked for when `auto`. `tests/test_i18n.py` checks that the German catalog is complete. |
| Safety | Never send messages, publish ads or reactivate without explicit user approval. Opt-in per ad counts as approval: reactivation (`auto_renew`) and automatic chat answers (`auto_reply`, added on the owner's request 2026-10-09, with the limits below). Low-frequency polling, all willhaben requests serialized with backoff. |

## 3. Current status

| Feature | Status |
|---|---|
| Search agents, hits, price drops, HA events | Done, live-tested. Builder at `/agents/new`: keyword + willhaben navigators (each value's `webLink`/`hits`), no copied link needed |
| Browser session, login check, silent SSO re-login, request recorder | Done; real login tested 2026-10-09 (`/me` profile is under `userData`), survives container restart |
| Chat list sync, unread notification | Done, verified with real account (snake_case fields; role from `ad_info.user_uuid` vs `user_uuid`) |
| Chat history | Done: `GET chat-api/v1/conversations/{id}/messages`; `system_message`/`integration_message` stored as direction `unknown` |
| Claude reply drafts | Done (tested with fake backend) |
| Send reply | Done from the web app bundle: chat WebSocket `wss://chat-realtime-web.willhaben.at/webapi/chat-ws/v1/ws/`, frame `{req_type:"send_message", request_id, payload:{conversation_id, client_message_id, text, type:"text"}}`, run inside the logged-in page; a send only counts once the REST history shows it. Ping verified live; **first real send not yet done** (setting `send_verified` flips on it) |
| Auto replies | Opt-in per own ad (`MyAd.auto_reply` + `auto_reply_instructions`) plus general `auto_reply_rules` (Settings). Only seller chats, only after `send_verified`, max 3/chat/day and 10/hour, Claude `auto_send` + code checks (placeholders, needs_info, scam, last message mine). Otherwise draft + HA push |
| Human-like replies + read receipts | Done (unit tests; receipt effect on the buyer side not yet observed). Auto replies run per conversation in the background (`chats._humanized_auto`, timing in `app/services/humanize.py`, window `reply_delay_min_s`/`reply_delay_max_s`, toggle `reply_humanize`, env `HUMANIZE_CHAT=0` in tests; ongoing chats where I already answered use the fast window `reply_fast_min_s`/`reply_fast_max_s`, default 5–20 s, with typing capped to it): wait → read receipt → think → typing time by text length → re-sync and re-check (new buyer message → redraft; any other draft/answer → stay out) → limits re-checked under `_send_lock` → send. Draft status `pending` while typing; ignored after a restart. Read receipts: chat WS frame `mark_message_as_read` per unread incoming message (`AccountClient.mark_read`), sent before every reply and when a chat with unread messages is opened in the dashboard |
| Live chat (no polling) | Done, live: own page in the logged-in browser keeps the chat WebSocket open (ping 10 s, reconnect with backoff, watchdog reopens after 90 s silence) and forwards frames naming a conversation to `chats.on_live_event` → debounced `sync()` (≥ 15 s apart). Poll job only runs as fallback (every 15 min) while live is healthy. `CHAT_LIVE=0` turns it off |
| Push buttons (HA) | Done, live: held-back auto replies push "Antwort freigeben?" with Senden / Direkte Antwort (textInput) / Mit Claude überarbeiten (textInput → revised draft, new push) / Verwerfen. `app/services/ha_actions.py` subscribes to `mobile_app_notification_action` over the HA websocket; action ids `WH_<VERB>_<draft>_<hmac>` (secret `data/action-secret`), a draft is acted on only while it is still a draft. Telegram buttons reuse `ha_actions.handle` |
| Push channel | Setting `push_channel`: `auto` (Telegram once linked, else HA), `ha`, `telegram`, `both`. HA events `willhaben_*` always fire. No "new message" push when the newest message is my own (`last_message.is_mine`); with auto reply on, only the auto-reply / approval push |
| Hit viewer | Done, live: `<a data-ad data-hit>` opens `/hits/ad/{id}` in a dialog (`_ad_panel.html`, `_ad_detail.html`) instead of leaving the dashboard; ←/→ browse, opening marks the hit seen; Ctrl/middle click keeps the willhaben link |
| Own ads: import + expiry check + warnings | Done: imported from `/iad/myprofile/myadverts` `__NEXT_DATA__` (daily + button); account status wins over the public page |
| Own ads: auto-reactivate | **Blocked**: endpoint unknown → reminder notification with link |
| Ad draft with Claude + comparable prices | Done, live-tested. Geizhals lookup per draft (`app/services/geizhals.py`: search → best `-a<id>` article → specs `<dl class=specs-grid>` + `lowPrice`), optional spec block in the description, "Improve ad" (refine with instruction, undo via `revisions`) |
| Batch ads ("multiple items at once") | Done (fake backend tested, **not yet live-tested**). `app/services/ad_batch.py`, table `AdBatch`: photo dump (max 60) → background `claude -p` sorts photos into product groups (`SORT_SCHEMA`) → review page `/new-ad/batch/{id}` (drag/select photos between groups, product name, comment, include, re-sort with hint; comments carry over by photo overlap) → `create_draft()` per group (2 in parallel), drafts get `batch_id` and are locked in the batch. Finished drafts are reviewed **inline on the batch page** (`_batch_draft.html`, htmx: `/ad-drafts/{id}/inline`, `/inline/refine` → `refine_draft`, `/inline/status` draft/published/discarded, `/photos.zip`), copy buttons with an http fallback, progress bar; while drafting the page polls `/new-ad/batch/{id}/progress` instead of reloading. Jobs run as asyncio tasks; `recover()` on startup clears jobs a restart interrupted |
| Telegram bot | Done (mocked Bot API tests, **not yet live-tested**). `app/services/telegram.py`: long polling (no public URL), setup in Settings → Telegram (BotFather token → `data/telegram-token`, one-time `/start <code>` links exactly one chat; other chats ignored). `notify()` also posts to Telegram (per-kind filter); same `tag` edits the message, `clear_push` removes its buttons. Buttons → table `TelegramAction` (`t:<token>`): held-back answers go through `ha_actions.handle` (only send path), chat message → Claude draft / typed reply, auto answer 👍/👎 + note (`app/services/feedback.py`, notes go into auto-mode prompts), auto answers off per ad (instructions kept), hits keep/ignore. Commands `/drafts`, `/auto`, `/status`. A BotFather step is unavoidable (Telegram's managed bots need a BotFather-created manager bot). `telegram.typing()` shows "typing…" while `chats.draft_reply` waits for Claude. The Bot API has the token in the URL: a filter on the `httpx` logger drops those log lines (never log Telegram request URLs) |
| Hit screening by Claude | Done (fake backend tested, live CLI call checked). Optional per search agent (`SearchAgent.screen`, `screen_brief` = what exactly is wanted, `screen_min` match/fair/good). `app/services/screening.py`: one `claude -p` call per run for new hits + price drops (max 10, first photo each, price stats of the agent's last 60 days); verdict on `Hit.screen_relevant/screen_deal/screen_reason`, badge on hit cards. Failing hits: `notify(..., push=False)` = log + HA event (with `screen_*` fields), no phone push/Telegram. Claude error → fail open. Own model purpose `screen` (`llm_model_screen`) in Settings → Claude |
| Google Calendar | Done (mocked Google API tests, **not yet live-tested**). `app/services/gcal.py`: owner's own Google Cloud OAuth client of type "Desktop app" (Settings → Google Calendar: upload client JSON, consent, paste back the `http://127.0.0.1:53682/?code=…` address; PKCE; tokens in `data/google-calendar.json`, 600; the consent screen must be "published" or Google drops the token after 7 days). Reads free/busy only (no event titles) → free slots inside a daily handover window → prompt block "Meine freien Zeiten" (reply drafts on by default, automatic answers opt-in via `gcal_in_auto`). The reply schema has `meetup` {start local ISO, minutes, location}, stored in `Draft.meetup` / `Conversation.meetup`. `app/services/meetups.py`: after a send → push/Telegram "Add to calendar" (`WH_CAL_<draft>_<sig>` / `D_CAL`, HMAC-checked); chat page form + "Find appointment in this chat" (Claude extraction). Nothing is written to the calendar without a click |
| Parcel labels | Done (tests incl. a real-Chromium fill test; **carrier sites not live-tested**). No carrier has a label API for private customers, so: `app/services/shipping_tariffs.py` = published AT→AT online tariffs (Post Paketmarke PM45/70/120 + counter-only Päckchen, GLS XS–XL incl. −1 € to PaketShop, Hermes S/M/L paid cash at the shop, DPD S–XL incl. −0.30 € to Pickup shop; checked 2026-10-09, the carrier checkout price is binding). `app/services/shipping.py`: seller chat gets a buyer message that looks like an address → Claude extracts address + estimates parcel → table `Shipment` (draft/labelled/sent/discarded), size/weight confirmed by the user and remembered in `MyAd.parcel`, cheapest online option preselected, push "Address received" with `WH_SHIP_<shipment>_<sig>` / Telegram `D_SHIP`. `app/services/label_fill.py`: opens the carrier's label page as a new tab of the logged-in browser (remote view on /session), collects visible form fields per step, Claude maps them (keyword fallback), Playwright only fills/selects/ticks: never clicks buttons, never ticks AGB/insurance/payment; "Fill this page" on /session after each step; downloads land in `data/labels/`. Settings → Shipping: sender address, carriers, auto-detect |
| Publish ad | Done, live-tested 2026-10-09 (first ad published from batch #2, public and `active`). `app/willhaben/insert.py` (flow from the `/bbx-adin` bundle): form template + category suggestion → `bap/presave` → photos (`iad-upload/atimage/temp`, `…/order-and-associate`) → `bap/save` → cart `{currency:EUR}` → `items {productReferences:[]}` → `checkout {billingData:null}` = `ACTIVATED`. Stops before checkout if `totalPrice > 0` or a `paymentUrl` appears (nothing is ever paid); free private ads with pickup only. Every publish is a click + confirm. Wired on batch cards; single-draft page still uses copy + link |
| MCP server (13 tools, stdio) | Done, tested against the running container |
| Settings | Sections: willhaben account (login status, link to the session page), Claude, Home Assistant, Telegram, Google Calendar, reply rules, chats, my ads, quiet hours; language picker (`ui_lang`). Per-purpose model (`llm_model_chat`, `llm_model_ads`; full ids, CLI alias `haiku` = 4.5); HA OAuth login (`app/services/ha_auth.py`, redirect back to dashboard, long-lived token → `data/ha-token`), push target picked from HA notify services |
| Networking | Optional: `PROXY_VIA`/`PROXY_HOSTS` let the entrypoint map HA's hostname to a reverse-proxy container on a shared network; host-specific settings live in the uncommitted `compose.override.yaml` |
| Dockerfile / compose | Built and running on the home server (2026-10-09): dashboard on host port `DASHBOARD_PORT` (default 8000), noVNC 6080; CI builds `ghcr.io/<owner>/willhaben-agent` (`.github/workflows/docker.yml`); healthcheck healthy, survives restart |
| Claude login | Settings → Claude → "Sign in with Claude" runs `claude setup-token` in a PTY inside the container (app/llm/claude_login.py), token → `data/claude-token` (600), wins over `CLAUDE_CODE_OAUTH_TOKEN` from `.env`. **Live call not yet verified.** Don't restart the container while a login is pending (state is in memory). |
| Connection watchdog | Done (fake tests, **needs live verification**: real HA websocket drop, Telegram outage, push fallback on the phone). `app/services/health.py`, job `health-check` every 5 min, reads only in-app state (no extra calls to willhaben/Claude/HA/Telegram/Google): willhaben login (`logged_in`, `last_check` not older than 3× session-check interval), Claude token + last `claude -p` outcome (`app/llm/base.last_call`, recorded in `ClaudeCLI.complete`), HA URL+token + push-button websocket (`ha_actions.listener`), Telegram `active()` + last Bot API outcome (`telegram.api_health`; 400s ignored), live chat `chat_live_ok()`, Google Calendar client + refresh token (a revoked grant only shows when the calendar is used), free space of `DATA_DIR` (< `health_disk_min_mb`, default 1024). States ok/problem/off/unknown; a problem must show on 2 checks in a row, then one `notify(kind="health")`; recovery of a reported problem once; first sighting is a silent baseline; state in setting `health_state`. If HA or Telegram is itself not ok and the normal push channel uses it, the message goes out on `channel="both"` (new optional `notify(channel=…)`); Telegram sends `health` regardless of the per-kind filter. Home page: row "Connections" with tiles linking to the settings panels |

Tests: `uv run pytest` (all green expected). The checked-in `.venv` is a Windows venv; on the server run tests in the image:
`docker run --rm --entrypoint sh -v $PWD:/src -w /src -e UV_PROJECT_ENVIRONMENT=/tmp/venv willhaben-agent-willhaben-agent -c 'uv sync --frozen -q && uv run --no-sync pytest -q -p no:cacheprovider'`. Lint: `uvx ruff check app tests --select F,E9,B --ignore B008` clean.

## 4. Repository map

```
app/
  config.py            env settings (pydantic-settings); user prefs live in DB table `setting`
  db.py                SQLModel tables: SearchAgent, Hit, Conversation, Message, Draft, MyAd, AdDraft, Setting, EventLog
                       NOTE: SQLModel 0.0.48 requires tz-aware datetimes (utcnow() returns aware UTC)
  main.py              FastAPI: dashboard routes + JSON API under /api (used by MCP); optional HTTP Basic auth
  scheduler.py         APScheduler jobs: per-agent polling, chat sync, session check, daily my-ads check
  mcp_server.py        stdio MCP server, thin httpx client of /api (WH_AGENT_URL, WH_AGENT_PASSWORD)
  __main__.py          `python -m app` → uvicorn on HOST/PORT
  willhaben/
    parse.py           search/detail JSON parsing, browser URL → API URL conversion
    public.py          anonymous httpx client + shared RateLimiter (403/429 → exponential cool-down)
    session.py         Playwright BrowserSession: fetch() inside logged-in page, login check, SSO refresh, request recorder
    account.py         logged-in endpoints; unverified writes raise EndpointUnknown  ← main place for open work
  services/
    search_agents.py   diff + notify (first run = silent baseline)
    chats.py           sync, ad context, draft_reply, send_draft, mark_sent
    my_ads.py          expiry tracking, warnings, reactivation attempt
    ad_creator.py      LLM draft + price stats from comparable search
    notify.py          Home Assistant REST
    gcal.py            Google Calendar OAuth, free/busy → free slots, create events
    meetups.py         appointments agreed in chats → calendar offer (push button, chat page)
    health.py          connection watchdog: status tiles on the home page, warning push on ok → problem
  llm/
    base.py            LLMBackend protocol, get_backend()
    claude_cli.py      `claude -p` wrapper (see §6)
    prompts.py         REPLY_SYSTEM/SCHEMA, AD_SYSTEM/SCHEMA, MEETUP_SYSTEM/SCHEMA
  i18n/                `t()` / Jinja `_()` + German catalog `de/*.json` (English -> German), `ui_lang` resolution
  templates/           Jinja/HTMX pages (English source strings)
docker/                entrypoint.sh, start-vnc.sh, supervisord.conf (xvfb, x11vnc, novnc, app as pwuser)
docs/willhaben-api.md  endpoint reference with verification status
tests/                 fixtures are real willhaben responses; fake_claude.py stands in for the CLI
```

## 5. willhaben interface facts (verified unless marked)

- All JSON under `https://www.willhaben.at/webapi/...`. Required header
  `x-wh-client: api@willhaben.at;responsive_web;server;1.0.0;desktop` (else empty 400).
- Bot protection cookies present: Cloudflare `__cf_bm`, `aws-waf-token`. Anonymous httpx from home IP works.
- **Search**: `GET /webapi/ad-search/search/atz/seo/<browser path without /iad/>?<filters>&sort=1&rows=30`.
  sort 1 newest, 3/4 price asc/desc, 7 relevance. Response `advertSummaryList.advertSummary[]` with
  `attributes.attribute[{name,values}]` (`ADID, HEADING, PRICE, PRICE_FOR_DISPLAY, OLD_PRICE, LOCATION, PUBLISHED(ms), ENDDATE, IS_BUMPED, SEO_URL`).
- **Detail**: `GET /iad/object?adId=<id>` → `__NEXT_DATA__.props.pageProps.advertDetails`; gone ad = HTTP 404 / page `/404`.
  `/_next/data/<buildId>/...json` also works but buildId rotates per deploy.
- **Login**: Keycloak `sso.willhaben.at`, client `bbx-bff`, PKCE; FingerprintJS on login page. Cookie replays from other
  clients die after ~40 min (third-party report) → all account calls run as `fetch()` inside the Playwright page.
  CSRF: cookie `x-bbx-csrf-token` mirrored as header. Silent re-login: `GET /webapi/sso/login?background-login`.
- **Account endpoints**: `GET /webapi/userprofile-service/userprofile/me` (**anonymous = 204 empty**, logged in = JSON),
  `GET /webapi/chat-api/v1/counter`, `GET /webapi/chat-api/v1/conversations?limit=&offset=`,
  `PUT /webapi/iad-messaging/conversation/init/{adId}?chatSystem=chat`, `POST /webapi/iad-messaging/sendrequest/chat`.
- **My adverts**: no JSON call; server-rendered in `/iad/myprofile/myadverts` → `props.pageProps.initialUserAdsList.verticalList[].advertDetails[]`
  (`id`, `description`=title, `startDate`, `endDate`, `advertStatus.id`, `advertEditStatusActionsList` with links such as `/bap/deactivate/{id}`).
- **Unknown (must be captured)**: send reply, reactivate/extend (only offered on expired ads), ad insertion
  (categories, attributes, image upload, publish).

## 6. Claude CLI contract

`app/llm/claude_cli.py` runs, with the prompt on **stdin** and cwd = temp dir (photos copied in as `fotoN.jpg`):

```
claude -p --output-format json --json-schema <schema> --model opus --system-prompt <sys>
       --tools "" | Read  --permission-prompts none --no-session-persistence
       --setting-sources project --strict-mcp-config
```

- Reads `structured_output` from the JSON result; `is_error: true` → `LLMError`.
- Env: sets `CLAUDE_CODE_OAUTH_TOKEN`, **removes `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`** (they would take precedence and bill the API).
- Version + author (`app/__init__.py`, footer): keep in sync with pyproject.toml.
- Never pass `--bare` (ignores the subscription token). CLI version pinned in Dockerfile (`CLAUDE_CODE_VERSION=2.1.286`)
  because docs announce `--bare` as future default for `-p`; re-check before bumping.

## 7. Next tasks, in order

1. **Deploy**: optionally copy `.env.example` to `.env` (at least `DASHBOARD_PASSWORD`; `PUBLIC_URL`, `DASHBOARD_PORT`,
   `VNC_PASSWORD`/`NOVNC_URL` if wanted; tokens are not needed there), `docker compose up -d --build`. Fix any build issues (image tag
   `mcr.microsoft.com/playwright/python:v1.63.0-noble` must match the locked playwright version; claude installer runs as `pwuser`).
2. **Set up in the web UI**: Settings → Claude → "Sign in with Claude", then "Test Claude" returns OK;
   Settings → Home Assistant → connect (OAuth), pick the push target, "Send test" arrives in HA.
3. **willhaben login**: Settings → willhaben account → "Log in to willhaben" opens the session page; open the login page,
   the owner logs in via the remote view; "Check login" shows the nickname.
   Restart container and confirm the session survives.
4. **Captures** (session page → Advanced → API recording; the owner performs actions in the remote view while the recorder
   runs; files land in `data/captures/`). Labels (German slugs, kept as-is): `chat`, `chat-senden` (send), `meine-anzeigen`
   (my ads), `reaktivieren` (reactivate), `anzeige-aufgeben` (place an ad).
5. **Wire endpoints** in `app/willhaben/account.py` from the captures:
   `messages()` (pin the real path), `send_message()`, `my_adverts()` (+ auto-import into `MyAd`), `reactivate()`,
   `publish_ad()` (+ image upload). Update `docs/willhaben-api.md` and add parser tests with redacted capture fixtures.
6. Optional: Playwright UI fallback for sending if the send API needs something not replayable via fetch.

## 8. Verification checklist

- `uv run pytest` green; add a test per newly wired endpoint using a redacted capture as fixture.
- Search agent: new ad appears exactly once and fires one `willhaben_hit` event.
- Chat: draft created, nothing sent until click; sent message visible in the willhaben app.
- Reactivation: test ad with `auto_renew`, after expiry `endDate` in detail JSON moves forward.
- Publish: draft from 3 photos, published ad reachable via `/iad/object?adId=`.
- Google Calendar: connect, "Show free times" matches the calendar; a chat with an agreed time → "Add to calendar" creates one entry.
- MCP: `claude mcp add willhaben -e WH_AGENT_URL=http://<server>:8000 -- uv --directory <repo> run python -m app.mcp_server`, call `status`.

## 9. Constraints and risks

- robots.txt explicitly forbids automated access; account suspension is possible. Keep intervals ≥ 2 min per agent,
  ≥ 2 s between requests, no bulk actions, human approval for every write.
- Rate limiting: one shared `willhaben_limiter()` (app/willhaben/public.py) for anonymous httpx AND the logged-in browser:
  `REQUEST_MIN_GAP_S` (3 s) + `REQUESTS_PER_HOUR` budget (240); waits > 45 s raise `Blocked` instead of hanging. One-off
  lookups (builder filters, details, keyword searches) are cached 2–5 min; agent polls are never cached. Usage shows in the footer.
- Internal paths change (old `/webapi/iad/search` is 404 now). Surface errors per agent in the dashboard; keep fixtures current.
- Captures contain personal chat data: keep them local, never commit them (`data/` is gitignored).
- Repo is `git init`-ed but nothing is committed yet; commit only when the owner asks. Never commit `data/`, `.env` or `compose.override.yaml`; redact new fixtures with `tests/redact_fixture.py`.
