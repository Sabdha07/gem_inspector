# Genome-scale Model Inspector

A small local Flask + COBRApy UI for inspecting a genome-scale metabolic model.

## What it currently does

1. Upload a local model.
2. Load SBML/XML, JSON, YAML, MATLAB, and supported compressed SBML files.
3. Show basic counts:
   - genes
   - metabolites
   - reactions
   - compartments
4. Reactions:
   - search/filter
   - group/filter by reaction type
   - filter by subsystem
   - show bounds, WT flux, compartments, GPR
5. Metabolites:
   - search/filter
   - filter by compartment
6. Exchanges:
   - identify exchanges using COBRApy
   - show lower/upper bounds
   - WT flux
   - uptake/secretion permission
   - KO growth
   - KO/WT growth fraction
   - essentiality
7. Objective:
   - show linear objective reaction components
   - show coefficient
   - show reactants/products
   - filter by reaction, metabolite, subsystem
8. Exchange debugging (on request, after the initial analysis):
   - pick which essential exchanges to debug from a checklist
   - for each one, trace active-flux pathways from the exchanged metabolite through to biomass
   - shown both with the exchange active ("before KO") and with it reversibly knocked out ("after KO"), including the flux carried by each reaction in the path
   - reports the growth impact of the knockout (WT growth vs. KO growth)
   - currency/cofactor metabolites (H, H2O, ATP/ADP, NAD(H), etc.) are excluded from traced paths by default, with a toggle to include them
9. Compartment-aware naming:
   - a compartment code (`c`, `c0`, `e0`, `[c]`, `(e)`, ...) is mapped to a friendly name (Cytosol, Extracellular space, ...) wherever a compartment is shown
   - a metabolite's displayed *name* has any embedded compartment tag stripped (e.g. "ATP[c]" → "ATP"); its *id* is never touched and still carries the full tag
10. Diet / media selection, applied before the analysis runs:
    - **Model's original bounds** — no change (default)
    - **Complete media** — every exchange opened to unrestricted uptake
    - **Minimal media** — the smallest set of nutrients (via `cobra.medium.minimal_medium`) that sustains the model's own maximum growth rate
    - **Custom diet** — upload a CSV/TSV of `(id, flux)` pairs; ids may be an exchange reaction id, or a metabolite id in a *different* compartment-tag convention than the model itself (e.g. diet says `glc_D_e`, model uses `glc_D_e0`) — entries are matched by compartment-stripped base id, and anything that still can't be matched is reported rather than silently dropped
11. Minimal Medium tab (on request, as soon as the model loads — separate from the "Minimal media" diet option above):
    - non-destructively reports the smallest nutrient set sustaining a chosen **growth cutoff** — any fraction from just above 0% up to 100% of the model's own maximum growth rate, not only 100%
    - never changes the model being analyzed elsewhere in the app, so it can be recomputed for a different cutoff at any time
    - shows each exchange reaction, its metabolite, and the uptake flux required
12. Pathway Tracer (on request, general-purpose — not tied to biomass or exchanges):
    - trace active-flux paths from any starting point (a metabolite, or a reaction resolved to its current-flux products) to any target metabolite
    - configurable "levels" (BFS depth) and max number of paths returned
    - currency/cofactor metabolites excluded by default, same rule and toggle as exchange debugging
    - two view modes: a text/words view, and an interactive, filterable Cytoscape.js network graph (filter by subsystem, minimum flux magnitude, or search/highlight)
13. Network Gaps & Audits (on request, as soon as the model loads):
    - **Blocked reactions** — reactions that cannot carry any flux under the model's current bounds
    - **Dead-end metabolites** — metabolites that can only ever be produced or only ever be consumed given their reactions' current bounds/reversibility
    - **Demand & sink audit** — every demand/sink reaction's bounds, WT usage, and knockout essentiality
    - all three tables sortable and independently exportable to CSV

## Essentiality definition

An exchange reaction is marked essential when its reaction knockout gives:

    KO growth < 0.05 × WT growth

The knockout is implemented by `reaction.knock_out()` inside a COBRApy model context, so the uploaded model is not permanently modified.

## Run

### macOS / Linux

    chmod +x run.sh
    ./run.sh

### Windows

    run.bat

Then open:

    http://127.0.0.1:5000

## Exchange debugging

