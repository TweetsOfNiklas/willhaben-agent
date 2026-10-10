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
`ORGID`, `categorytreeids` (category path as ids, root → leaf, e.g. `"2691;2722;2724;5013241"`). **V** (example:
`tests/fixtures/search_fahrrad_rows3.json`)

`navigatorGroups[].navigatorList[]` are the filters of the result list (label, type HIERARCHICAL/RANGE/STANDARD, values with
`label`, `hits` and a result-list link, selected values with a reset link). The navigator with `id: "category"` is the
category tree for the current search; following a value's link narrows the search to that category. **V** (used by the
search-agent builder and the price-check category selector)

## Ad detail (anonymous)

- `GET /iad/object?adId=<id>` redirects to the SEO URL. The page contains
  `<script id="__NEXT_DATA__">` with `props.pageProps.advertDetails`. **V**
- Lighter variant: `GET /_next/data/<buildId>/iad/kaufen-und-verkaufen/d/<seopath>.json?seopath=<seopath>`.
  `buildId` is in `__NEXT_DATA__.buildId` and changes with every deploy (on 2026-10-09 it went from
  `1057938-cb2ab3f0` to `1058731-20d7e775` within one hour). **V**
- Important fields: `id`, `description` (title), `startDate`, `endDate`, `publishedDate`,
  `firstPublishedDate`, `advertStatus`, `attributes` (`DESCRIPTION` with `<br/>`), `advertImageList`,
  `advertAddressDetails`, `sellerProfileUserData`, `loginId`, `loginUUid`, `chatEnabled`, `breadcrumbs`,
  `advertEditStatusActionsList`, `categoryTreeId` (the leaf category, same ids as `categorytreeids` in search results).
  **V** (example: `tests/fixtures/detail_1662146981_nextdata.json`)
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
| Activate a deactivated ad | `PUT /webapi/<serviceName><relativePath>` of my-adverts link `4`, no body | V (bundle) |
| Put a passive ad online again | "Passiv" (`advertStatus{id:"passive", statusId:60}`): deactivated by hand it offers link `4` (row above); otherwise it has **no** `4`, only `6` "Neu veröffentlichen" (`serviceName` `iad`, `relativePath` `/bap/copy/{id}`), `requestBillLink` and `3`, and comes back like an expired ad (row below) under a new id | V (links without `4`, 2026-10-09) |
| Renew an expired ad | `POST /webapi/<svc><path>` of link `6` (REPUBLISH) → JSON with a **new** `adId` (insertion draft), then the normal insertion flow from "Save" (see "Placing an ad"); the renewad page's "Neu veröffentlichen" uses the same call | V (bundle), first real renewal pending |
| Deactivate / delete own ad | link `2` `PUT` / link `3` `DELETE`, no body; bulk variants `PUT`/`DELETE` with `{bulkChangeAdIds:[…]}` via the list's BULK_* links; delete reasons optionally via `GET/POST /webapi/iad/deletereasons?adId=` | V (delete used live) |
| Edit own ad | `GET /webapi/iad/bap/{id}` (editor JSON) → `PUT /webapi/iad/bap/edit` with the full object | V (read) / ? (save) |
| Photos of own ad | `GET` link `imageList` (`/webapi/iad/atimage/{id}`) → `advertImage[]` with `reference` + thumbnails; new files `POST /webapi/iad-upload/atimage/temp`; then one `PUT /webapi/iad-upload/atimage/{id}/order-and-associate` `{advertImage:[{reference}], floorPlans:[…]}` (left-out photos are removed, first = cover) | V (read) / ? (save) |
| Mark sold / reserved / unreserve | `PUT /webapi/<serviceName><relativePath>` of the ad's my-adverts links `setAdSold` / `markAdReserved` / `removeMarkAdReserved`, no body; sold reason first via `/webapi/iad/soldreasons?adId=` (see below) | V (bundle), needs live verification |
| Create ad (categories, attributes, image upload, publish) | see "Placing an ad" below | V (pickup) / ? (shipping) |

Conversation list fields according to willmehr: `conversations[]` or `data[]` with `id`/`conversationId`,
`adId`, `adTitle`, `partnerName`, `lastMessage{text, createdAt}`, `unreadCount`. The parser in
`app/willhaben/account.py` accepts several variants.

