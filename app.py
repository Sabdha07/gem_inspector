from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import tempfile
import time
import threading
import uuid
from pathlib import Path
from collections import deque

from flask import Flask, jsonify, render_template, request, Response
from werkzeug.utils import secure_filename

import cobra
from cobra.io import (
    load_json_model,
    load_matlab_model,
    load_yaml_model,
    read_sbml_model,
)
from cobra.flux_analysis import find_blocked_reactions
from cobra.medium import minimal_medium
from cobra.util import linear_reaction_coefficients

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

ALLOWED = {".xml", ".sbml", ".json", ".yml", ".yaml", ".mat", ".gz", ".zip", ".bz2"}
MODEL_CACHE = {}  # In-memory cache for models: model_id -> (model, last_touched_ts)
MODEL_TTL_SECONDS = 600  # 10 minutes of inactivity before a cached model is reaped

# Add a cleanup scheduler for old models
def cleanup_old_models():
    """Periodically clean up old models from cache."""
    while True:
        time.sleep(300)  # Cleanup every 5 minutes
        now = time.time()
        expired = [mid for mid, (model, timestamp) in MODEL_CACHE.items() if now - timestamp > MODEL_TTL_SECONDS]
        for mid in expired:
            del MODEL_CACHE[mid]

cleanup_thread = threading.Thread(target=cleanup_old_models, daemon=True)
cleanup_thread.start()

def touch_model_cache(model_id):
    """Refresh a cached model's TTL so it survives while the user is still
    working with it (e.g. moving from KO analysis to exchange debugging)."""
    entry = MODEL_CACHE.get(model_id)
    if entry is not None:
        model, _ = entry
        MODEL_CACHE[model_id] = (model, time.time())

def safe_float(value):
    try:
        x = float(value)
        if math.isfinite(x):
            return x
    except (TypeError, ValueError):
        pass
    return None