After the initial analysis (structure + exchange essentiality) finishes, an "Exchange Debugging" section becomes available. Click **Debug Essential Exchanges** on the Exchange essentiality tab, pick which essential exchanges to inspect, and run the analysis. For each selected exchange, the app traces active-flux pathways from the exchanged metabolite to biomass:

- **Before KO** — the pathway as it exists in the current FBA solution.
- **After KO** — the same trace after the exchange is reversibly knocked out (via a COBRApy model context, so the uploaded model is never permanently modified). Flux may reroute through an alternate pathway, or vanish entirely if the exchange is the sole route to biomass.

Only reactions carrying non-zero flux in the relevant solution are followed, so a missing path after KO usually means either the metabolite has no active alternate route, or the model reroutes around the knockout using a pathway that wasn't part of the original solution's basis.

## Pathway Tracer

A separate, general-purpose module, visible as its own tab as soon as the model loads — but it never runs on its own. Pick a starting point and a target metabolite, then click **Run trace**:

- **Start from** — either a metabolite (traced directly), or a reaction (resolved to the metabolite(s) it actually produces under its current flux direction; a reaction with no active flux in the current solution reports an error instead of a trace, since there's nothing downstream to follow).
- **Target metabolite** — any metabolite in the model. The trace stops as soon as it's reached.
- **Max levels** — the BFS depth cap (how many reaction hops the trace is allowed to take).
- **Max paths** — how many distinct paths to return.
- **Hide common cofactors** — same currency/cofactor filter as exchange debugging (H, H2O, ATP/ADP, NAD(H), etc.), on by default. The chosen start and target are never filtered out even if they're on that list.

Results can be viewed two ways:

- **Words** — the same step-by-step path listing style as exchange debugging (metabolite → reaction (flux) → metabolite → …).
- **Network** — an interactive Cytoscape.js graph, laid out by BFS level (start node(s) as roots), with the start/target/intermediate metabolites color-coded. Filterable by subsystem, by a minimum flux-magnitude threshold, and by a text search that highlights and dims non-matching nodes/edges. Hovering a metabolite node shows its id, name, compartment, formula, and charge; hovering a reaction edge shows its id, name, subsystem, type, GPR, bounds, the full stoichiometric reaction string, and the flux it carries in this trace.

Optionally, list one or more reaction or metabolite ids in **Compare knockout** to trace the same start → target a second time with them blocked. A reaction is knocked out directly; a metabolite is "knocked out" by blocking every reaction it participates in as a reactant or product (an approximation, since COBRApy has no single built-in notion of a metabolite knockout — this can be a large effect for a highly-connected metabolite like ATP). When a knockout comparison runs, both views gain:

- A **Before KO / After KO** toggle to switch which snapshot's paths are rendered, plus the WT vs. KO growth and percent retained.
- A **Highlight KO-affected reactions** toggle (on by default) that calls out, for every reaction appearing in either snapshot's traced paths, whether it was blocked by the knockout, is a new reroute that only appears after it, or simply changed flux — as an inline badge in the Words view, and as a colored/dashed edge (red = removed, green = new route, amber = changed) in the Network view.

Only reactions carrying non-zero flux in the current FBA solution are followed, exactly as in exchange debugging — both features share the same underlying active-flux tracing code.

## Diet / media

Choose a diet before clicking **Analyze model**; it's applied to the model's exchange lower bounds (uptake capacity) before any optimization runs, so it shapes everything downstream — WT growth, essentiality, debugging, and pathway tracing. Only lower bounds are touched; secretion (upper bounds) is left as defined in the uploaded model.

A custom diet file needs at least two columns: an identifier and a flux value. Following the VMH diet-file convention, a positive flux is treated as an uptake magnitude (and negated into a lower bound); a non-positive value is used directly as the lower bound. A header row is auto-detected and skipped. The identifier can be:

- An exact exchange reaction id (`EX_glc_D(e)`)
- A metabolite id, in *this model's own* compartment convention (`glc_D_e0`)
- A metabolite id in a *different* compartment convention — underscore (`glc_D_e`), bracket (`glc_D[e]`), or paren (`glc_D(e)`) — matched to this model's actual exchange by compartment-stripped base id
- A bare compound id with no compartment tag at all (`glc_D`), assumed extracellular

Whatever couldn't be matched is listed on the Overview tab rather than silently ignored.

The target growth used for "Minimal media" (both this diet option and the Minimal Medium tab below) is always the model's own maximum growth rate under its own original bounds exactly as uploaded — never a growth rate computed after forcing every exchange open, which would let FBA route flux through byproduct/secretion exchanges run in reverse and inflate the target to a biologically unrealistic value.

## Minimal Medium tab

A separate, read-only, on-request report — visible as its own tab as soon as the model loads, independent of whatever diet was chosen before analysis. Pick a **growth cutoff** (a fraction from just above 0 up to 1.0 — the default 1.0 uses the model's full own maximum growth rate, 0.5 asks for half of it, etc.) and click **Compute minimal medium** to see the smallest exchange-reaction set that sustains that fraction of growth, one row per component with separate **exchange reaction name**, **exchange reaction ID**, **metabolite ID**, **metabolite name**, and **flux** columns — sortable by any of them, and exportable to CSV via the button above the table.

It never changes the model being analyzed elsewhere in the app (see the note on numerical robustness below); recompute it for a different cutoff as often as you like without needing to re-run the initial analysis.

## Network Gaps & Audits tab

A separate, on-request tab — visible as soon as the model loads — with three independent diagnostics, each its own button, each sortable and independently exportable to CSV, none of which ever change the analyzed model:

- **Blocked reactions** — reactions that cannot carry any flux at all, via COBRApy's flux-variability-based `find_blocked_reactions`. Run with the model's real bounds (not artificially opened), so the result reflects the diet actually in effect. Can take a while for large models since it runs flux variability analysis on every reaction that carries no flux in the current solution; runs on GLPK's exact-arithmetic solver internally (see the numerical-robustness note below) to avoid a crash some GLPK builds hit when re-solving the same LP many times in a row.
- **Dead-end metabolites** — a fast, purely structural check (no optimization involved) for metabolites that, given each of their reactions' current bounds/reversibility, can only ever be produced or only ever be consumed — never both. This is often *why* a reaction ends up blocked, though a reaction can also be blocked for more global network reasons a local per-metabolite check can't see, which is why this and the blocked-reactions check are shown together rather than one substituting for the other.
- **Demand & sink audit** — every demand and sink reaction (`model.demands` / `model.sinks` — single-metabolite boundary reactions distinct from exchanges, so they don't appear in the Exchange essentiality tab) with its metabolite, bounds, whether it carries flux in the current FBA solution, and the same reaction-knockout essentiality test used for exchanges (KO growth < 5% of WT growth). A demand/sink that's unused and non-essential is a reasonable candidate for pruning; one that's unused but silently essential can be a sign of an over-permissive reaction papering over a thermodynamically unrealistic loop.

## Numerical robustness note (GLPK on Windows)

Two operations in this app — "Minimal media" (both the diet option and the Minimal Medium tab) and the Network Gaps tab's blocked-reactions check — have been observed to trigger a fatal `glp_free: memory allocation error` crash on some GLPK/Windows builds: one from solving a single very poorly-scaled LP (forcing every exchange wide open), the other from re-solving the same LP many times in a row (flux variability analysis). Because this is a crash inside GLPK's own C code, Python cannot catch or recover from it — it takes down the whole server process. Both operations now run on GLPK's exact-arithmetic interface (`glpk_exact`) instead of the default floating-point solver, which sidesteps both failure modes (COBRApy's own `minimal_medium` docs list "switching to a different solver" as the standard remedy for numerical instability). The trade-off is slower solves for these two specific computations; everything else in the app keeps using the fast default solver.

## Notes

- Exchange/demand/sink classification uses COBRApy's boundary-reaction classification, computed once per analysis rather than per reaction.
- "Transport" is deliberately only a heuristic in this MVP: reactions spanning more than one compartment are shown as transport unless COBRApy classifies them as exchange/demand/sink.
- Large models can take a while during exchange essentiality testing, though each knockout now uses `model.slim_optimize()` (objective value only, no full solution object) to keep the per-exchange cost as low as possible.
- The model stays cached in memory for a short window (10 minutes of inactivity) after the initial analysis so the debug step can reuse it without re-uploading; it's evicted automatically after that.
- For a production version, the next useful step is moving essentiality analysis into a background job with progress reporting and cancellation.
