# UI guide: Tailwind CSS + daisyUI + Alpine.js + htmx

The dashboard keeps its design (sidebar rail, list → detail screens, popovers, command palette), built on
[Tailwind CSS 4](https://tailwindcss.com) and [daisyUI 5](https://daisyui.com) instead of a hand-written stylesheet.
No JavaScript framework and no Node: server-rendered Jinja templates, Alpine.js for state in the page, htmx for
swapping fragments from the server.

## Files

| File | What |
|---|---|
| `app/styles/app.css` | Tailwind entry: daisyUI themes with the design tokens, extra colours, daisyUI tweaks |
| `app/static/app.css` | Built stylesheet (committed, so the Docker image needs no build step) |
| `scripts/build-css.sh` | Builds it with Tailwind's standalone binary and daisyUI's plugin files (pinned, checksummed, cached in `build/`) |
| `app/static/vendor/` | htmx and Alpine.js, served locally |
| `app/templates/base.html` | Shell: rail, meters, popovers (account, usage, Claude, inbox), command palette, phone tab bar, Alpine components |
| `app/templates/_ui.html` | Macros: icons, pills, price ranges, thumbnails, switches, segmented control, chips, empty state, bars, rings, page heading, messages |

After changing classes in a template run `scripts/build-css.sh` (Tailwind only emits classes it finds in
`app/templates`). Class names must appear literally in a template: never build them from pieces
(`lvl-{{ level }}`, `pill {{ tone }}`). Map values to whole class names in a dict, or use a macro from `_ui.html`.

## Colours and sizes

daisyUI's theme slots carry the design tokens; light and dark follow the system or the account menu's toggle
(`data-theme` on `<html>`).

| Design token | Use |
|---|---|
| `--bg` page background | `bg-base-200` |
| `--surface` cards, panels | `bg-base-100` |
| `--border` | `border-base-300` |
| `--text` | `text-base-content` |
| `--muted` | `text-muted` |
| `--hover` | `bg-hover`, `hover:bg-hover` |
| `--brand` / `--brand-soft` | `text-primary`, `bg-primary` / `bg-brand-soft` |
| `--ok` / `--ok-soft` | `text-success` / `bg-ok-soft` |
| `--warn` / `--warn-soft` | `text-warning` / `bg-warn-soft` |
| `--bad` / `--bad-soft` | `text-error` / `bg-bad-soft` |
| `--off` | `text-off`, `bg-off` |
| channel colours | `bg-ch-anonymous`, `bg-ch-account`, `bg-ch-chat` |
| request types (statistics) | `bg-type-search`, `bg-type-detail`, `bg-type-messages`, `bg-type-account`, `bg-type-other` (`fill-type-*` in SVG; light/dark steps in `--wh-type-*`) |
| price levels | `ui.deal_lvl(level, text)` (`bg-lvl-bargain` …) |
| radii 8 / 10 / 12 px | `rounded-field` / `rounded-[10px]` / `rounded-box` |

Body text is 14 px. Small print: `text-[12.5px] text-muted`; uppercase labels: `ui.eyebrow(text)`.

## Former class names → daisyUI / Tailwind

| Before | Now |
|---|---|
| `btn` (filled, brand) | `btn btn-primary` |
| `btn secondary` | `btn` (surface + border, see `app.css`) |
| `btn ghost` / `btn danger` | `btn btn-ghost` / `btn btn-ghost text-error hover:bg-bad-soft` |
| `btn outline-danger` | `btn btn-outline btn-error` |
| `btn sm` / `btn xs` | `btn btn-sm` / `btn btn-xs` |
| `aria-busy` spinner | `<span class="loading loading-spinner loading-xs">` (Alpine `x-show="busy"`), or htmx `busy:` variant |
| `icon-btn` | `btn btn-ghost btn-sm btn-square` |
| `card` / `card pad` | `rounded-box border border-base-300 bg-base-100` (+ `px-5 py-4`) |
| `card > header` | `flex items-center justify-between gap-2.5 border-b border-base-300 px-4 py-3 font-semibold` |
| `pill ok|warn|bad|brand` | `ui.pill(text, tone)` (`dot=false` for `pill plain`) |
| `tag` / `tag buy` | `ui.tag(text, buy)` |
| `seg` | `ui.seg(items)` |
| `chip` / `chip on` | `ui.chip(href, label, count, on)` |
| `switch` | `toggle toggle-primary` (`toggle-sm`) |
| `switch-row` | `ui.switch_row(title, desc, checked, extra)` |
| `field` | `<label class="flex flex-col gap-1.5"><span class="text-[12.5px] text-muted">…</span>…</label>` |
| `input` / `select` / `textarea` | `input w-full` / `select w-full` / `textarea w-full` (inside the page: add `bg-base-200`) |
| checkbox / radio | `checkbox checkbox-primary checkbox-sm` / `radio radio-primary radio-sm` |
| `range` | `range range-primary range-xs` |
| `list-item` (+ `on`) | `grid grid-cols-[52px_minmax(0,1fr)_auto] items-center gap-3 rounded-[10px] p-2.5 text-base-content no-underline hover:bg-hover` (+ `bg-brand-soft`) |
| `img.thumb` / any `<img>` of an ad | `ui.thumb(url, size_classes)`: placeholder when there is no picture or it fails to load |
| `cards` | `grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-3.5` |
| `tile` | `flex flex-col overflow-hidden rounded-box border border-base-300 bg-base-100` |
| `stats` / `stat` | `grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3` / a card with `px-4 py-3.5`, number `text-[26px] font-semibold` |
| `table` | `table table-sm` (header row: `text-[11.5px] uppercase tracking-wide text-muted`) |
| `bubble in|out` | daisyUI `chat chat-start|chat-end` + `chat-bubble` (out: `chat-bubble-primary`) |
| `kv` | `grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-[13px]`, `dt` `text-muted` |
| `two-col` / `side-col` | `grid gap-5 items-start md:grid-cols-2` / `md:grid-cols-[minmax(0,1fr)_340px]` |
| `msg` / `msg error` / `msg warn` | `ui.msg(text, kind)` |
| `empty-state` | `ui.empty(title, text)` |
| `eyebrow` | `ui.eyebrow(text)` |
| `muted`, `meta`, `num` | `text-muted`, `text-[12.5px]`, `tabular-nums` |
| `dialog.modal > .panel` | `<dialog class="modal">` + `<div class="modal-box …">`, opened with `showModal()` |
| `drawer` | `<dialog class="modal modal-end">` or a fixed panel with Alpine `x-show` |
| `drop` (file drop zone) | `rounded-[10px] border-2 border-dashed border-base-300 …`, drag state in Alpine (`:class`) |
| `deal-track` / `deal-needle` | `deal-track` (kept in `app.css`) + an absolutely positioned needle |
| `htmx-indicator` | still works (`app.css`); or the `busy:` variant on the element htmx sends for |

## Layout

Pages pick the layout with top-level sets (outside any block):

```jinja
{% extends "base.html" %}
{% import "_ui.html" as ui %}
{% set layout = "split" %}                  {# list → detail; leave out for one centred column #}
{% set has_detail = conversation is defined %}  {# phone: show the detail instead of the list #}
```

In a split page the content block holds exactly two columns:

```jinja
<div x-init="splitResize($el, 'chats')" class="flex min-w-0 flex-col border-base-300 md:border-r {{ 'max-md:hidden' if has_detail else '' }}">…list…</div>
<div class="flex min-w-0 max-w-[860px] flex-col gap-5 px-4 pt-5 pb-8 md:px-7 md:pt-[22px] {{ '' if has_detail else 'max-md:hidden' }}">
  <a href="/chats" class="btn btn-ghost btn-sm -ml-2 self-start md:hidden">{{ ui.icon('back', 16) }}{{ _("Chats") }}</a>
  …detail…
</div>
```

Messages (`msg`, `error`) appear as a bar on top of single-column pages and as toasts on split pages.
`myads.html` is the reference split page.

## Behaviour

- **Alpine** for state that lives in the page: open/close, selection mode, tabs, previews, counters.
  Register larger components with `Alpine.data('name', …)` inside `document.addEventListener('alpine:init', …)`
  in the page's own `<script>`; small ones inline (`x-data="{ open: false }"`). Hide until Alpine runs: `x-cloak`.
- **Auto-save**: `<form action="…" x-data="autosave">` posts on change (text fields after a pause) with the
  `X-Autosave` header; show the state with `<small x-text="state"></small>`.
- **htmx** where a page swaps server-rendered fragments (`hx-get`/`hx-post` + `hx-target`/`hx-swap`).
  Keep the existing routes; existing `fetch()` code that already works may stay.
- Range selection with Shift-click works for every checkbox with class `sel` (base.html).
- Confirmations: none for anything that runs as a background job (the click is the decision; the element shows
  "Queued: …" with a **Cancel** until the job runs, and the jobs popover has one per queued job). Irreversible jobs
  (delete an ad, mark it sold, send a message, publish) wait a grace period in the queue first (`job_grace_s`,
  5 s; `jobs.submit(grace=True)`), shown as a countdown and as a notice with **Undo**. A `confirm()` stays only
  for local, immediate actions without an undo (remove a chat from the app, clear hits, delete an agent) and for
  the binding PayLivery steps that run on willhaben at once (accept, decline, offer, buy): `onsubmit='return
  confirm(…)'` on plain forms, `hx-confirm` on forms that send through htmx (an `onsubmit` would not stop htmx,
  and an `onclick` confirm next to `hx-confirm` would ask twice).
- Keyboard: Ctrl K / ⌘K / `/` open the command palette, Esc closes popovers.

## Background work and no reloads

Anything that talks to the willhaben account (or takes long) runs as a background job (`app/services/jobs.py`,
lanes `ads` and `chat`); the page never waits for it and never reloads for it.

- **Send in the background:** the form gets `hx-post` plus `hx-target`/`hx-select` (same id) and `hx-swap="outerHTML"`;
  the route keeps redirecting as before (plain forms still work), htmx follows the redirect and swaps in just that
  part. More parts from the same answer: `hx-select-oob="#a,#b"`. Purely saving switches/text: `x-data="autosave"`.
- **Waiting element:** the element (with an `id`) carries `data-pending-key="ad:123"` (`draft:5`, `addraft:7`,
  `refining:7`, `agent:3`, `shipping:<conversation>`, `purchase:9`, `batch:2`; the keys `/jobs` lists under `pending`)
  and shows `ui.pending(key, running_text, queued_text)`: a spinner while its job
  works on it, a clock while it is queued. When its key is no longer pending, `base.html` replaces it by its fresh copy
  (same id) from the page fetched in the background, or from `data-refresh="url"`; gone there = removed here.
  `data-refresh-also="id …"` takes more elements along.
- **Lists that jobs change:** `data-refresh-on-lane="ads"` refreshes the element whenever a job of that lane finished
  (e.g. My ads after "Update").
- **Taking a click back:** `jobs.submit(..., keys=[marker keys], on_cancel=fn)` makes a queued job cancellable
  (`POST /jobs/{id}/cancel`, `jobs.cancel`): `on_cancel` clears the submitter's markers (`pending_action`, a draft's
  "sending"). `ui.pending()` shows the Cancel button by itself for the element's key (`$store.jobs.cancelFor(key)`).
  `link=` names the page with the result: the finished-job notice opens it (and is not shown on that page).