def clean(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [clean(x) for x in value]
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    return str(value)

def subsystem_value(reaction):
    # COBRApy models vary: subsystem may be a string or absent.
    value = getattr(reaction, "subsystem", None)
    if isinstance(value, (list, tuple, set)):
        return "; ".join(map(str, value))
    return str(value or "")

# "Currency"/cofactor metabolites that shuttle through almost every pathway
# (energy carriers, redox carriers, protons, water, phosphate...) and tend to
# clutter a pathway trace without adding pathway-specific information.
# Matched against a metabolite's compartment-stripped *base* id, so "atp_c",
# "atp_c0", and "atp_e" all match the single entry "atp" below regardless of
# which compartment they're in. Edit this set to taste.
TRIVIAL_METABOLITES = {
    "h", "h2o", "pi", "ppi",
    "atp", "adp", "amp",
    "gtp", "gdp", "gmp",
    "utp", "udp", "ump",
    "ctp", "cdp", "cmp",
    "itp", "idp", "imp",
    "nad", "nadh", "nadp", "nadph",
    "fad", "fadh2",
    "coa", "co_a",
    "q8", "q8h2", "mqn8", "mql8",
}

# A trailing compartment tag can show up in any of several conventions
# depending on how the model was built: "_c", "_e0", "[c]", "[e0]", "(c)",
# "(e0)". This matches whichever one is present at the very end of an id and
# captures (compartment_code, numeric_index_or_empty) separately, e.g.
# "atp_c0" -> ("c", "0"), "glc_D[e]" -> ("e", ""), "EX_thm(e)" -> ("e", "").
_COMPARTMENT_SUFFIX_RE = re.compile(
    r"(?:_([A-Za-z]+)(\d*)|\[([A-Za-z]+)(\d*)\]|\(([A-Za-z]+)(\d*)\))$"
)

def parse_compartment_suffix(raw_id):
    """Split a trailing compartment tag off an id, in whichever convention
    it uses. Returns (base_id, compartment_code_lower_or_None). The
    compartment code returned excludes any trailing numeric index (so
    "atp_c0" -> ("atp", "c") — the "0" is discarded here since it's just an
    index, not part of the compartment code) — see compartment_display_name
    for where that index is used for display disambiguation.
    """
    match = _COMPARTMENT_SUFFIX_RE.search(raw_id)
    if not match:
        return raw_id, None
    groups = [g for g in match.groups() if g]
    code = groups[0].lower() if groups else None
    base = raw_id[: match.start()]
    return base, code

def metabolite_base_id(met_id):
    """Strip a trailing compartment tag from a metabolite id so it can be
    matched against a compartment-agnostic name list. E.g. "atp_c0",
    "atp_c", "atp[c]", and "atp(c)" all reduce to "atp"."""
    base, _ = parse_compartment_suffix(met_id)
    return base.lower()

def is_trivial_metabolite(met, trivial_bases=TRIVIAL_METABOLITES):
    return metabolite_base_id(met.id) in trivial_bases

# Base compartment code -> friendly display name. Codes are matched after
# stripping any trailing numeric index (see parse_compartment_suffix), so
# "c", "c0", "c1", ... all map to the same entry. Extend this to taste.
COMPARTMENT_NAME_MAP = {
    "c": "Cytosol",
    "e": "Extracellular space",
    "p": "Periplasm",
    "m": "Mitochondria",
    "mm": "Mitochondrial membrane",
    "im": "Mitochondrial intermembrane space",
    "r": "Endoplasmic reticulum",
    "g": "Golgi apparatus",
    "n": "Nucleus",
    "x": "Peroxisome",
    "v": "Vacuole",
    "l": "Lysosome",
    "w": "Cell wall",
    "u": "Thylakoid lumen",
    "cm": "Cytoplasmic membrane",
    "cx": "Carboxysome",
    "flag": "Flagellum",
    "per": "Periplasm",
}

def compartment_display_name(comp_id, model_provided_name=None):
    """Best-effort friendly name for a compartment id/code.

    Prefers the model's own compartment name if it's meaningfully set (not
    blank and not just a repeat of the code). Otherwise looks up the base
    code (compartment-index stripped, e.g. "c0" -> "c") in
    COMPARTMENT_NAME_MAP. Falls back to the raw id, title-cased, if the code
    isn't recognized. A non-zero numeric index (multi-instance compartments,
    e.g. community models with "c0"/"c1" per organism) is appended for
    disambiguation.
    """
    comp_id = comp_id or ""
    if model_provided_name:
        name = str(model_provided_name).strip()
        if name and name.lower() != comp_id.lower():
            return name

    match = re.match(r"^([A-Za-z]+)(\d*)$", comp_id)
    if not match:
        return comp_id.title() if comp_id else "Unknown compartment"
    base, index = match.group(1).lower(), match.group(2)

    friendly = COMPARTMENT_NAME_MAP.get(base, base.title())
    if index and index != "0":
        friendly = f"{friendly} {index}"
    return friendly

def clean_metabolite_display_name(met):
    """Metabolite display name with any embedded compartment tag stripped.

    Some model exports bake the compartment into the metabolite *name*
    field itself (e.g. name="Thiamine[c]" or "Pyruvate_e"), which is
    redundant with the "Compartment" column/badge already shown next to it.
    The metabolite *id* is left completely untouched everywhere in this
    app — only the human-readable name is cleaned here.

    Bracket/paren tags ("[c]", "(e)") are always stripped since a real
    chemical name essentially never ends that way. An underscore tag
    ("_c", "_e0") is only stripped when the code is a recognized
    compartment (matches this metabolite's own compartment, or is a known
    key in COMPARTMENT_NAME_MAP) — this avoids mangling a name that
    legitimately ends in "_D"/"_L" etc. (e.g. "glc__D").
    """
    name = (met.name or met.id or "").strip()
    if not name:
        return name

    match = _COMPARTMENT_SUFFIX_RE.search(name)
    if not match:
        return name

    is_bracket_or_paren = match.group(3) is not None or match.group(5) is not None
    code = next((g for g in (match.group(1), match.group(3), match.group(5)) if g), "").lower()
    own_compartment_base = None
    met_compartment = getattr(met, "compartment", None)
    if met_compartment:
        comp_match = re.match(r"^([A-Za-z]+)\d*$", met_compartment)
        own_compartment_base = comp_match.group(1).lower() if comp_match else met_compartment.lower()

    if is_bracket_or_paren or code == own_compartment_base or code in COMPARTMENT_NAME_MAP:
        cleaned = name[: match.start()].rstrip(" _-")
        if cleaned:
            return cleaned
    return name

def classify_reactions(model):
    """Classify every reaction once and return {reaction_id: type}.

    The previous implementation rebuilt the exchange/demand/sink id sets
    (each an O(n) scan over model.reactions under the hood) on every single
    call, and was called once per reaction — making reaction-type lookup
    O(n^2) for a model with n reactions. Building the sets a single time and
    classifying every reaction in one pass makes this O(n).
    """
    exchange_ids = {r.id for r in model.exchanges}
    demand_ids = {r.id for r in model.demands}
    sink_ids = {r.id for r in model.sinks}

    types = {}
    for reaction in model.reactions:
        if reaction.id in exchange_ids:
            types[reaction.id] = "exchange"
        elif reaction.id in demand_ids:
            types[reaction.id] = "demand"
        elif reaction.id in sink_ids:
            types[reaction.id] = "sink"
        else:
            # Transport is inherently model-dependent. This simple local
            # heuristic flags reactions whose metabolites span multiple
            # compartments.
            compartments = {m.compartment for m in reaction.metabolites if m.compartment}
            types[reaction.id] = "transport" if len(compartments) > 1 else "internal"
    return types

def stoich_rows(reaction):
    rows = []
    for met, coeff in reaction.metabolites.items():
        rows.append({
            "id": met.id,
            "name": clean_metabolite_display_name(met),
            "compartment": met.compartment,
            "coefficient": safe_float(coeff),
            "side": "reactant" if coeff < 0 else "product",
        })
    return rows

def load_uploaded_model(path: Path):
    suffixes = "".join(path.suffixes).lower()

    # Use explicit readers for local files rather than cobra.io.load_model,
    # which is intended for remote repository identifiers.
    if suffixes.endswith(".json"):
        return load_json_model(str(path))
    if suffixes.endswith(".yml") or suffixes.endswith(".yaml"):
        return load_yaml_model(str(path))
    if suffixes.endswith(".mat"):
        return load_matlab_model(str(path))
    # SBML reader supports .gz/.zip/.bz2 when libSBML was built accordingly.
    return read_sbml_model(str(path))

def quick_analyze(model):
    """Fast initial analysis without KO tests."""
    wt_solution = model.optimize()
    wt_status = str(wt_solution.status)
    wt_growth = safe_float(wt_solution.objective_value)

    if wt_growth is None:
        wt_growth = 0.0

    # Basic statistics
    stats = {
        "model_id": model.id,
        "model_name": getattr(model, "name", None),
        "genes": len(model.genes),
        "metabolites": len(model.metabolites),
        "reactions": len(model.reactions),
        "compartments": len(model.compartments),
        "compartment_details": [
            {"id": cid, "name": compartment_display_name(cid, name), "raw_name": str(name)}
            for cid, name in model.compartments.items()
        ],
        "solver": str(model.solver.interface.__name__).split(".")[-1],
        "objective_direction": str(model.objective.direction),
        "wt_status": wt_status,
        "wt_growth": wt_growth,
    }

    # Precompute metabolite -> single-metabolite exchange lookup once, instead
    # of scanning every exchange (and rebuilding a metabolite-id list on every
    # scan) for each objective metabolite below.
    direct_exchange_by_met = {}
    for exch in model.exchanges:
        if len(exch.metabolites) == 1:
            (met,) = exch.metabolites.keys()
            direct_exchange_by_met.setdefault(met.id, exch.id)

    # Objective components
    objective_components = []
    objective_metabolites = []
    try:
        coefficients = linear_reaction_coefficients(model)

        # Collect metabolites from objective reactions, excluding the same
        # currency/cofactor metabolites filtered out of exchange-debugging
        # traces (see TRIVIAL_METABOLITES). Note: an earlier version of this
        # filter stripped the metabolite id with a character-class rstrip(),
        # which (for all-lowercase BiGG-style ids like "atp_c") ate the whole
        # id down to "" and so never actually filtered anything. Using the
        # same compartment-suffix-aware helper as the debug trace fixes that.
        met_coeff_map = {}

        for rxn, coeff in coefficients.items():
            objective_components.append({
                "reaction_id": rxn.id,
                "reaction_name": rxn.name,
                "coefficient": safe_float(coeff),
                "reaction": rxn.reaction,
                "subsystem": subsystem_value(rxn),
                "reactants": stoich_rows(rxn),
                "products": stoich_rows(rxn),
            })
            
            for met, met_coeff in rxn.metabolites.items():
                if not is_trivial_metabolite(met):
                    key = met.id
                    if key not in met_coeff_map:
                        met_coeff_map[key] = {"met": met, "total_coeff": 0.0, "rxn_count": 0}
                    met_coeff_map[key]["total_coeff"] += safe_float(met_coeff) * coeff if coeff else 0
                    met_coeff_map[key]["rxn_count"] += 1
        
        for met_id, met_info in sorted(met_coeff_map.items()):
            met = met_info["met"]
            objective_metabolites.append({
                "metabolite_id": met.id,
                "metabolite_name": clean_metabolite_display_name(met),
                "compartment": met.compartment,
                "coefficient": safe_float(met_info["total_coeff"]),
                "direct_exchange": direct_exchange_by_met.get(met.id),
                "appears_in_reactions": met_info["rxn_count"],
            })
    except Exception as exc:
        objective_components = [{
            "error": f"Objective could not be reduced to linear reaction coefficients: {exc}"
        }]

    # Reaction table + WT flux
    fluxes = wt_solution.fluxes if hasattr(wt_solution, "fluxes") else {}
    reaction_types = classify_reactions(model)
    reactions = []
    for rxn in model.reactions:
        flux = safe_float(fluxes.get(rxn.id)) if hasattr(fluxes, "get") else None
        reactions.append({
            "id": rxn.id,
            "name": rxn.name,
            "type": reaction_types.get(rxn.id, "internal"),
            "subsystem": subsystem_value(rxn),
            "compartments": sorted({m.compartment for m in rxn.metabolites if m.compartment}),
            "lower_bound": safe_float(rxn.lower_bound),
            "upper_bound": safe_float(rxn.upper_bound),
            "reversible": bool(rxn.lower_bound < 0 and rxn.upper_bound > 0),
            "wt_flux": flux,
            "gene_reaction_rule": getattr(rxn, "gene_reaction_rule", ""),
        })

    # Metabolite table
    metabolites = []
    for met in model.metabolites:
        metabolites.append({
            "id": met.id,
            "name": clean_metabolite_display_name(met),
            "compartment": met.compartment,
            "formula": getattr(met, "formula", None),
            "charge": clean(getattr(met, "charge", None)),
        })

    return {
        "stats": stats,
        "reactions": reactions,
        "metabolites": metabolites,
        "objective": objective_components,
        "objective_metabolites": objective_metabolites,
        "wt_growth": wt_growth,
        "fluxes": dict(fluxes),
        "essentiality_rule": "Exchange reaction is essential when reaction KO gives growth < 5% of WT growth.",
        "transport_rule": "Transport is shown using a simple heuristic: reaction metabolites span more than one compartment and the reaction is not classified by COBRApy as exchange/demand/sink.",
    }

def stream_ko_analysis(model, wt_growth, fluxes):
    """Generator that yields KO results with progress updates."""
    exchanges = list(model.exchanges)
    total = len(exchanges)
    
    yield "data: " + json.dumps({"type": "info", "message": f"Starting KO analysis for {total} exchanges..."}) + "\n\n"
    
    essentiality = []
    for idx, rxn in enumerate(exchanges):
        yield "data: " + json.dumps({"type": "progress", "current": idx + 1, "total": total, "message": f"Testing {rxn.id}..."}) + "\n\n"
        
        ko_growth = None
        ko_status = "not tested"
        if wt_growth > 0:
            # Use temporary bounds instead of copying the model (much faster!)
            old_lb = rxn.lower_bound
            old_ub = rxn.upper_bound
            try:
                rxn.lower_bound = 0
                rxn.upper_bound = 0

                # slim_optimize() skips building a full Solution object
                # (fluxes/reduced costs/shadow prices for every reaction) and
                # just returns the objective value, which is all a knockout
                # screen needs. For a model with thousands of exchanges this
                # avoids thousands of unnecessary Solution constructions.
                value = model.slim_optimize(error_value=None)
                if value is None:
                    ko_status = "infeasible"
                else:
                    ko_status = "optimal"
                    ko_growth = max(0.0, safe_float(value) or 0.0)
            except Exception as exc:
                ko_status = f"error: {exc}"
            finally:
                # Always restore bounds, whether or not optimization succeeded.
                rxn.lower_bound = old_lb
                rxn.upper_bound = old_ub

        ratio = None
        essential = False
        if ko_growth is not None and wt_growth > 0:
            ratio = ko_growth / wt_growth
            essential = ratio < 0.05

        met_ids = [m.id for m in rxn.metabolites]
        met_names = [clean_metabolite_display_name(m) for m in rxn.metabolites]

        essentiality.append({
            "id": rxn.id,
            "name": rxn.name,
            "metabolites": met_ids,
            "metabolite_names": met_names,
            "lower_bound": safe_float(rxn.lower_bound),
            "upper_bound": safe_float(rxn.upper_bound),
            "wt_flux": safe_float(fluxes.get(rxn.id)) if hasattr(fluxes, "get") else None,
            "uptake_allowed": bool(rxn.lower_bound < 0),
            "secretion_allowed": bool(rxn.upper_bound > 0),
            "reversible": bool(rxn.lower_bound < 0 and rxn.upper_bound > 0),
            "ko_growth": ko_growth,
            "ko_growth_fraction": ratio,
            "essential": essential,
            "ko_status": ko_status,
            # Extra context for a hover tooltip on the essentiality table —
            # not shown in the table itself, just on demand.
            "reaction_string": rxn.reaction,
            "subsystem": subsystem_value(rxn),
            "gene_reaction_rule": getattr(rxn, "gene_reaction_rule", ""),
        })
    
    yield "data: " + json.dumps({"type": "complete", "exchanges": essentiality, "essentiality_rule": "Exchange reaction is essential when reaction KO gives growth < 5% of WT growth.", "transport_rule": "Transport is shown using a simple heuristic: reaction metabolites span more than one compartment and the reaction is not classified by COBRApy as exchange/demand/sink."}) + "\n\n"

def analyze_model(model):
    """Legacy function for backward compatibility."""
    quick_data = quick_analyze(model)
    fluxes = quick_data["fluxes"]
    wt_growth = quick_data["wt_growth"]
    
    essentiality = []
    for result in stream_ko_analysis(model, wt_growth, fluxes):
        try:
            data = json.loads(result.strip())
            if data.get("type") == "complete":
                essentiality = data.get("exchanges", [])
        except:
            pass
    
    return {
        "stats": quick_data["stats"],
        "reactions": quick_data["reactions"],
        "metabolites": quick_data["metabolites"],
        "exchanges": essentiality,
        "objective": quick_data["objective"],
        "objective_metabolites": quick_data["objective_metabolites"],
        "essentiality_rule": "Exchange reaction is essential when reaction KO gives growth < 5% of WT growth.",
        "transport_rule": "Transport is shown using a simple heuristic: reaction metabolites span more than one compartment and the reaction is not classified by COBRApy as exchange/demand/sink.",
    }

def find_biomass_reaction(model):
    """Best-effort identification of the biomass reaction.

    The model's linear objective is almost always exactly the biomass
    reaction, so that is checked first (and is O(1) rather than scanning
    every reaction's id/name). Falls back to a name search over
    model.reactions (not model.exchanges — a biomass reaction is essentially
    never an exchange) for edge cases where the objective isn't set.
    """
    try:
        coefficients = linear_reaction_coefficients(model)
        if coefficients:
            return next(iter(coefficients)).id
    except Exception:
        pass

    for rxn in model.reactions:
        if "biomass" in rxn.id.lower() or "biomass" in (rxn.name or "").lower():
            return rxn.id
    return None

# ---------------------------------------------------------------------------
# Diet / media application.
#
# A "diet" constrains which metabolites the model may take up (exchange
# reaction lower bounds) and by how much. Three built-in options plus a
# user-uploaded custom diet file:
#   - "none"     : leave the model's own bounds exactly as uploaded.
#   - "complete" : open every exchange to unrestricted uptake ("rich media").
#   - "minimal"  : compute the smallest set of nutrients (via
#                  cobra.medium.minimal_medium) that sustains the same max
#                  growth the model achieves with everything open.
#   - "custom"   : close every exchange, then open exactly the ones named in
#                  an uploaded diet file, at the given bound.
#
# Only lower bounds (uptake capacity) are ever touched; secretion (upper
# bounds) is left as defined in the uploaded model.
# ---------------------------------------------------------------------------

def run_with_exact_solver(model, fn):
    """Run fn() with the model's solver temporarily switched to GLPK's
    exact-arithmetic interface ("glpk_exact"), then always switch back to
    whatever interface the model was using before -- even if fn() raises.

    Why: cobra.medium.minimal_medium's open_exchanges=True path force-opens
    every exchange reaction to (-1000, 1000), which for a genome-scale model
    with a wide range of stoichiometric coefficients can produce a very
    poorly scaled LP. The default GLPK solver runs a floating-point
    scaling/factorization step for problems like that, and on some GLPK
    builds (this has been seen on Windows) a badly-scaled problem can
    trigger a fatal, unrecoverable crash in GLPK's own memory allocator
    ("glp_free: memory allocation error") -- a C-level abort that takes the
    whole Python process down with it, so no amount of try/except around
    the call can catch it. glpk_exact uses exact rational arithmetic and
    skips floating-point scaling entirely, avoiding that failure mode
    (cobra's own minimal_medium docs list "switching to a different solver"
    as the recommended remedy for numerical instability here). It's slower,
    so it's only used for the specific computation passed in, not globally.
    """
    try:
        original = model.solver.interface.__name__.split(".")[-1].split("_interface")[0]
    except Exception:
        original = None

    if original == "glpk_exact":
        return fn()

    switched = False
    if original is not None:
        try:
            model.solver = "glpk_exact"
            switched = True
        except Exception:
            switched = False  # glpk_exact unavailable -- fall back to running as-is

    try:
        return fn()
    finally:
        if switched:
            try:
                model.solver = original
            except Exception:
                pass

def build_exchange_index(model):
    """Map an exchange reaction's own id, its single metabolite's full id,
    and that metabolite's compartment-stripped base id, all lowercased, to
    the exchange reaction's real id. Used to match diet-file entries (which
    may use a different compartment-tag convention than this model, e.g.
    "glc_D_e" vs this model's "glc_D_e0", or VMH-style "glc_D[e]") against
    this model's actual exchange reactions.
    """
    by_rxn_id, by_full_met_id, by_base_met_id = {}, {}, {}
    for exch in model.exchanges:
        by_rxn_id[exch.id.lower()] = exch.id
        if len(exch.metabolites) == 1:
            (met,) = exch.metabolites.keys()
            by_full_met_id.setdefault(met.id.lower(), exch.id)
            by_base_met_id.setdefault(metabolite_base_id(met.id), exch.id)
    return by_rxn_id, by_full_met_id, by_base_met_id

def parse_diet_file(file_storage):
    """Parse an uploaded diet/media file into (raw_id, flux_value) pairs.

    Accepts CSV, TSV, or semicolon-separated text with at least two
    columns: an identifier (an exchange reaction id, or a bare/
    compartment-tagged metabolite id) and a flux value. Following the VMH
    diet-file convention, a positive flux is treated as an uptake magnitude
    and negated into a lower bound; a non-positive value is used directly
    as the lower bound. A header row is auto-detected (any row whose second
    column isn't a number) and skipped.
    """
    raw = file_storage.read()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel  # default to comma-separated

    entries = []
    for row in csv.reader(io.StringIO(text), dialect):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        if len(cells) < 2:
            continue
        raw_id = cells[0].strip('"').strip("'")
        try:
            flux = float(cells[1])
        except ValueError:
            continue  # header row, or an unparseable line — skip rather than fail the whole file
        if raw_id:
            entries.append((raw_id, flux))
    return entries

def resolve_diet_entries(model, entries):
    """Match parsed diet entries against this model's actual exchange
    reactions. Returns (matched: {exchange_id: lower_bound}, unmatched:
    [{"id":..., "flux":...}, ...]) so unmatched entries can be surfaced to
    the user rather than silently dropped."""
    by_rxn_id, by_full_met_id, by_base_met_id = build_exchange_index(model)
    matched, unmatched = {}, []

    for raw_id, flux in entries:
        lower_bound = -flux if flux > 0 else flux
        key = raw_id.strip().lower()

        exch_id = by_rxn_id.get(key) or by_full_met_id.get(key)
        if exch_id is None:
            exch_id = by_base_met_id.get(metabolite_base_id(raw_id.strip()))
        if exch_id is None and not key.startswith("ex_"):
            exch_id = by_rxn_id.get("ex_" + key)

        if exch_id is None:
            unmatched.append({"id": raw_id, "flux": flux})
        else:
            matched[exch_id] = lower_bound

    return matched, unmatched

def apply_diet_to_model(model, mode, diet_entries=None):
    """Constrain exchange lower bounds according to the requested diet mode.
    Returns a summary dict describing what was applied/skipped, suitable for
    returning straight to the frontend."""
    if mode not in ("none", "complete", "minimal", "custom"):
        return {"mode": mode, "applied": [], "unmatched": [], "note": f"Unknown diet mode '{mode}'; no changes made.", "error": True}

    if mode == "none":
        return {"mode": "none", "applied": [], "unmatched": [], "note": "Using the model's original exchange bounds, unchanged."}

    if mode == "complete":
        applied = []
        for exch in model.exchanges:
            exch.lower_bound = -1000.0
            applied.append(exch.id)
        return {
            "mode": "complete",
            "applied": applied,
            "unmatched": [],
            "note": f"All {len(applied)} exchange reactions opened to unrestricted uptake (lower bound -1000).",
        }

    if mode == "minimal":
        # The target is the model's OWN maximum growth rate, under its own
        # original bounds exactly as uploaded. Bug fix: this used to force
        # every exchange bound open to (-1000, 1000) *before* measuring the
        # "model's own maximum growth", which lets FBA route flux through
        # byproduct/secretion exchanges run in reverse (no thermodynamic
        # constraints stop it) and inflates the target to a biologically
        # meaningless value -- e.g. ~47x too high on the bundled E. coli core
        # test model (40.85 vs the model's real 0.87 h^-1). minimal_medium's
        # own open_exchanges=True below already searches over every exchange
        # (temporarily, inside its own model context) while solving for
        # whatever target we pass it, so it doesn't need us to open anything
        # first -- doing so only changes what target we ask it to hit.
        target_growth = safe_float(model.slim_optimize(error_value=0.0)) or 0.0

        if target_growth <= 1e-9:
            return {
                "mode": "minimal", "applied": [], "unmatched": [],
                "note": "Model cannot grow under its own original bounds; a minimal medium is undefined.",
                "error": True,
            }

        try:
            medium = run_with_exact_solver(
                model, lambda: minimal_medium(model, target_growth, minimize_components=False, open_exchanges=True)
            )
        except Exception as exc:
            return {
                "mode": "minimal", "applied": [], "unmatched": [],
                "note": f"Could not compute a minimal medium: {exc}", "error": True,
            }
        if medium is None:
            return {
                "mode": "minimal", "applied": [], "unmatched": [],
                "note": "Minimal-medium computation was infeasible for this model.", "error": True,
            }

        for exch in model.exchanges:
            exch.lower_bound = 0.0
        applied = []
        for rxn_id, flux in medium.items():
            if flux <= 0:
                continue
            model.reactions.get_by_id(rxn_id).lower_bound = -abs(float(flux))
            applied.append(rxn_id)
        return {
            "mode": "minimal",
            "applied": applied,
            "unmatched": [],
            "note": f"Computed the smallest set of {len(applied)} nutrient(s) that sustains the model's original maximum growth rate ({target_growth:.4g}).",
        }

    # mode == "custom"
    for exch in model.exchanges:
        exch.lower_bound = 0.0
    matched, unmatched = resolve_diet_entries(model, diet_entries or [])
    applied = []
    for exch_id, lower_bound in matched.items():
        rxn = model.reactions.get_by_id(exch_id)
        rxn.lower_bound = min(lower_bound, rxn.upper_bound)
        applied.append(exch_id)

    total = len(applied) + len(unmatched)
    note = f"Applied {len(applied)} of {total} diet entries."
    if unmatched:
        note += f" {len(unmatched)} entries could not be matched to an exchange reaction in this model (shown below)."
    return {"mode": "custom", "applied": applied, "unmatched": unmatched, "note": note}

# ---------------------------------------------------------------------------
# On-request "Minimal Medium" tab.
#
# Separate from the diet applied at upload time (mode == "minimal" above,
# which permanently constrains the analyzed model to that computed medium):
# this is a read-only, repeatable report the user can (re)run for any growth
# cutoff after the model is loaded, without ever changing the cached model's
# bounds. Shares the same underlying cobra.medium.minimal_medium call and the
# same "target is the model's own growth, not an artificially opened one" fix
# described above.
# ---------------------------------------------------------------------------

def compute_minimal_medium_report(model, growth_cutoff_fraction=1.0, minimize_components=False):
    """Compute the minimal medium needed to sustain growth_cutoff_fraction of
    the model's own maximum growth rate (under its own original bounds).
    cobra's minimal_medium() already manages open_exchanges internally and
    leaves the model's own bounds completely unchanged when it returns, so
    this never permanently changes the cached model -- and deliberately does
    NOT add its own extra `with model:` around that call (an earlier version
    did, "just to be safe"; nesting a redundant context manager around
    minimal_medium's own reentrant optimize() calls could corrupt the GLPK
    solver's internal memory and crash the whole process). growth_cutoff_fraction
    is clamped to (0, 1] -- 1.0 means the model's full own maximum growth,
    0.5 means half of it, etc.

    Returns a dict with wt_growth, growth_cutoff_fraction, target_growth,
    components (a list of {exchange_id, exchange_name, metabolite_id,
    metabolite_name, uptake_flux}, sorted by exchange id), and a note; or an
    "error" key (alongside wt_growth, when known) on failure.
    """
    wt_growth = safe_float(model.slim_optimize(error_value=0.0)) or 0.0
    if wt_growth <= 1e-9:
        return {
            "error": "Model cannot grow under its own original bounds; a minimal medium is undefined.",
            "wt_growth": wt_growth,
        }

    try:
        growth_cutoff_fraction = float(growth_cutoff_fraction)
    except (TypeError, ValueError):
        growth_cutoff_fraction = 1.0
    growth_cutoff_fraction = max(1e-6, min(1.0, growth_cutoff_fraction))
    target_growth = wt_growth * growth_cutoff_fraction

    # minimal_medium() already leaves the model's own bounds completely
    # unchanged on its own (verified: it manages open_exchanges internally
    # and restores everything before returning) -- no outer `with model:` is
    # needed, and nesting one around it here previously caused reentrant
    # calls into the solver that could corrupt GLPK's internal memory
    # ("glp_free: memory allocation error"), crashing the whole server.
    try:
        medium = run_with_exact_solver(
            model,
            lambda: minimal_medium(model, target_growth, minimize_components=minimize_components, open_exchanges=True),
        )
    except Exception as exc:
        return {"error": f"Could not compute a minimal medium: {exc}", "wt_growth": wt_growth}
    if medium is None:
        return {
            "error": f"Minimal-medium computation was infeasible at a {growth_cutoff_fraction * 100:.4g}% growth cutoff.",
            "wt_growth": wt_growth,
        }

    components = []
    for rxn_id, flux in medium.items():
        if flux <= 0:
            continue
        rxn = model.reactions.get_by_id(rxn_id)
        met = next(iter(rxn.metabolites.keys()), None)
        components.append({
            "exchange_id": rxn_id,
            "exchange_name": rxn.name,
            "metabolite_id": met.id if met else None,
            "metabolite_name": clean_metabolite_display_name(met) if met else None,
            "uptake_flux": abs(safe_float(flux) or 0.0),
        })
    components.sort(key=lambda c: c["exchange_id"])

    return {
        "wt_growth": wt_growth,
        "growth_cutoff_fraction": growth_cutoff_fraction,
        "target_growth": target_growth,
        "components": components,
        "note": (
            f"Smallest set of {len(components)} nutrient(s) sustaining "
            f"{growth_cutoff_fraction * 100:.4g}% of the model's own maximum growth rate "
            f"({target_growth:.4g} of {wt_growth:.4g})."
        ),
    }

# ---------------------------------------------------------------------------
# Shared active-flux pathway tracing helpers.
#
# These back both the biomass-specific exchange debugger
# (debug_exchange_to_biomass) and the general-purpose Pathway Tracer
# (/api/trace-pathway): a BFS over a graph built only from reactions
# carrying non-zero flux in the current FBA solution, respecting actual flux
# direction, with currency/cofactor metabolites optionally excluded as path
# steps. Kept as small, independently testable functions so the two features
# can share exactly the same tracing logic without drifting apart.
# ---------------------------------------------------------------------------

def compute_active_flux(model, solution, flux_tolerance=1e-9):
    """{reaction_id: flux} for every reaction carrying non-zero flux
    (abs(flux) > flux_tolerance) in the given FBA solution."""
    return {
        rxn.id: solution.fluxes[rxn.id]
        for rxn in model.reactions
        if abs(solution.fluxes[rxn.id]) > flux_tolerance
    }

def build_active_flux_downstream_graph(model, active_flux, flux_tolerance, trivial_ids):
    """Build {substrate_metabolite_id: [(reaction_id, product_metabolite_id), ...]}
    from only the reactions present in active_flux, respecting the actual
    flux direction (a reaction with negative flux is walked with its
    substrates/products swapped). Metabolite ids in trivial_ids are skipped
    both as a substrate key and as a product hop, so a trace never starts
    from or jumps onto them."""
    downstream = {}
    for rxn in model.reactions:
        if rxn.id not in active_flux:
            continue
        flux = active_flux[rxn.id]

        if flux > 0:
            substrates = [met for met, coeff in rxn.metabolites.items() if coeff < -flux_tolerance]
            products = [met for met, coeff in rxn.metabolites.items() if coeff > flux_tolerance]
        else:
            substrates = [met for met, coeff in rxn.metabolites.items() if coeff > flux_tolerance]
            products = [met for met, coeff in rxn.metabolites.items() if coeff < -flux_tolerance]

        for substrate in substrates:
            if substrate.id in trivial_ids:
                continue
            downstream.setdefault(substrate.id, [])
            for product in products:
                if product.id in trivial_ids:
                    continue
                downstream[substrate.id].append((rxn.id, product.id))
    return downstream

def bfs_flux_paths(downstream, start_met_id, is_target, max_paths=20, max_depth=20):
    """BFS from start_met_id over the `downstream` graph until `is_target`
    (a function of the current metabolite id) is true — that node's path is
    recorded and not expanded further — or max_paths/max_depth is reached. A
    reaction id is never revisited within the same path, which rules out
    cycles. Returns a list of paths, each a list of (reaction_id,
    next_metabolite_id) hops starting from start_met_id (an empty list means
    start_met_id itself already satisfies is_target).
    """
    queue = deque()
    queue.append((start_met_id, []))
    visited = set()
    paths = []

    while queue and len(paths) < max_paths:
        current_met, path = queue.popleft()

        if len(path) >= max_depth:
            continue

        state = (current_met, tuple(rxn_id for rxn_id, _ in path))
        if state in visited:
            continue
        visited.add(state)

        if is_target(current_met):
            paths.append(path)
            continue

        for rxn_id, next_met in downstream.get(current_met, []):
            if any(rxn_id == existing_rxn for existing_rxn, _ in path):
                continue
            new_path = path + [(rxn_id, next_met)]
            queue.append((next_met, new_path))

    return paths

def format_flux_path(model, solution, start_met, path, reaction_types=None, compartment_names=None):
    """Render a BFS path (a list of (reaction_id, next_metabolite_id) hops
    starting from start_met) as {"metabolites": [...], "reactions": [...]}
    with display names and per-step flux/subsystem — used by both the text
    view and the network-graph view.

    Each metabolite/reaction step also carries the "basic info" fields
    needed for a hover tooltip in the network view (compartment, formula,
    charge for metabolites; reaction string, bounds, GPR, and a best-effort
    type for reactions), so the frontend never has to make a second request
    just to describe a node/edge the user is hovering.

    reaction_types/compartment_names are optional precomputed maps (see
    classify_reactions / compartment_display_name). A caller formatting many
    paths in one request should compute these once and pass them in, since
    they only depend on model structure, not on the path being formatted —
    computed on the fly here (once per call) when omitted, so existing
    callers/tests are unaffected.
    """
    if reaction_types is None:
        reaction_types = classify_reactions(model)
    if compartment_names is None:
        compartment_names = {cid: compartment_display_name(cid, name) for cid, name in model.compartments.items()}

    def met_info(met):
        return {
            "id": met.id,
            "name": clean_metabolite_display_name(met),
            "compartment": met.compartment,
            "compartment_name": compartment_names.get(met.compartment, met.compartment),
            "formula": getattr(met, "formula", None),
            "charge": clean(getattr(met, "charge", None)),
        }

    formatted = {
        "metabolites": [met_info(start_met)],
        "reactions": [],
    }
    for rxn_id, next_met in path:
        rxn = model.reactions.get_by_id(rxn_id)
        flux = solution.fluxes[rxn_id]
        next_met_obj = model.metabolites.get_by_id(next_met)

        formatted["reactions"].append({
            "id": rxn_id,
            "flux": float(flux),
            "name": rxn.name,
            "subsystem": subsystem_value(rxn),
            "reaction_string": rxn.reaction,
            "type": reaction_types.get(rxn_id, ""),
            "lower_bound": safe_float(rxn.lower_bound),
            "upper_bound": safe_float(rxn.upper_bound),
            "reversible": bool(rxn.lower_bound < 0 and rxn.upper_bound > 0),
            "gene_reaction_rule": getattr(rxn, "gene_reaction_rule", ""),
        })
        formatted["metabolites"].append(met_info(next_met_obj))
    return formatted

def resolve_reaction_start_metabolites(model, reaction_id, solution, flux_tolerance=1e-9):
    """Resolve a reaction serving as a pathway-trace starting point into the
    metabolite(s) it actually produces under its current flux direction, so
    a general trace can begin from real graph nodes. Returns
    (metabolites, error_message) — metabolites is empty and error_message is
    set when the reaction doesn't exist or carries no active flux in the
    current FBA solution (there's nothing meaningful to trace downstream
    from a dead reaction)."""
    try:
        rxn = model.reactions.get_by_id(reaction_id)
    except KeyError:
        return [], f"Reaction '{reaction_id}' not found in model"

    flux = solution.fluxes[reaction_id] if reaction_id in solution.fluxes else None
    if flux is None or abs(flux) <= flux_tolerance:
        return [], (
            f"Reaction '{reaction_id}' carries no active flux in the current FBA solution — "
            "nothing to trace downstream from."
        )

    if flux > 0:
        products = [met for met, coeff in rxn.metabolites.items() if coeff > flux_tolerance]
    else:
        products = [met for met, coeff in rxn.metabolites.items() if coeff < -flux_tolerance]

    return products, None

def debug_exchange_to_biomass(
    model,
    exchange_rxn_id,
    internal_metabolite_id,
    biomass_rxn_id,
    max_flux_paths=20,
    max_flux_path_depth=20,
    flux_tolerance=1e-9,
    exclude_trivial_metabolites=True,
    reaction_types=None,
    compartment_names=None,
):
    """
    Trace active-flux pathways from an exchange-derived metabolite toward the biomass reaction.
    Only reactions carrying non-zero flux in the current model solution are considered.

    When exclude_trivial_metabolites is True (the default), currency/cofactor
    metabolites (see TRIVIAL_METABOLITES — H+, water, ATP/ADP, NAD(H), etc.)
    are never followed as a path step: a reaction that happens to also
    produce/consume one doesn't let the trace jump onto it. The requested
    start metabolite is always traced from, even if it happens to itself be
    on the trivial list.

    reaction_types/compartment_names are optional precomputed maps passed
    straight through to format_flux_path (see there) — a caller tracing
    several exchanges in one request (api_debug_exchanges) computes these
    once up front rather than recomputing them for every exchange's
    before/after-KO trace.
    """
    model.reactions.get_by_id(exchange_rxn_id)  # validate it exists before running FBA
    biomass = model.reactions.get_by_id(biomass_rxn_id)
    start_met = model.metabolites.get_by_id(internal_metabolite_id)

    solution = model.optimize()
    if solution.status != "optimal":
        return {"status": solution.status, "paths": [], "error": "FBA not optimal"}

    active_flux = compute_active_flux(model, solution, flux_tolerance)

    # Metabolite ids to skip as path steps (but never the start metabolite
    # itself, so tracing can still begin even if it happens to be trivial).
    trivial_ids = set()
    if exclude_trivial_metabolites:
        trivial_ids = {
            met.id for met in model.metabolites
            if met.id != start_met.id and is_trivial_metabolite(met)
        }

    downstream = build_active_flux_downstream_graph(model, active_flux, flux_tolerance, trivial_ids)

    def is_biomass_target(met_id):
        biomass_coeff = biomass.metabolites.get(model.metabolites.get_by_id(met_id), 0)
        return biomass_coeff < -flux_tolerance

    paths = bfs_flux_paths(downstream, start_met.id, is_biomass_target, max_flux_paths, max_flux_path_depth)
    formatted_paths = [
        format_flux_path(model, solution, start_met, path, reaction_types, compartment_names)
        for path in paths
    ]

    return {
        "status": "success",
        "exchange_id": exchange_rxn_id,
        "exchange_flux": float(solution.fluxes[exchange_rxn_id]),
        "biomass_flux": float(solution.objective_value),
        "paths": formatted_paths,
        "num_active_reactions": len(active_flux),
        "excluded_trivial_metabolites": exclude_trivial_metabolites,
    }

def run_pathway_trace(
    model, solution, start_type, start_id, target_met,
    max_levels, max_paths, exclude_trivial,
    flux_tolerance=1e-9, reaction_types=None, compartment_names=None,
):
    """Run one pass of the general pathway trace (resolve the start point,
    build the active-flux graph, BFS to the target, format the paths)
    against `solution` — the FBA result for `model` exactly as it's
    currently bounded. This is the one piece of logic shared by both the
    baseline trace and, when the caller has applied a knockout inside its
    own `with model:` block first, the after-KO trace, so the two are
    guaranteed to be traced the same way.

    Returns a dict with an "error" key on failure, or on success:
    "status", "start_metabolites", "num_active_reactions", "paths",
    "growth", and "active_flux" ({reaction_id: flux} for every reaction
    carrying flux in `solution` — kept for the caller's own before/after
    comparison, not meant to be sent to the frontend as-is since it can be
    large; the endpoint below pulls out just the reactions it needs).
    """
    if solution.status != "optimal":
        return {"status": solution.status, "error": "FBA not optimal"}

    if start_type == "metabolite":
        try:
            start_metabolites = [model.metabolites.get_by_id(start_id)]
        except KeyError:
            return {"error": f"Starting metabolite '{start_id}' not found in model"}
    else:
        start_metabolites, resolve_error = resolve_reaction_start_metabolites(model, start_id, solution, flux_tolerance)
        if resolve_error:
            return {"error": resolve_error}
        if not start_metabolites:
            return {"error": f"Reaction '{start_id}' has no products to trace from under its current flux direction."}

    active_flux = compute_active_flux(model, solution, flux_tolerance)

    # Never treat the target or any starting metabolite as trivial, even if
    # it's on the currency/cofactor list — the user explicitly chose it, so
    # it must remain a usable node in the traced graph.
    trivial_ids = set()
    if exclude_trivial:
        keep_ids = {target_met.id} | {m.id for m in start_metabolites}
        trivial_ids = {
            met.id for met in model.metabolites
            if met.id not in keep_ids and is_trivial_metabolite(met)
        }

    downstream = build_active_flux_downstream_graph(model, active_flux, flux_tolerance, trivial_ids)

    def is_target(met_id):
        return met_id == target_met.id

    all_paths = []
    for start_met in start_metabolites:
        if len(all_paths) >= max_paths:
            break
        paths = bfs_flux_paths(downstream, start_met.id, is_target, max_paths - len(all_paths), max_levels)
        all_paths.extend(
            format_flux_path(model, solution, start_met, path, reaction_types, compartment_names)
            for path in paths
        )

    return {
        "status": "success",
        "start_metabolites": [
            {
                "id": m.id, "name": clean_metabolite_display_name(m),
                "compartment": m.compartment,
                "compartment_name": (compartment_names or {}).get(m.compartment, m.compartment),
                "formula": getattr(m, "formula", None),
                "charge": clean(getattr(m, "charge", None)),
            }
            for m in start_metabolites
        ],
        "num_active_reactions": len(active_flux),
        "paths": all_paths,
        "growth": safe_float(solution.objective_value),
        "active_flux": active_flux,
    }

def resolve_knockout_targets(model, raw_ids):
    """Resolve a list of user-typed ids for the Pathway Tracer's optional
    before/after-knockout comparison. Each id is looked up as a reaction
    first, then as a metabolite. A reaction resolves to itself. Knocking
    out a metabolite has no single built-in meaning in COBRApy, so it's
    approximated here as blocking every reaction the metabolite
    participates in as a reactant or product — effectively removing it from
    the network. Returns (resolved, not_found):
      resolved: [{"input": raw_id, "type": "reaction"|"metabolite", "id": ..., "name": ..., "reactions_blocked": [rxn_id, ...]}, ...]
      not_found: [raw_id, ...] — ids that matched neither a reaction nor a metabolite
    """
    resolved, not_found = [], []
    for raw in raw_ids:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            rxn = model.reactions.get_by_id(raw)
        except KeyError:
            rxn = None
        if rxn is not None:
            resolved.append({
                "input": raw, "type": "reaction", "id": rxn.id, "name": rxn.name,
                "reactions_blocked": [rxn.id],
            })
            continue
        try:
            met = model.metabolites.get_by_id(raw)
        except KeyError:
            met = None
        if met is not None:
            rxn_ids = sorted(r.id for r in met.reactions)
            resolved.append({
                "input": raw, "type": "metabolite", "id": met.id, "name": clean_metabolite_display_name(met),
                "reactions_blocked": rxn_ids,
            })
            continue
        not_found.append(raw)
    return resolved, not_found

def apply_knockouts(model, resolved_targets):
    """Zero the bounds of every reaction implicated by resolve_knockout_targets
    (deduplicated across all targets). Meant to be called inside the
    caller's own `with model:` context so the change is automatically
    reverted afterwards. Returns the sorted list of reaction ids actually
    blocked."""
    rxn_ids = sorted({rid for t in resolved_targets for rid in t["reactions_blocked"]})
    for rid in rxn_ids:
        model.reactions.get_by_id(rid).knock_out()
    return rxn_ids

# ---------------------------------------------------------------------------
# Network Gaps tab (on-request, as soon as the model loads).
#
# Two complementary diagnostics for spotting network-connectivity gaps
# (dead-end/orphan metabolites, un-fillable pathways) under the model's
# CURRENT bounds -- i.e. whatever diet was applied at upload time, exactly
# as everything else in the app sees it:
#   - Blocked reactions: reactions that cannot carry any flux at all, via
#     cobra's own FVA-based find_blocked_reactions. open_exchanges is left
#     False (unlike minimal_medium) so this uses the model's real current
#     bounds rather than an artificially widened, poorly-scaled problem --
#     the kind of thing that has been observed to destabilize GLPK on some
#     builds (see run_with_exact_solver above). processes is pinned to 1:
#     cobra's default parallel FVA spawns worker processes, and on Windows
#     that uses the "spawn" start method, which re-pickles the model into a
#     fresh interpreter for each worker -- fragile and slow to do from
#     inside a live Flask request, so a single serial pass is used instead.
#   - Dead-end metabolites: a fast, local, purely structural check (no LP
#     solves at all) for metabolites that, given each of their reactions'
#     current bounds/reversibility, can only ever be produced or only ever
#     be consumed -- never both. That's often *why* a reaction ends up
#     blocked, though a reaction can also be blocked for more global
#     network reasons a local per-metabolite check can't see, which is why
#     both diagnostics are shown together rather than one substituting for
#     the other.
# ---------------------------------------------------------------------------

def find_dead_end_metabolites(model):
    """Bounds-aware structural dead-end check (see module comment above).
    Returns a list of {"id", "reason", "reactions"} for every metabolite
    that can only ever be produced, only ever be consumed, or never either
    (reason: "no_consuming_reaction", "no_producing_reaction", or
    "fully_blocked"), plus any metabolite with no reactions at all
    ("no_reactions" -- a data artifact, but worth surfacing)."""
    produce_rxns, consume_rxns = {}, {}
    for rxn in model.reactions:
        lb, ub = rxn.lower_bound, rxn.upper_bound
        for met, coeff in rxn.metabolites.items():
            if coeff > 0:
                if ub > 0:
                    produce_rxns.setdefault(met.id, set()).add(rxn.id)
                if lb < 0:
                    consume_rxns.setdefault(met.id, set()).add(rxn.id)
            elif coeff < 0:
                if lb < 0:
                    produce_rxns.setdefault(met.id, set()).add(rxn.id)
                if ub > 0:
                    consume_rxns.setdefault(met.id, set()).add(rxn.id)

    dead_ends = []
    for met in model.metabolites:
        touching = sorted(r.id for r in met.reactions)
        if not touching:
            dead_ends.append({"id": met.id, "reason": "no_reactions", "reactions": []})
            continue
        can_produce = met.id in produce_rxns
        can_consume = met.id in consume_rxns
        if can_produce and can_consume:
            continue
        if can_produce:
            reason = "no_consuming_reaction"
        elif can_consume:
            reason = "no_producing_reaction"
        else:
            reason = "fully_blocked"
        dead_ends.append({"id": met.id, "reason": reason, "reactions": touching})
    return dead_ends

def compute_network_gaps_report(model, zero_cutoff=None):
    """Combined report for the Network Gaps tab. Never changes the cached
    model: find_blocked_reactions manages its own `with model:` internally,
    and find_dead_end_metabolites only reads bounds/stoichiometry."""
    reaction_types = classify_reactions(model)
    compartment_names = {cid: compartment_display_name(cid, name) for cid, name in model.compartments.items()}

    # Bug fix: find_blocked_reactions runs flux variability analysis, which
    # (unlike a single slim_optimize call) re-solves the LP many times in a
    # row -- one min and one max per candidate reaction -- reusing the same
    # floating-point GLPK problem object each time. On this GLPK/Windows
    # build that repeated-solve pattern has been observed to trigger the
    # same fatal "glp_free: memory allocation error" crash as
    # minimal_medium's single wide-open solve did (see run_with_exact_solver
    # above) -- just from a different trigger (many solves vs. one poorly
    # scaled one). Running it on glpk_exact avoids both failure modes; it's
    # slower per solve, but find_blocked_reactions already pre-filters to
    # only the reactions with near-zero flux in the current solution, so in
    # practice this is far fewer solves than "every reaction x2".
    try:
        blocked_ids = run_with_exact_solver(
            model, lambda: find_blocked_reactions(model, zero_cutoff=zero_cutoff, open_exchanges=False, processes=1)
        )
    except Exception as exc:
        return {"error": f"Could not compute blocked reactions: {exc}"}

    blocked_reactions = []
    for rxn_id in sorted(blocked_ids):
        rxn = model.reactions.get_by_id(rxn_id)
        blocked_reactions.append({
            "id": rxn.id,
            "name": rxn.name,
            "subsystem": subsystem_value(rxn),
            "type": reaction_types.get(rxn.id, ""),
            "reaction_string": rxn.reaction,
            "lower_bound": safe_float(rxn.lower_bound),
            "upper_bound": safe_float(rxn.upper_bound),
            "gene_reaction_rule": getattr(rxn, "gene_reaction_rule", ""),
        })

    try:
        dead_end_raw = find_dead_end_metabolites(model)
    except Exception as exc:
        return {"error": f"Could not compute dead-end metabolites: {exc}"}

    dead_end_metabolites = []
    for d in dead_end_raw:
        met = model.metabolites.get_by_id(d["id"])
        dead_end_metabolites.append({
            "id": met.id,
            "name": clean_metabolite_display_name(met),
            "compartment": met.compartment,
            "compartment_name": compartment_names.get(met.compartment, met.compartment),
            "formula": getattr(met, "formula", None),
            "reason": d["reason"],
            "reactions": d["reactions"],
        })

    return {
        "blocked_reactions": blocked_reactions,
        "dead_end_metabolites": dead_end_metabolites,
        "zero_cutoff": safe_float(zero_cutoff) if zero_cutoff is not None else safe_float(model.tolerance),
        "note": (
            f"{len(blocked_reactions)} blocked reaction(s) (cannot carry any flux under the model's current bounds) "
            f"and {len(dead_end_metabolites)} dead-end metabolite(s) (can only ever be produced or only ever be "
            "consumed given current reaction bounds/reversibility, or -- rarely -- have no reactions at all)."
        ),
    }

# ---------------------------------------------------------------------------
# Demand & Sink Audit (on-request, part of the Network Gaps tab).
#
# Demand and sink reactions (cobra's model.demands / model.sinks) are
# single-metabolite boundary reactions used to model things like forced
# accumulation, biomass side-components, or buffered cofactor pools --
# they're often added by hand during model curation and can go stale
# (left over from an earlier model version, or never actually load-bearing)
# without anyone noticing, since they don't show up in the Exchange
# essentiality tab (that's exchanges only). This mirrors the same
# essentiality test used there (reaction knockout, KO growth < 5% of WT
# growth), applied to demand/sink reactions instead, plus whether each one
# even carries flux in the current FBA solution at all -- a demand/sink
# with zero flux AND zero essentiality is a reasonable candidate for "is
# this reaction still needed?"
# ---------------------------------------------------------------------------

def compute_demand_sink_audit(model):
    """Audit every demand and sink reaction: metabolite, bounds/
    reversibility, whether it carries flux in the current (fresh) FBA
    solution, and a reaction-knockout essentiality test identical in
    definition to the one used for exchanges. Uses plain slim_optimize()
    per reaction (bounds temporarily zeroed and restored, exactly like
    stream_ko_analysis above) rather than FVA, so it doesn't carry the
    same repeated-solve numerical risk noted on find_blocked_reactions
    above -- this is the same pattern already used for exchange
    essentiality, which has not shown that failure mode.
    """
    wt_solution = model.optimize()
    if wt_solution.status != "optimal":
        return {"error": f"WT optimization was not optimal (status: {wt_solution.status})."}
    wt_growth = max(0.0, safe_float(wt_solution.objective_value) or 0.0)
    fluxes = wt_solution.fluxes

    rows = []
    for kind, reactions in (("demand", model.demands), ("sink", model.sinks)):
        for rxn in reactions:
            met = next(iter(rxn.metabolites.keys()), None)
            wt_flux = safe_float(fluxes.get(rxn.id)) if hasattr(fluxes, "get") else None

            ko_growth = None
            ko_status = "not tested"
            if wt_growth > 0:
                old_lb, old_ub = rxn.lower_bound, rxn.upper_bound
                try:
                    rxn.lower_bound = 0
                    rxn.upper_bound = 0
                    value = model.slim_optimize(error_value=None)
                    if value is None:
                        ko_status = "infeasible"
                    else:
                        ko_status = "optimal"
                        ko_growth = max(0.0, safe_float(value) or 0.0)
                except Exception as exc:
                    ko_status = f"error: {exc}"
                finally:
                    rxn.lower_bound = old_lb
                    rxn.upper_bound = old_ub

            ratio = None
            essential = False
            if ko_growth is not None and wt_growth > 0:
                ratio = ko_growth / wt_growth
                essential = ratio < 0.05

            rows.append({
                "id": rxn.id,
                "name": rxn.name,
                "kind": kind,
                "metabolite_id": met.id if met else None,
                "metabolite_name": clean_metabolite_display_name(met) if met else None,
                "compartment": met.compartment if met else None,
                "lower_bound": safe_float(rxn.lower_bound),
                "upper_bound": safe_float(rxn.upper_bound),
                "reversible": bool(rxn.lower_bound < 0 and rxn.upper_bound > 0),
                "wt_flux": wt_flux,
                "used_in_wt_solution": bool(wt_flux is not None and abs(wt_flux) > 1e-9),
                "ko_growth": ko_growth,
                "ko_growth_fraction": ratio,
                "essential": essential,
                "ko_status": ko_status,
                "subsystem": subsystem_value(rxn),
                "gene_reaction_rule": getattr(rxn, "gene_reaction_rule", ""),
                "reaction_string": rxn.reaction,
            })

    rows.sort(key=lambda r: (r["kind"], r["id"]))
    n_demand = sum(1 for r in rows if r["kind"] == "demand")
    n_sink = sum(1 for r in rows if r["kind"] == "sink")
    n_unused = sum(1 for r in rows if not r["used_in_wt_solution"])
    n_essential = sum(1 for r in rows if r["essential"])

    return {
        "rows": rows,
        "wt_growth": wt_growth,
        "note": (
            f"{n_demand} demand reaction(s), {n_sink} sink reaction(s). {n_unused} carry no flux in the current "
            f"FBA solution; {n_essential} are essential (reaction knockout gives growth < 5% of WT growth)."
        ),
    }

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/analyze-quick")
def api_analyze_quick():
    """Fast endpoint that returns stats without KO analysis."""
    uploaded = request.files.get("model")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Please upload a model file."}), 400

    filename = secure_filename(uploaded.filename)
    suffixes = "".join(Path(filename).suffixes).lower()
    if not any(suffixes.endswith(ext) for ext in ALLOWED):
        return jsonify({"error": "Unsupported model format. Use SBML/XML, JSON, YAML, MATLAB .mat, or supported compressed SBML."}), 400

    diet_mode = (request.form.get("diet_mode") or "none").strip().lower()
    diet_file = request.files.get("diet_file")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / filename
        uploaded.save(path)
        try:
            model = load_uploaded_model(path)

            diet_entries = None
            if diet_mode == "custom":
                if not diet_file or not diet_file.filename:
                    return jsonify({"error": "Custom diet selected, but no diet file was uploaded."}), 400
                try:
                    diet_entries = parse_diet_file(diet_file)
                except Exception as exc:
                    return jsonify({"error": f"Could not read the diet file: {exc}"}), 400
                if not diet_entries:
                    return jsonify({"error": "The diet file didn't contain any usable (id, flux) rows."}), 400

            diet_result = apply_diet_to_model(model, diet_mode, diet_entries)

            result = quick_analyze(model)
            result["diet"] = diet_result

            # Store model in cache for streaming
            model_id = str(uuid.uuid4())
            MODEL_CACHE[model_id] = (model, time.time())
            result["model_id"] = model_id
            return jsonify(result)
        except Exception as exc:
            return jsonify({
                "error": str(exc),
                "hint": "For SBML, install python-libsbml. For MATLAB files, ensure the relevant COBRApy dependencies are installed."
            }), 500

@app.get("/api/analyze-ko/<model_id>")
def api_analyze_ko(model_id):
    """Stream KO analysis progress."""
    if model_id not in MODEL_CACHE:
        return jsonify({"error": "Model not found or expired"}), 404
    
    model, _ = MODEL_CACHE[model_id]

    try:
        wt_solution = model.optimize()
        wt_growth = safe_float(wt_solution.objective_value) or 0.0
        fluxes = wt_solution.fluxes if hasattr(wt_solution, "fluxes") else {}

        def generate():
            try:
                for line in stream_ko_analysis(model, wt_growth, fluxes):
                    yield line
            finally:
                # Keep the model cached (refresh its TTL) instead of deleting
                # it: the user can request exchange debugging right after KO
                # analysis finishes, which needs this same model object. The
                # periodic cleanup thread reaps it after MODEL_TTL_SECONDS of
                # inactivity.
                touch_model_cache(model_id)

        return Response(generate(), mimetype='text/event-stream', headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        })
    except Exception as exc:
        if model_id in MODEL_CACHE:
            del MODEL_CACHE[model_id]
        return jsonify({"error": str(exc)}), 500

