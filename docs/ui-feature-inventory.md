# UI feature inventory and parity review

The UX refresh is confined to templates, presentation, browser state and read-only view context. All production actions use their existing routes. Original quote text remains read-only. Counts, labels, readiness and disabled reasons use the existing application results; the UI does not invent completion percentages or timestamps.

## Library and workspace

| Area | Existing controls retained | New location / parity |
| --- | --- | --- |
| Library `/` | DOCX upload to `upload_book`, max-size/file checks, drag/drop, progress/errors/duplicate handling, local import polling | Native upload disclosure; all upload IDs and hooks retained. Open on an empty library. |
| Library pagination | Independent `local_page` and remote `page`, storage/database errors | Both paginators retained. Browser search/filter explicitly applies to books on the currently loaded pages. |
| Library remote status | Title, author, remote state, chapter/quote counts, last publication | Same-ID local/remote books are shown once with expandable Supabase status. Remote-only entries retain their table. Title matching is never used. A receipt confirms an earlier transfer, not that subsequent edits are synced. |
| Book `/books/local/{id}` | Original filename, size, upload date, receipt/revision | Word-document disclosure in the overview. |
| Import | `extract_book`, retry, job progress, import warnings | Overview/import area. Live progress remains visible independently of the selected section. |
| Chapter correction | `correct_chapters`, revision, starts, titles, heading counts, add/remove row, manual confirmation | Content section, `#chapter-review`. It opens when directly linked. |
| Analysis | `analyze_book`, providers, initial/continue/reuse, failure/stale/empty warnings, job polling/counters | Dedicated KI-Analyse section. |
| Analysis evidence | Rejected source candidates; internal summary, genre/mood/world/characters/spoilers/style/caption suggestion | Existing disclosures under the analysis. |
| Sync | `sync_book` and open-post constraints | Persistent workspace transfer action, with accurate receipt wording. |
| Local deletion | Existing activity blocks, confirmation dialog, cancel, submit, local-only warning | Native Danger Zone disclosure. No deletion rules changed. |

## Quotes and reel/carousel production

| Area | Existing controls retained | New location / parity |
| --- | --- | --- |
| Quote review `/books/local/{id}/chapters/{chapter}` | All six filters (`all`, `usable`, `blocked`, `unsuitable`, `unused`, `used`), counts, paging, usage errors | Chapter sidebar and compact quote cards. |
| Quote evidence | Scores, spoiler classification, suitability reason, exact source paragraph/offsets/context, original full chapter, summary | Compact classification remains visible; detailed evidence and chapter text use native disclosures. |
| Manual quote block | `set_quote_block`, revision/run/filter/page values | Same form/action and states (`Nutzbar`, `Gesperrt`, `Nicht geeignet`). No editable quote text or invented approval state. |
| Reel opener | `reel_workshop`, existing reel state/label, modal loading/close/error handling | Real link usable without JS, enhanced into existing modal with JS. Carousel preparation is mentioned alongside it. |
| Copy | Generate/save caption and image prompt, provider, original quote, character selection, caption preview/count | Step 1 native disclosure. All POST action contracts unchanged. |
| Image | Candidate preview, source selection, Comfy generation/retry, character optimization, upload, stale/disabled explanations | Step 2 “Bild & Carousel”. |
| Prepared quote image | `quote_carousel_source_preview`, `set_quote_carousel_source`, `remove_quote_carousel_source`, schema/sync/error conditions | Step 2, highlighted independent carousel section. 4:5 preview, replacing/removing and quote-vs-chapter fallback messages retained. Can finish a carousel image without audio/video. |
| Music | WAV upload/title, track selection, waveform/drag/loop/play/stop, numeric start/duration, current clip | Step 3. Hidden accordion resize still uses existing ResizeObserver. |
| Video | Generate/save motion prompt, providers, preview, generation/regeneration, stale checks | Step 4. |
| Publication | Preview/caption, stock/requeue distinction, platform snapshot, title/description, daily/scheduled Berlin date, sync/storage requirements | Step 5. |
| Fragment polling | Existing status polling and error handling | Native disclosure selections persist per workshop URL. Dirty non-file/non-hidden form fields survive fragment refresh; hidden revisions always come from the latest server fragment. Successful save clears only the submitted form’s dirty state. |

## Chapter and book teaser