- **Work Claude does for a page** (writing an ad draft, a revision, reading a parcel address, the first message of
  a purchase) is a job in lane `claude` (or `chat` when it ends in a message): the route returns at once, the
  section carries the marker. Nothing a page waits on with a spinner in the request itself.
- **List → detail without a page load:** the rows carry `hx-get` with `hx-target`/`hx-select` of the detail column,
  `hx-push-url="true"` and `hx-trigger="open-row"`; the page's Alpine click handler fires that event
  (`htmx.trigger(row, 'open-row')`) on a plain click on a wide screen and leaves the link alone otherwise (phones,
  Ctrl/Shift-click, selection mode). Never `hx-trigger="click[…]"` on a link: htmx cancels the link before the filter
  runs, so a rejected click does nothing at all. The chat list swaps `#chat-pane` (+ `#chat-list-items`, `#chat-live`
  out of band), My ads swaps `#myads-detail`.
- **Swapped fragments and Alpine:** when htmx settles a swapped element that has an `id`, it copies the new element's
  attributes over the old ones, which undoes an Alpine `x-show`/`:class` on it. Inside swapped regions, show or hide
  by CSS (`group-has-[…]`), or keep Alpine state on elements without an id.
- **Flash messages after a swap:** every page renders the toast container `#flash`; a fragment that may carry a
  message adds `#flash` to its `hx-select-oob`. On a one-column page a full load shows messages as the bar on top and
  a request from htmx as toasts (`base.html` looks at the `HX-Request` header). Messages that only restate the click
  are not sent on htmx paths: the card shows the new state (routes get the `request` and drop the text; errors stay).
- **A whole page section as one unit** (the agent builder, the batch page): `hx-boost="true"` on a wrapper with
  `hx-target`/`hx-select` of its own id and `hx-push-url="true"`; links to other pages and forms that lead away get
  `hx-boost="false"`. Event handlers for content inside such a region are delegated (`document.addEventListener`)
  so a fresh copy works without binding.
- An element the user is editing (focus, or a value different from the rendered one) is never replaced; a hidden tab
  waits until it is shown (fetching a chat page marks its messages read on willhaben).
- The rail (phones: a button above the tab bar) shows what runs, what is queued and the last results.

## Rules for templates

- No `style="…"` except for data-driven values (a width in %, a channel colour from Python).
- Keep ids, `name`s, form actions, routes and every visible text: tests and scripts rely on them.
  Translatable strings stay word for word (`app/i18n/de/*.json`); a new string needs a German entry.
- `ui.css` and `legacy.css` are gone: none of the former class names (`row`, `muted`, `pill`, `seg`, `list-item`, …) in templates.