@app.post("/api/analyze")
def api_analyze():
    """Full analysis endpoint (legacy, slower)."""
    uploaded = request.files.get("model")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Please upload a model file."}), 400

    filename = secure_filename(uploaded.filename)
    suffixes = "".join(Path(filename).suffixes).lower()
    if not any(suffixes.endswith(ext) for ext in ALLOWED):
        return jsonify({"error": "Unsupported model format. Use SBML/XML, JSON, YAML, MATLAB .mat, or supported compressed SBML."}), 400

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / filename
        uploaded.save(path)
        try:
            model = load_uploaded_model(path)
            result = analyze_model(model)
            return jsonify(result)
        except Exception as exc:
            return jsonify({
                "error": str(exc),
                "hint": "For SBML, install python-libsbml. For MATLAB files, ensure the relevant COBRApy dependencies are installed."
            }), 500

@app.post("/api/minimal-medium")
def api_minimal_medium():
    """On-request report for the Minimal Medium tab: the smallest nutrient
    set sustaining a user-chosen fraction of the model's own maximum growth
    rate. Independent of the diet chosen at upload time, and never changes
    the cached model (see compute_minimal_medium_report)."""
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    model_id = data.get("model_id")
    if not model_id or model_id not in MODEL_CACHE:
        return jsonify({"error": "Model not found or expired. Please re-run the initial analysis."}), 404

    try:
        growth_cutoff_fraction = float(data.get("growth_cutoff_fraction", 1.0))
    except (TypeError, ValueError):
        growth_cutoff_fraction = 1.0

    touch_model_cache(model_id)
    model, _ = MODEL_CACHE[model_id]

    try:
        report = compute_minimal_medium_report(model, growth_cutoff_fraction)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if "error" in report:
        return jsonify({"status": "error", **report}), 400
    return jsonify({"status": "success", **report})