## Reading the web-app bundles / capturing requests

The pages for chat, my ads and ad creation are separate Next.js apps (`/bbx-lead`, `/bbx-adin`) whose page HTML needs a
login, but their JavaScript chunks are public static files. Sending chat messages, placing ads and all own-ad actions in
this document were read from those chunks (find the chunk names in the logged-in page or a capture, download them
anonymously, search for the action ids). To see real request/response shapes, capture them:

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

## Placing an ad (insertion app `/bbx-adin`, 2026-10-09)

Free private marketplace ad (`adTypeId=productId=67`), every call as `fetch()` in the logged-in page
(`app/willhaben/insert.py`). **V** = first ad published live on 2026-10-09 with pickup only.

| Step | Call | Status |
|---|---|---|
| Form | `GET /iad/anzeigenaufgabe/marktplatz?adTypeId=67&productId=67` (HTML) → `__NEXT_DATA__.props.pageProps`: `ad` (template with location, contact, `contextLinkList`), `categoryTree` (`categoryNode` with `children`/`attributeReferences{code,required}`, `attributes[].treeAttributeElement{code,label}` + `values[]`), `prefillAttributes`. The app caches it ~1 h for the category search; publishing always loads it fresh | V |
| Category suggestion | `GET /webapi/iad/bap/categorysuggestion/attributetree?title=…&adTypeId=67&productId=67` | V |
| Presave | `POST /webapi/iad/bap/presave` (ad JSON) → `adId`, `adUuid`, `contextLinkList` (non-public draft) | V |
| Photos | `POST /webapi/iad-upload/atimage/temp` (multipart `file`) → `reference`; `PUT /webapi/iad-upload<imageList link>/order-and-associate` `{advertImage:[{reference}], floorPlans:[]}` | V |
| Save | `POST /webapi/iad/bap/save` (link `selfLinkFinish`/`selfLinkSave`) | V |
| Cart | `POST /webapi/iad/products/privates/atz/<adUuid>/carts` `{currency:"EUR"}` → `PUT …/carts/<id>/items` `{productReferences:[]}` → `totalPrice` → `POST …/carts/<id>/checkout` `{billingData:null}` → `status:"ACTIVATED"`. The app stops if `totalPrice > 0` or a `paymentUrl` appears | V |

### Handover and shipping ("Übergabe & Versand", PayLivery) – from the bundle only, **needs live verification**

Page `/iad/anzeigenaufgabe/marktplatz/versandoptionen` (edit: `/iad/anzeige-bearbeiten/marktplatz/versandoptionen`),
between save and the cart page `zusatzprodukte`. Read in bbx-adin buildId `1058731-20d7e775`, chunk
`9691-e1ac02b24a890a29.js`: hook module 41117 (`saveBapAdWithDeliveryOption`, `getP2PPOptions`, `handleFormSubmit`),
API module 54527, constants 76697/35688, form 45421. Nothing of this was sent to willhaben by us.

- Attribute `Uebergabe` (tree attribute) with value codes **`PICKUP`** (Selbstabholung) and **`Versand`** (not
  `SHIPPING`); both together = pickup or shipping. The page replaces the attribute in `treeAttributes`:
  `{attributeNode:{code:"Uebergabe"}, selectedAttributeValueNodes:[{code:"PICKUP"},{code:"Versand"}]}`.
- Ticking "Versand": `PUT /webapi/iad/bap/edit` (ad JSON incl. `adId`, function `Ih`), then from the returned ad's
  `contextLinkList` the link `p2ppUserOptions` → `GET /webapi/<serviceName><relativePath>` (function `tQ`) =
  PayLivery options:
  `deliveryOptions[] {uuid, parcelSize{size: XS|S|M|L, longName, additionalDescription}, carrier{description: Post|dpd|Hermes},
  price, depositoryPrice, shippingAddOns[{uuid}], descriptionShort, parcelInsurance, carrierInfoText}`,
  `selectedDeliveryOptions[] {deliveryOptionUuid, isOverSizePackageChosen, isFragileLabelChosen}`, `autoSelectPayLivery`,
  `attributes.labelTrackingNumber`, `sellerOnboardingDTO` (seller still has to set up PayLivery), `qrCodeInformationDto`,
  `payLiveryInactiveDTO`. Add-on uuids: oversize `1b27afa3-8b38-49f5-b635-85c7e905f704`, fragile
  `e87f3937-5a2d-4e63-b1f0-0102f2862007`.