| Area | Existing controls retained | New location / parity |
| --- | --- | --- |
| Song | `upload_teaser_audio`, title/WAV | Production step 1. |
| Chapter analysis | Provider/song/transition, retry/resume/stale/error/progress details | Production steps 2–4 grouped as the actual shared chapter workspace. |
| Chapter media | Batch and per-chapter scene image generation, character refs/selection/optimization, image variant selection, prompts/caption editor, enlarge/lightbox | Same chapter workspace. |
| Prepared chapter fallback | `chapter_carousel_source_preview`, `set_chapter_carousel_source`, `remove_chapter_carousel_source`, readiness/schema/sync gating | Existing per-chapter “Bild für Zitat-Carousels” disclosure retained. |
| Chapter video | Batch/per-chapter generation, text-only update, text/clean previews and MP4 downloads, stale reasons/errors | Same chapter workspace. |
| Chapter publication | Per-chapter title/description/platforms/text-vs-clean/daily-vs-scheduled/requeue, queue availability | Existing chapter publication disclosure retained. |
| Complete teaser | `render_complete_book_teaser`, revision/song/transition/aspect, complete readiness/export activity checks | Production step 5. |
| Advanced edit | Song/aspect/transition, chapter sources/include/crop/duration/focus, stale warning, save/render | Production step 5 native advanced-cut disclosure. |
| Final export | Render stage/time progress/errors, final preview/download, requeue/platform/format restrictions/daily/scheduled publication | Export progress remains globally visible; result/queue in step 5. |

## Settings

Book settings retain one complete save form with revision, suggestion and profile-run IDs. Group switching never disables inputs or splits the save into partial domain updates.

| Group | Retained features |
| --- | --- |
| Buchdaten | Title and author. |
| Kreativprofil | Eight-field profile generator/provider/progress/resume/apply, manual profile fields, AI preview suggestion and spoiler warnings. |
| Art Direction | Global image style and caption guidance. |
| Medien | Carousel end text, overlay title/font/color, background colors, previews, cover/logo uploads and asset revisions. |
| Charaktere | Analysis/provider/status, manual add, name/aliases/description/prompt/evidence/approval, save/revision, generation/regeneration/upload/reference previews. |
| Publishing | Target URL, publication mode and promotion checkbox with readiness errors. |
| Global settings | Connection inventory and tests, Open WebUI model search/selection/save/test, ComfyUI checks, Supabase/R2 storage selection/check, all Instagram/Facebook/YouTube/TikTok options, daily book selection/revision/remote error handling. |

## Progressive enhancement and verification

- Main settings/workspace/production sections are all present and visible in server-rendered HTML. Browser section navigation activates only with JS; “Alles anzeigen” permits cross-section review.
- Hash links and per-path session selection reopen the relevant section across POST redirects and job reloads. Invalid required fields reveal their section and enclosing disclosures before native browser validation focuses them.
- All existing named routes, field names, IDs and data hooks were compared against the original templates by the parent review agent. Actual test results and browser verification are reported in the task handoff.
- The UI reorganization itself leaves backend generation, queue, synchronization, n8n, Supabase migrations and storage contracts unchanged. The separately requested character-reference bugfix is documented below. A separate additive local SQLite table stores image-specific quote-carousel framing with revision fencing; this does not modify the reel image or invalidate running jobs. The existing prepare action uses the saved framing to create the same 1080×1350 JPEG contract.
- Quote-carousel framing supports mouse/touch dragging in a fixed 4:5 frame and keyboard-accessible horizontal/vertical sliders. Local save does not upload; unsaved framing blocks preparation until saved. Remote prepared images stay unchanged until explicit prepare/replace. Selecting another image never inherits a different image's framing.
- Supabase promotion status and carousel last-post timestamp are visible on library rows, separate from the desired local promotion selection. The lower Supabase table remains only for unmatched books or remote errors; pagination stays available.
- Tracked forms show unsaved markers, protect page unload/modal close and defer background page reloads. Comparing values permits undo; successful AJAX saves accept the submitted snapshot, not edits made afterward. No form content is stored in browser storage.
- File selections cannot be copied into replacement modal DOM by browsers; modal polling is deferred while a file or unsaved carousel crop is pending. Other typed values are retained in memory across fragment polling.
- The accompanying character-reference bugfix is separate from the UI reorganization: validated masks and native reference-conditioned editing replace weak identity retention in the masked optimization step. See [character-reference-editing.md](character-reference-editing.md) for input roles, retained features and verification limits. Existing images, selections, storage and queue contracts remain intact.