@app.post("/api/network-gaps")
def api_network_gaps():
    """On-request report for the Network Gaps tab: reactions that can't
    carry any flux under the model's current bounds, plus structurally
    dead-end metabolites. Independent of every other tab; never changes
    the cached model (see compute_network_gaps_report)."""
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    model_id = data.get("model_id")
    if not model_id or model_id not in MODEL_CACHE:
        return jsonify({"error": "Model not found or expired. Please re-run the initial analysis."}), 404

    touch_model_cache(model_id)
    model, _ = MODEL_CACHE[model_id]

    try:
        report = compute_network_gaps_report(model)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if "error" in report:
        return jsonify({"status": "error", **report}), 400
    return jsonify({"status": "success", **report})

@app.post("/api/demand-sink-audit")
def api_demand_sink_audit():
    """On-request report for the Demand & Sink Audit section: every demand
    and sink reaction's metabolite, bounds, WT usage, and knockout
    essentiality. Independent of every other tab; never permanently changes
    the cached model (see compute_demand_sink_audit)."""
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    model_id = data.get("model_id")
    if not model_id or model_id not in MODEL_CACHE:
        return jsonify({"error": "Model not found or expired. Please re-run the initial analysis."}), 404

    touch_model_cache(model_id)
    model, _ = MODEL_CACHE[model_id]

    try:
        report = compute_demand_sink_audit(model)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if "error" in report:
        return jsonify({"status": "error", **report}), 400
    return jsonify({"status": "success", **report})