- "Weiter" (`handleFormSubmit`, function `o4`): `PUT /webapi/iad/bap/edit` again (same ad) and
  `PUT /webapi/p2p-aggregator/restapi/v2/p2pp/options/delivery` `{adId, deliveryOptions:[<deliveryOption uuid>, …add-on uuids]}`;
  `[]` = shipping without PayLivery (or the user is blocked for PayLivery). Then the cart page as above.
- `POST /webapi/p2p-aggregator/restapi/v2/p2pp/onboarding/views` only records that the onboarding hint was seen (not used).
- The buyer pays the PayLivery shipping; the private seller confirms "nichtsteuerpflichtige Privatperson" (text only,
  no extra field in the calls).

The page edits the ad it loaded from willhaben (`{...ad, treeAttributes}`), so the app first reads the saved ad
(`GET /webapi/iad/bap/{id}`, keeps photos and other server state) and only replaces `Uebergabe`.
The app (`insert.set_delivery`) does edit → options (only with PayLivery) → delivery PUT, skips the second identical
edit, picks the option by parcel size (and carrier, else the cheapest) and stops before the cart if PayLivery onboarding
is missing or no option matches. Pickup-only ads skip this step entirely (the live-verified path). A retry that switches a draft whose delivery
options were already set back to pickup is refused (clearing them, e.g. `deliveryOptions: []`, is unverified).

### Chat system notices (PayLivery) – **unverified, no live recording yet**

willhaben's own chat notices are messages with a `type` ending in `system_message` or `integration_message`; neither
side wrote them. `parse_messages` (app/willhaben/account.py) tags them with `kind` "system" or "integration", collects
every `http(s)` string inside the message object as `raw_links` and keeps the structured `parameters` as
`ChatMessage.params` (stored as `Message.params`). The chat sync stores them with `Message.kind` set and direction
"unknown"; the chat page shows them as a centred notice line.

**State per chat** (`Conversation.paylivery`, app/services/paylivery.py): `role` (seller | buyer), `stage`, `amount`,
`offer`, `deadline` (ISO), `tracking`, `label_url`, `updated_at`, `history` (newest 30: `at`, `stage`, `text`) and
`applied` (ids of the notices already applied, newest 200; a notice is applied once, so a retried notice never adds
history twice). A stage never goes back once the buyer has paid (`RANK`); "problem" and "cancelled" always apply, and
offers may be sent again until payment.

**Stage patterns** (`detect_stage`, most advanced first; matched on the text plus every string of `parameters`;
a match preceded by "nicht", "kein…" or "un" is ignored):

| stage | matches (examples, unverified) |
|---|---|
| problem | "Problem gemeldet", "Reklamation", "Streitfall" |
| cancelled | "storniert", "abgebrochen", "zurückgezogen" |
| paid_out | "ausgezahlt", "Auszahlung", "überwiesen" |
| received | "Erhalt bestätigt", "Ware erhalten/angekommen" |
| delivered | "zugestellt", "geliefert", "ausgeliefert" |
| shipped | "verschickt", "versendet", "unterwegs", "abgeholt von" |
| label_ready | "Etikett/Paketschein/Versandlabel … bereit/verfügbar/steht", or a label-like link with those words |
| paid | "bezahlt", "Zahlung eingegangen/erhalten", "Kauf abgeschlossen" |
| offer_declined | "Preisvorschlag … abgelehnt/abgesagt", "nicht angenommen" |
| offer_accepted | "Preisvorschlag … angenommen/akzeptiert"; seller: "gekauft" (buy-now sale agreed) |
| offer_sent / offer_received | "Preisvorschlag", "Vorschlag", "Angebot" (not "Angebote"); the words "gesendet" / "erhalten" decide, otherwise the role does (buyer: sent, seller: received) |

Generic help text (e.g. "Dank QR-Code kannst du … versenden", "Das Versandetikett lässt sich …", the buy-now promo
"Fixpreis-Angebote") matches nothing; `tests/test_paylivery_process.py` pins this.

