# 22.2-03 — The planner's spot-check (QD10 step 5)

Done by the planner on 2026-10-06, by hand, against each question's cited
`path:line` in the linkwarden checkout at `952ac4540657cae3a67c3ca59433899d2fda8374`
(`C:/Users/Alec/Desktop/code/rag-bench-corpora/linkwarden`). No retrieval was run,
and no retrieval result for linkwarden exists. The ten ids were fixed by the plan
before any question was read: `lw-03`, `lw-06`, …, `lw-30`.

For each one, the planner read the declaration at the cited line and the code
it covers. The questions were checked against the answer key. Their wording was
not judged.

| id | answer key | cited line | result |
|---|---|---|---|
| lw-03 | `apps/worker/lib/preservationScheme/sanitizeHtmlForMonolith.ts` · `sanitizeHtmlForMonolith` | :106, `export default async function sanitizeHtmlForMonolith` | **Confirmed.** Strips unsafe `src`/`href`/`srcset`/SVG href attributes, and CSS `url()` targets, that fail the safe-target check, before the monolith step. |
| lw-06 | `apps/worker/workers/linkIndexing.ts` · `startIndexing` | :63, `export async function startIndexing` | **Confirmed.** Loops over link batches whose `indexVersion` is null or differs from `MEILI_INDEX_VERSION`, and pushes them to Meilisearch. |
| lw-09 | `packages/lib/ssrf.ts` · `isIpAddressBlockedForServerSideFetch` | :204, `export function isIpAddressBlockedForServerSideFetch` | **Confirmed.** IPv4 (including IPv4 mapped in IPv6) against the blocked CIDR ranges, otherwise IPv6 prefixes. Unparseable input is blocked. |
| lw-12 | `apps/web/lib/api/verifyToken.ts` · `verifyToken` | :10, `export default async function verifyToken` | **Confirmed.** Missing user id, then `exp` in the past, then a revoked `accessToken` lookup by `jti`. |
| lw-15 | `apps/web/components/ClickAwayHandler.tsx` · `ClickAwayHandler` | :55, `export default function ClickAwayHandler` | **Corrected** to `useOutsideAlerter` at :28. See below. |
| lw-18 | `apps/worker/lib/preservationScheme/handleMonolith.ts` · `handleMonolith` | :8, `export default async function handleMonolith` | **Confirmed.** Spawns `monolith`, and rejects on a non-zero exit, on empty output ("Monolith produced an empty file") and above `MONOLITH_MAX_BUFFER` MB ("exceeded buffer limit"). |
| lw-21 | `packages/lib/getFormatFromContentType.ts` · `getFormatFromContentType` | :3, `const getFormatFromContentType = (contentType: string)` | **Confirmed.** It is a module-level `const` bound to an arrow function, an allowed form. jpg/jpeg→jpeg, png, pdf, html→monolith, plain→readability; anything else throws "Invalid file type." |
| lw-24 | `apps/web/lib/api/controllers/collections/collectionId/deleteCollectionById.ts` · `updateDashboardSectionLayout` | :177, `async function updateDashboardSectionLayout` | **Confirmed.** It is a module-level named function, called on both paths in the file (:37, :70). It deletes the collection's dashboard section and decrements the `order` of every later section. |
| lw-27 | `apps/web/lib/copyToClipboard.ts` · `fallbackCopyTextToClipboard` | :1, `export function fallbackCopyTextToClipboard` | **Confirmed.** A hidden read-only textarea plus `document.execCommand("copy")`. `copyTextToClipboard` (:23) falls back to it when `navigator.clipboard.writeText` is missing or throws, and the comment there names insecure contexts. The question asks *how* the copy is done in that case, which is the fallback. |
| lw-30 | `apps/web/hooks/useSidebarCollapse.ts` · `useSidebarCollapse` | :3, `export default function useSidebarCollapse` | **Confirmed.** Reads and writes `localStorage` key `sidebarIsCollapsed`. |

## The correction: lw-15

- **Question:** "What decides whether clicking outside a popup closes it,
  especially when the click lands on something layered above it?"
- **Answer key as written:** `ClickAwayHandler` at :55. That component only
  wraps its children in a div and calls `useOutsideAlerter(wrapperRef,
  onClickOutside)` (:65). None of the deciding logic is in its body.
- **What decides:** `useOutsideAlerter` (:28), a module-level named function, an
  allowed form. Its listener ignores clicks inside the wrapper or on
  `[data-ignore-click-away]`. For an outside click, it calls the callback only
  when the clicked element's z-index (from `getZIndex`, :11) is not above the
  wrapper's (`clickedZIndex <= refZIndex`).
  - The listener itself is a function nested inside `useOutsideAlerter`, which
    is not an allowed form.
  - `getZIndex` only computes a number and decides nothing.
- **Corrected key:** path unchanged (`apps/web/components/ClickAwayHandler.tsx`).
  - Symbol: `useOutsideAlerter`.
  - Evidence: `apps/web/components/ClickAwayHandler.tsx:28 — Registers the
    mousedown listener that ignores clicks inside the wrapped element or on
    [data-ignore-click-away] elements, and fires the callback only when the
    clicked element's z-index (from getZIndex) is not above the wrapper's.`
- **What changes:** the question's text, id and set are unchanged. Its
  file-level answer is unchanged, and only the symbol-level key moves.

## Result

Nine of ten are confirmed, and one (lw-15) is corrected in the spec in the same
commit as this record. `--check` is re-run after the correction.