@app.post("/api/debug-exchanges")
def api_debug_exchanges():
    """Debug selected essential exchanges by tracing external metabolite ->
    biomass, both with the exchange active (before KO) and with it
    reversibly knocked out (after KO), so the two flux traces can be
    compared side by side."""
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    model_id = data.get("model_id")
    exchange_ids = data.get("exchange_ids", [])
    exclude_trivial = bool(data.get("exclude_trivial_metabolites", True))

    if not model_id or model_id not in MODEL_CACHE:
        return jsonify({"error": "Model not found or expired. Please re-run the initial analysis."}), 404

    if not exchange_ids:
        return jsonify({"error": "No exchanges selected"}), 400

    touch_model_cache(model_id)
    model, _ = MODEL_CACHE[model_id]

    try:
        biomass_rxn = find_biomass_reaction(model)
        if not biomass_rxn:
            return jsonify({"error": "Could not identify biomass reaction"}), 400

        wt_solution = model.optimize()
        wt_growth = max(0.0, safe_float(wt_solution.objective_value) or 0.0)

        # Computed once and reused across every selected exchange's
        # before/after trace below — both depend only on model structure,
        # not on which exchange is knocked out, so recomputing them inside
        # the loop would waste an O(n) pass per exchange for nothing.
        reaction_types = classify_reactions(model)
        compartment_names = {cid: compartment_display_name(cid, name) for cid, name in model.compartments.items()}

        results = {}
        for exch_id in exchange_ids:
            try:
                exch = model.reactions.get_by_id(exch_id)
            except KeyError:
                results[exch_id] = {"error": f"Exchange reaction '{exch_id}' not found in model"}
                continue

            internal_mets = list(exch.metabolites.keys())
            if not internal_mets:
                results[exch_id] = {"error": "No metabolites found in exchange"}
                continue
            int_met = internal_mets[0]

            try:
                # Before KO: trace on the model exactly as analyzed.
                before = debug_exchange_to_biomass(
                    model, exch_id, int_met.id, biomass_rxn,
                    max_flux_paths=5, max_flux_path_depth=15,
                    exclude_trivial_metabolites=exclude_trivial,
                    reaction_types=reaction_types, compartment_names=compartment_names,
                )

                # After KO: knock the exchange out inside a model context so
                # the bound change is automatically reverted afterwards, then
                # re-trace on the perturbed steady state (flux may reroute
                # through an alternate pathway, or vanish entirely).
                with model:
                    exch.knock_out()
                    after = debug_exchange_to_biomass(
                        model, exch_id, int_met.id, biomass_rxn,
                        max_flux_paths=5, max_flux_path_depth=15,
                        exclude_trivial_metabolites=exclude_trivial,
                        reaction_types=reaction_types, compartment_names=compartment_names,
                    )

                ko_growth = 0.0
                if after.get("status") == "success":
                    ko_growth = max(0.0, safe_float(after.get("biomass_flux")) or 0.0)
                ratio = (ko_growth / wt_growth) if wt_growth > 0 else None

                results[exch_id] = {
                    "exchange_id": exch_id,
                    "exchange_name": exch.name,
                    "internal_metabolite": {"id": int_met.id, "name": clean_metabolite_display_name(int_met)},
                    "biomass_reaction": biomass_rxn,
                    "before_ko": before,
                    "after_ko": after,
                    "growth_impact": {
                        "wt_growth": wt_growth,
                        "ko_growth": ko_growth,
                        "ko_growth_fraction": ratio,
                    },
                }
            except Exception as exc:
                results[exch_id] = {"error": str(exc)}

        return jsonify({
            "status": "success",
            "biomass_reaction": biomass_rxn,
            "results": results,
            "excluded_trivial_metabolites": exclude_trivial,
            "trivial_metabolite_rule": (
                "Currency/cofactor metabolites (H+, water, ATP/ADP/AMP and other "
                "nucleotides, NAD(H)/NADP(H), FAD(H2), CoA, phosphate/diphosphate, "
                "quinones) are excluded from traced paths so they don't obscure the "
                "pathway-specific route to biomass."
                if exclude_trivial else
                "Currency/cofactor metabolites are included in traced paths."
            ),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.post("/api/trace-pathway")
def api_trace_pathway():
    """General-purpose pathway tracer (a separate module the user runs on
    request — never triggered automatically). Unlike the biomass-specific
    exchange debugger, the start can be any reaction or metabolite and the
    target can be any metabolite; "levels" is the BFS depth cap. Reuses the
    exact same active-flux tracing helpers as debug_exchange_to_biomass.

    Optionally accepts "knockouts": a list of reaction/metabolite ids (or a
    comma-separated string). When given, the same start->target trace is
    run a second time with those reaction(s)/metabolite(s) blocked (inside
    a `with model:` context, so the uploaded model is never permanently
    changed), and the result is returned under "knockout" alongside the
    baseline ("before KO") trace at the top level — so a single request
    gives the frontend everything it needs to toggle between the two and
    highlight what changed.
    """
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    model_id = data.get("model_id")
    start_type = (data.get("start_type") or "metabolite").strip().lower()
    start_id = (data.get("start_id") or "").strip()
    target_metabolite_id = (data.get("target_metabolite_id") or "").strip()
    exclude_trivial = bool(data.get("exclude_trivial_metabolites", True))

    knockout_raw = data.get("knockouts") or []
    if isinstance(knockout_raw, str):
        knockout_raw = knockout_raw.split(",")
    knockout_ids = [str(x).strip() for x in knockout_raw if str(x).strip()]

    if not model_id or model_id not in MODEL_CACHE:
        return jsonify({"error": "Model not found or expired. Please re-run the initial analysis."}), 404
    if start_type not in ("metabolite", "reaction"):
        return jsonify({"error": "start_type must be 'metabolite' or 'reaction'"}), 400
    if not start_id:
        return jsonify({"error": "A starting reaction or metabolite id is required"}), 400
    if not target_metabolite_id:
        return jsonify({"error": "A target metabolite id is required"}), 400

    try:
        max_levels = max(1, min(100, int(data.get("max_levels", 15))))
    except (TypeError, ValueError):
        max_levels = 15
    try:
        max_paths = max(1, min(100, int(data.get("max_paths", 10))))
    except (TypeError, ValueError):
        max_paths = 10

    touch_model_cache(model_id)
    model, _ = MODEL_CACHE[model_id]

    try:
        target_met = model.metabolites.get_by_id(target_metabolite_id)
    except KeyError:
        return jsonify({"error": f"Target metabolite '{target_metabolite_id}' not found in model"}), 400

    flux_tolerance = 1e-9
    # Computed once and reused for both the baseline and (if requested) the
    # after-KO trace below — both depend only on model structure, not on
    # which reactions are knocked out.
    reaction_types = classify_reactions(model)
    compartment_names = {cid: compartment_display_name(cid, name) for cid, name in model.compartments.items()}

    try:
        baseline_solution = model.optimize()
    except Exception as exc:
        return jsonify({"error": f"FBA failed: {exc}"}), 500

    try:
        before = run_pathway_trace(
            model, baseline_solution, start_type, start_id, target_met,
            max_levels, max_paths, exclude_trivial, flux_tolerance,
            reaction_types, compartment_names,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if "error" in before:
        return jsonify({"error": before["error"]}), 400

    before_active_flux = before.pop("active_flux", {})

    response = {
        "status": "success",
        "start_type": start_type,
        "start_id": start_id,
        "start_metabolites": before["start_metabolites"],
        "target_metabolite": {
            "id": target_met.id, "name": clean_metabolite_display_name(target_met),
            "compartment": target_met.compartment,
            "compartment_name": compartment_names.get(target_met.compartment, target_met.compartment),
            "formula": getattr(target_met, "formula", None),
            "charge": clean(getattr(target_met, "charge", None)),
        },
        "num_active_reactions": before["num_active_reactions"],
        "paths": before["paths"],
        "growth": before["growth"],
        "excluded_trivial_metabolites": exclude_trivial,
        "max_levels": max_levels,
        "max_paths": max_paths,
    }

    # Optional comparative knockout: trace the same start->target again with
    # the requested reaction(s)/metabolite(s) blocked, so the two can be
    # toggled side by side in the UI.
    if knockout_ids:
        resolved_targets, not_found = resolve_knockout_targets(model, knockout_ids)
        if not resolved_targets:
            response["knockout"] = {
                "error": f"None of the requested knockout id(s) were found in the model: {', '.join(not_found)}",
                "not_found": not_found,
            }
        else:
            try:
                with model:
                    blocked_rxn_ids = apply_knockouts(model, resolved_targets)
                    try:
                        ko_solution = model.optimize()
                        ko_fba_error = None
                    except Exception as exc:
                        ko_solution = None
                        ko_fba_error = f"FBA failed after knockout: {exc}"

                    if ko_solution is not None:
                        after = run_pathway_trace(
                            model, ko_solution, start_type, start_id, target_met,
                            max_levels, max_paths, exclude_trivial, flux_tolerance,
                            reaction_types, compartment_names,
                        )
                    else:
                        after = {"error": ko_fba_error}

                    after_active_flux = after.pop("active_flux", {}) if "error" not in after else {}

                    # Flux comparison, limited to reactions that actually
                    # appear on the traced paths (before and/or after) —
                    # this is "the subset that it got", not the whole
                    # model's flux table, so the payload stays small and
                    # directly answers "did this KO affect this path".
                    path_rxn_ids = {r["id"] for p in response["paths"] for r in p["reactions"]}
                    if "error" not in after:
                        path_rxn_ids |= {r["id"] for p in after.get("paths", []) for r in p["reactions"]}

                    flux_comparison = {
                        rid: {
                            "before_flux": float(before_active_flux.get(rid, 0.0)),
                            "after_flux": float(after_active_flux.get(rid, 0.0)),
                            "active_before": rid in before_active_flux,
                            "active_after": rid in after_active_flux,
                        }
                        for rid in sorted(path_rxn_ids)
                    }
            except Exception as exc:
                response["knockout"] = {"error": str(exc), "not_found": not_found}
            else:
                if "error" in after:
                    response["knockout"] = {
                        "targets": resolved_targets,
                        "not_found": not_found,
                        "blocked_reactions": blocked_rxn_ids,
                        "after_ko": after,
                    }
                else:
                    # Clamp near-zero floating-point solver noise (a lethal
                    # knockout can solve to e.g. -1e-15 instead of exactly 0)
                    # up to 0, matching how stream_ko_analysis and
                    # debug_exchange_to_biomass's caller already treat KO
                    # growth elsewhere in this file. Without this, a fully
                    # lethal knockout could display as "-0%" retained.
                    wt_growth = max(0.0, safe_float(before["growth"]) or 0.0)
                    ko_growth = max(0.0, safe_float(after.get("growth")) or 0.0)
                    response["knockout"] = {
                        "targets": resolved_targets,
                        "not_found": not_found,
                        "blocked_reactions": blocked_rxn_ids,
                        "after_ko": after,
                        "growth_impact": {
                            "wt_growth": wt_growth,
                            "ko_growth": ko_growth,
                            "ko_growth_fraction": (ko_growth / wt_growth) if wt_growth > 0 else None,
                        },
                        "flux_comparison": flux_comparison,
                    }

    return jsonify(response)

if __name__ == "__main__":
    app.run(
        host="127.0.0.1", 
        port=5000, debug=True)
