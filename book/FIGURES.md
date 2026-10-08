# Adding diagrams to the book — brief for diagram agents

The book is assembled from HTML fragments in `book/src/*.html` (see `book/CONTRACT.md` for the general
component classes). The user asked for more diagrams "like the who-writes-what block diagram", placed at
the right spots. Your job: read your assigned chapters, then insert NEW figures where they genuinely help
understanding. Edit only your assigned files.

## Reference example — study it first
`book/src/05-01.html`, block starting at `<h3 id="p5-39-who-writes-what">` (Chapter 39, Figure 39.0).
It is an interactive step-through diagram (`.flow`). Copy its structure and visual style.

## Two kinds of figure

### 1. Interactive step-through (`.flow`) — for any sequence where "who does what, in which order" matters
```html
<div class="flow" id="pN-flow-UNIQUE">
  <div class="legend-roles"><span><i class="sw"></i>Software / host starts it</span><span><i class="dev"></i>Device starts it</span>...</div>
  <figure class="diagram"><svg viewBox="0 0 1000 H" role="img" aria-label="...">
     ... static boxes ...
     <g class="fstep sw" data-step="1"> arrow path + label + badge </g>
     <g class="fstep dev" data-step="2"> ... </g>
  </svg><figcaption>Figure N.x — ... <span class="tag conceptual">conceptual</span></figcaption></figure>
  <ol class="flow-steps">
    <li data-who="sw"><b>Short step title</b><div class="flow-detail"><p>What happens, in plain words.</p>
       <div class="flow-meta"><span>Where: ...</span><span class="pc">PCIe: Yes, host → device · MWr, 4 bytes</span></div>
       <div class="flow-ex">example values (synthetic)</div></div></li>
    ...
  </ol>
</div>
```
- One `<li>` per step, in order; `data-step="n"` on the SVG group(s) for step n (a group may list several: `data-step="2 3"`).
- `data-who` ∈ sw (software/host CPU/driver/initiator), dev (device/SSD/endpoint/target), in (internal, not on the wire),
  pcie (link-level, e.g. DLLPs), mem (memory). The page script adds step buttons, prev/next and "show all arrows".
- The `<ol>` must read well on its own (it is the print/no-JS version): every step complete in words.
- Use 4–10 steps. Give concrete synthetic example values in `.flow-ex`.

### 2. Static block / structure diagram — for layouts, hierarchies, comparisons, maps, state machines
`<figure class="diagram"><svg ...>...</svg><figcaption>Figure N.x — ...</figcaption></figure>` with a
`legend-roles` line above it if colours carry meaning.

## SVG vocabulary (defined in book/shell.html — never hard-code colours)
- Boxes: `class="b-sw"` (software/host), `b-dev` (device), `b-mem` (memory/RAM), `b-pcie` (link band),
  `b-plain` (neutral), `b-hot` (highlighted slot); zones (dashed outlines): `z-sw`, `z-dev`.
  Older generic classes still exist: `box`, `box accent`, `box alt`, `ln`, `lbl`, `lbl small`.
- Arrows: `<path class="ar sw" d="..." marker-end="url(#m-sw)"/>` — colours sw/dev/mem/pcie/in, markers
  `#m-sw #m-dev #m-mem #m-pcie #m-in` (also `marker-start` for two-way). `ar in` is dashed.
- Badges: `<g class="badge dev"><circle cx cy r="11"/><text x y+4>3</text></g>`.
- Text: `t-title` (bold caps), `t-sub` (muted small), `t-lbl` (label), `t-mono` (mono small), `alab sw|dev|in|pcie|mem`
  (coloured arrow label), colour-only fills `c-sw c-dev c-mem c-pcie c-in`. Plain `<text>` inherits the theme colour.
- Charts: draw bars to scale with one scale; label values; colour from tokens (`c-*` classes or `b-*`).

## Layout rules (the screenshots are checked)
- viewBox width 1000 (or 820 for smaller figures); leave ≥ 12 px margin; text ≥ 11 px.
- No text overlapping lines or other text; route arrows around boxes; keep arrow labels on clear background.
- Arrow labels short (≤ 28 chars); the details go into the step list.
- Every figure: numbered caption "Figure <chapter>.<letter or next number> — ...", plus a tag
  (conceptual / synthetic / verified layout). Check existing figure numbers in the chapter and don't duplicate;
  new figures may use suffix letters, e.g. Figure 12.1, 12.2 or 16.0a.
- Insert at the most relevant spot (usually right after the heading/paragraph that introduces the idea), with a one- or
  two-sentence lead-in paragraph. Do not delete existing content. Don't duplicate an existing figure that already shows
  the same thing — improve coverage instead.

## Truth rules
Same as CONTRACT.md: synthetic values labelled, no invented spec section numbers, well-known facts only
(doorbell offsets: SQy tail = 1000h + (2y)·(4<<DSTRD), CQy head = 1000h + (2y+1)·(4<<DSTRD)); flag anything uncertain.

## Verify before finishing
1. `python3 /home/user/Making-Hands-Dirty-With-Nvme-/book/check.py <your files>` → OK.
2. `cd /home/user/Making-Hands-Dirty-With-Nvme-/book && python3 build.py` (others may build concurrently — fine).
3. Screenshot each new figure and LOOK at it, fix overlaps/clipping, in light and dark:
   `python3 /tmp/claude-0/-home-user-Making-Hands-Dirty-With-Nvme-/53eb0212-5ff4-5485-9584-aad527e78f95/scratchpad/figshot.py '#ELEMENT-ID' /tmp/claude-0/-home-user-Making-Hands-Dirty-With-Nvme-/53eb0212-5ff4-5485-9584-aad527e78f95/scratchpad/shots/NAME.png [light|dark]`
   (give static figures an id on the `<figure>` so you can screenshot them; the script clicks "Show all arrows" for flows).
   Use the Read tool on the PNG to view it. Rebuild before each screenshot.
4. Do NOT git commit. Report: files edited, figure ids, chapter, one line each.
