# willhaben.at – unofficial interfaces

As of 2026-10-09. willhaben has no public API for private users. Everything here is derived from the
website and can change without notice. **V** = verified by us (live request or read in the JS bundle),
**Q** = from a third-party source (willmehr, Samxel/willhaben-mcp), **?** = open.

## General

- All JSON endpoints live under `https://www.willhaben.at/webapi/...` (server side
  `barbarix-bff.prod.whint.at/webapi`). **V**
- Required header: `x-wh-client: api@willhaben.at;responsive_web;server;1.0.0;desktop`, otherwise HTTP 400
  with an empty body. **V**
- Bot protection: willhaben sets `__cf_bm` (Cloudflare Bot Management) and `aws-waf-token` (AWS WAF).
  Anonymous search and detail requests with httpx from a home IP still worked. **V**
  According to third-party sources, datacenter/VPN IPs get 403 and too many requests get 429. **Q**
- robots.txt explicitly forbids automated access and disallows `/webapi/` as well as parameters such as
  `keyword` and `PRICE_TO`. **V**

## Search (anonymous)

`GET /webapi/ad-search/search/atz/seo/<page path>?<filters>` **V**

The page path is the path of the result list in the browser, without `/iad/`:

| Browser URL | API URL |
|---|---|
| `/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad` | `/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz?keyword=fahrrad` |
| `/iad/kaufen-und-verkaufen/marktplatz/fahrraeder-4552?PRICE_TO=300&areaId=900` | `.../seo/kaufen-und-verkaufen/marktplatz/fahrraeder-4552?PRICE_TO=300&areaId=900` |
| `/iad/immobilien/mietwohnungen/wien` | `.../seo/immobilien/mietwohnungen/wien` |
| `/iad/gebrauchtwagen/auto/gebrauchtwagenboerse?PRICE_TO=5000` | `.../seo/gebrauchtwagen/auto/gebrauchtwagenboerse?PRICE_TO=5000` |

- `sort`: 1 newest, 2 distance, 3 price ascending, 4 price descending, 7 relevance. **V**
- `rows` (page size), `page`; from page 2 on, the website appends `publishedDateTo=<ms>` taken from
  `pagingLinksList.nextLink`. **V**
- Filters repeat the parameter for multiple selections (`areaId=900&areaId=3`). **V**
- The old path `/webapi/iad/search/...` returns 404. **V**

Response: `rowsFound`, `rowsReturned`, `advertSummaryList.advertSummary[]`. Each ad has `id`,
`advertStatus{id,statusId}`, `advertImageList.advertImage[].mainImageUrl|thumbnailImageUrl` and
`attributes.attribute[{name, values[]}]` with, among others, `ADID`, `HEADING`, `BODY_DYN` (truncated), `PRICE`,
`PRICE/AMOUNT`, `PRICE_FOR_DISPLAY`, `OLD_PRICE`, `LOCATION`, `POSTCODE`, `STATE`, `DISTRICT`, `COORDINATES`,
`PUBLISHED` (epoch ms), `CHANGED`, `ENDDATE`, `IS_BUMPED`, `ISPRIVATE`, `SEO_URL`, `ALL_IMAGE_URLS`, `AD_UUID`,
`ORGID`. **V** (example: `tests/fixtures/search_fahrrad_rows3.json`)

## Ad detail (anonymous)

- `GET /iad/object?adId=<id>` redirects to the SEO URL. The page contains
  `<script id="__NEXT_DATA__">` with `props.pageProps.advertDetails`. **V**
- Lighter variant: `GET /_next/data/<buildId>/iad/kaufen-und-verkaufen/d/<seopath>.json?seopath=<seopath>`.
  `buildId` is in `__NEXT_DATA__.buildId` and changes with every deploy (on 2026-10-09 it went from
  `1057938-cb2ab3f0` to `1058731-20d7e775` within one hour). **V**
- Important fields: `id`, `description` (title), `startDate`, `endDate`, `publishedDate`,
  `firstPublishedDate`, `advertStatus`, `attributes` (`DESCRIPTION` with `<br/>`), `advertImageList`,
  `advertAddressDetails`, `sellerProfileUserData`, `loginId`, `loginUUid`, `chatEnabled`, `breadcrumbs`,
  `advertEditStatusActionsList`. **V** (example: `tests/fixtures/detail_1662146981_nextdata.json`)
- Deleted or expired ad: HTTP 404, `__NEXT_DATA__.page == "/404"`. **V**
- Private marketplace ads expire 45 days after publication (`endDate`). **V**