Amounts: "€ 12,34", "12,34 €", "EUR 12,34" or a numeric `amount`/`betrag`/`preis`/`summe` parameter (offers store the
amount as `offer`). Deadlines: "bis 12.10." (year from the notice date, next year if the day has passed) and
"innerhalb von 7 Tagen" (counted from the notice). Tracking numbers: after "Sendungsnummer", "Paketnummer" or
"Tracking" (8–30 characters, at least one digit). Label links: https on willhaben, containing "label", "paketschein",
"etikett", "qr" or ".pdf".

**What runs from a notice**: every notice updates the state of its chat (both roles). For seller chats only, a paid
notice marks the chat's shipment paid (`paid_via` "paylivery", amount from the text) and `shipping.after_paid` runs; a
label link is fetched through the logged-in browser session and a response starting with `%PDF-` is saved as
`DATA_DIR/labels/shipment-<id>-paylivery.pdf`. Buyer chats never create or mark a shipment. A push ("PayLivery: …") goes
out only for a stage change of a notice from the last 3 days, and only if the setting is on.

Unverified: the wording of the notices, which field carries the amount, the deadline and the label link, and whether
the label URL is reachable with the browser session. The patterns are generous on purpose; a false match changes the
state (fixable on the chat page only by a later notice) or records a payment that has to be corrected in the
database (`shipping.unmark_paid`, no button yet). A notice that could not be applied to a shipment yet (no shipment,
no PDF) is retried on the next sync until it is 14 days old; older ones are only marked as seen.

**Actions** (chat page card, `POST /chats/{id}/paylivery/{action}`). Local, no willhaben call: `label` (fetch the
notice's label link into the shipment) and `mark_shipped` (stage "shipped" in this app). willhaben actions, each run
by `paylivery.run_action` in the app's browser window (`/session` is the remote view) with the API recording switched
on (`start_capture("paylivery-<action>")`, stopped at the end, the file name is in the result message):

| action | page | final button (the only commit click allowed for it) |
|---|---|---|
| accept | chat | annehmen / akzeptieren |
| decline | chat | ablehnen |
| receipt | chat | Erhalt bestätigen / bestätigen |
| problem | chat | Problem melden |
| offer | ad page | Preisvorschlag senden (amount typed by the app, never by Claude) |
| buy | ad page | Sofort kaufen / Jetzt kaufen / Kaufen / Bezahlen / zahlungspflichtig |

Each step: the visible fields and buttons are collected (passwords, card and bank fields are never offered), Claude
answers with fills, one button to click and a status (continue | done | need_human | abort), at most 10 steps. Every
click is checked: a commit button of another action, a deny-listed word (löschen, abmelden, registrieren, Konto,
Bedingungen, Cookie consents …) or an unlabelled element is refused. Terms and consent checkboxes are never ticked, card
fields are only handed to `label_buy.fill_card_fields` when that helper exists, and a payment check (3-D Secure, TAN,
another website) stops the step: the page stays open in the remote view and the chat shows why. `done` counts as success
only when this run pressed the action's commit button. Buying and offers need the setting "Allow purchases and offers as
a buyer" and PayLivery on the ad; a buy-now purchase also needs the ad's buy-now flag (`advertDetails.p2ppOptions.
buyNowInformationDto.isBuyNowAd`); PayLivery on the ad is `advertDetails.advertisingParameters.payliveryEnabled`
(a list, "true"/"false"). Each purchase or offer asks for a confirmation in the browser.

**Recording the real calls** (to replace the Claude-guided steps by fixed requests later): as the buyer, record
"paylivery-buy" once through a purchase you can cancel before paying (or a test ad) and "paylivery-offer" through an
offer; as the seller, record "paylivery-accept" and "paylivery-decline" through an offer. The label download and
"receipt" are not app-recorded yet: record them by hand (Session → API recording, labels `paylivery-label` and
`paylivery-receipt`). Send the capture files; each one has
the request and response bodies of the willhaben calls that the action made, so the endpoints, request bodies and
notice shapes can be written down here.

To record a notice: Settings → willhaben account → Advanced → API recording with the label `chat`, then in the browser
window open a conversation of a sold ad where willhaben shows the PayLivery notice. Stop the recording and add the
notice's JSON shape (type, text, parameters, link fields) to this section.