## Login

- Keycloak OIDC: `sso.willhaben.at/auth/realms/willhaben/protocol/openid-connect/auth`,
  `client_id=bbx-bff`, authorization code with PKCE S256, redirect to `/webapi/oauth2/code/sso`. **V**
- The SSO login page loads FingerprintJS. **V**
- Protected pages (`/iad/myprofile/chat`, `/iad/myprofile/myadverts`) redirect with 307 to
  `/iad/myprofile/login?r=...`. **V**
- After login: cookie `BBX_JSESSIONID` plus `x-bbx-csrf-token`; the website sends the latter as a header of
  the same name on every `/webapi` call. **Q** (header name in the bundle **V**)
- Silent re-login via the still-valid SSO session: `GET /webapi/sso/login?background-login`
  (the website loads this in an invisible iframe). **V** (bundle)
- Cookie replays from a different HTTP client became invalid after ~40 min. **Q**
  That is why all account calls of this app run as `fetch()` inside the logged-in Playwright browser.

## Account endpoints

| Purpose | Method + path | Status |
|---|---|---|
| Own profile | `GET /webapi/userprofile-service/userprofile/me` (anonymous: **204 without body**; logged in: profile under `userData` → `loginId`, `uuid`, `firstName`, `emailAddress`) | V |
| Unread chats | `GET /webapi/chat-api/v1/counter` (anonymous 401) | V |
| Conversation list | `GET /webapi/chat-api/v1/conversations?limit=N&offset=0` (anonymous 401) → `conversations[]` with `conversation_id`, `ad_info{id,subject,user_uuid,…}`, `participant_info{name,uuid}`, `unseen`, `last_message{is_mine,preview,timestamp}` | V |
| User statistics | `GET /webapi/iad/user/statistics` | V |
| Watchlist | `GET /webapi/iad/userfolders/all/{userId}` | Q |
| Number of search alerts | `GET /webapi/ad-search/alert/user/activeCount` | V |
| Create conversation for an ad | `PUT /webapi/iad-messaging/conversation/init/{adId}?chatSystem=chat` | V (bundle) |
| First message to the seller | `POST /webapi/iad-messaging/sendrequest/chat` (JSON, fields ?) | V path |
| Contact request by e-mail | `POST /webapi/iad-messaging/sendrequest` | V path |
| Messages of a conversation | `GET /webapi/chat-api/v1/conversations/{id}/messages?limit=100&offset=0` → `conversation_info` (same as list entry) + `messages[]` with `message_id`, `type` (`text` / `integration_message` / `system_message`), `text`, `is_mine`, `read`, `created_at`, `parameters` | V |
| Send reply | REST: ? — sent via the chat WebSocket instead, see below | V (WebSocket) |
| My ads (list, status) | no JSON call: `GET /iad/myprofile/myadverts` (HTML) → `__NEXT_DATA__.props.pageProps.initialUserAdsList.verticalList[].advertDetails[]` with `id`, `description` (title), `startDate`, `endDate`, `advertStatus{id,description}`, `advertEditStatusActionsList` | V |
| Extend / reactivate ad | ? | ? |
| Mark sold / reserved / unreserve | `PUT /webapi/<serviceName><relativePath>` of the ad's my-adverts links `setAdSold` / `markAdReserved` / `removeMarkAdReserved`, no body; sold reason first via `/webapi/iad/soldreasons?adId=` (see below) | V (bundle), needs live verification |
| Create ad (categories, attributes, image upload, publish) | entry point `/iad/anzeigenaufgabe` ("Anzeige aufgeben", place an ad) | ? |

Conversation list fields according to willmehr: `conversations[]` or `data[]` with `id`/`conversationId`,
`adId`, `adTitle`, `partnerName`, `lastMessage{text, createdAt}`, `unreadCount`. The parser in
`app/willhaben/account.py` accepts several variants.

## Capturing open endpoints

The pages for chat, my ads and ad creation are separate Next.js apps that are not served without a
login, so their endpoints are not visible in the public bundle. This is how to capture them:

1. Dashboard → Settings → willhaben account → log in (this opens the session page with the remote browser view).
2. On the session page, start a recording with a matching label (`chat`, `chat-senden` (send chat),
   `meine-anzeigen` (my ads), `reaktivieren` (reactivate), `anzeige-aufgeben` (place an ad)).
3. Perform the action in the remote browser view (or noVNC), e.g. open a conversation and send a test
   message.
4. Stop the recording. The JSON file under `data/captures/` contains method, URL, headers without
   cookies/CSRF, request body and response.
5. Add the endpoint to `app/willhaben/account.py` and update this table.

## Sending chat messages (from the web app bundle, 2026-10-09)

No REST call: the web app sends via the chat WebSocket `wss://chat-realtime-web.willhaben.at/webapi/chat-ws/v1/ws/`
(cookies of the `.willhaben.at` domain, no token). Frames are JSON:

- Send: `{"req_type": "send_message", "request_id": "<uuid>", "payload": {"conversation_id": "…", "client_message_id": "<uuid>", "text": "…", "type": "text"}}`
  → responses with the same `request_id`, `req_type: "send_message"`, `status`: `ack` → `success` | `error`.
- New conversation for an ad: `req_type: "create_conversation"`, `payload: {ad_id, client_conversation_id, message}`.
- Health check: `{"req_type": "ping"}` → `{"req_type": "ping", "status": "success"}` (confirmed live).
- Read receipt: `{"req_type": "mark_message_as_read", "payload": {"conversation_id": "…", "message_id": "…"}}`, no
  `request_id`, fire-and-forget; the web app sends one per unread incoming message (`read: false` in the history) when
  it scrolls into view. The server echoes `mark_message_as_read` with `conversation_id` and `payload{incoming, message_id}`
  (from the chat app bundle `/bbx-lead/…/pages/iad/myprofile/chat/[[...slug]]`, 2026-10-09). Used by `AccountClient.mark_read`.
- Other types: `mark_inbox_as_read`, `mark_inbox_as_unread`, `track_presence`, `update_presence`, `block_user`, …
- Deep link to a conversation: `/iad/myprofile/chat/<conversation_id>` (Next route `[[...slug]]`).

The app verifies every sent message via `GET chat-api/v1/conversations/{id}/messages` (`client_message_id`).

## Mark as sold / reserved (from the my-adverts bundle, 2026-10-09)

Read in the public `/bbx-adin` chunk `pages/iad/myprofile/myadverts` (buildId `1058731-20d7e775`), helper module
60775 and the shared fetch wrapper 43396 (browser base `/webapi`, `Accept: application/json`, `X-WH-Client`, CSRF
header on non-GET). **V** (bundle) — **needs live verification** (no call made yet; response shapes unseen).

- The actions are context links of each ad in the my-adverts list:
  `advertEditStatusActionsList.contextLinkList.contextLink[] = {id, serviceName, relativePath, uri}`. The page calls
  `fetch("/webapi/" + serviceName + relativePath)`, so the path is taken from the link, never built by hand.
  Link ids (enum in chunk 1730): `1` edit, `2` deactivate, `3` delete, `4` activate, `6` republish,
  `setAdSold`, `markAdReserved`, `removeMarkAdReserved`, `changeDeliveryOptions`, bulk ids.
- **Sold** ("Auf verkauft setzen"): `PUT /webapi/<svc><setAdSold.relativePath>` (seen: `/bap/sold/{id}`), no body.
  Before it, only when the ad has a non-empty `deleteReasonQueryParameter`, the dialog loads
  `GET /webapi/iad/soldreasons?adId={id}` → `{soldReasons: [{soldReasonId, description, playAnimation}], header,
  actionHeader, acceptButtonText, footer, banner}`; a reason is a required field and is posted first:
  `POST /webapi/iad/soldreasons?adId={id}` with `{"soldReasonId": "<id as string>"}` (the radio value).
  Irreversible: "Auf verkauft gesetzte Anzeigen können nicht wieder aktiviert oder neu veröffentlicht werden." No undo link.
- **Reserved** ("Als reserviert markieren"): `PUT /webapi/<svc><markAdReserved.relativePath>` (seen:
  `/bap/updatesalesinfo/{id}/5`), no body. **Undo**: same with `removeMarkAdReserved`. Reserved ads have
  `advertStatus.id == "reserved"` (badge with `advertStatus.description`).
- Status enums in the bundle: `advertStatus.id` ∈ sold, passive, active, expired, deactivated, deleted, rented,
  given_away, reserved; `statusId` = main×10 + sub (main 12 = SOLD).
- The ad editor JSON (`GET /webapi/iad/bap/{id}`) has only `selfLinkActivate`/`selfLinkDelete`/`selfLinkSave`/…, no sold link.

Used by `app/willhaben/sales.py` (always after a click + confirm on `/sold/{ad_id}`).

