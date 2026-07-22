import json
import re
from typing import Dict, Any, List
from .doc_parser import get_all_clause_headers, get_clause_text, get_submittal_content
from .llm_client import call_llm


# -- JSON parsing helper -------------------------------------------------------

def _parse_json(content: str) -> Any:
    """Best-effort parse JSON from LLM output, tolerating markdown fences."""
    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r'(\[.*\]|\{.*\})', cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Cannot parse JSON: {content[:300]}...")


# -- Public entry point --------------------------------------------------------

def verify_submittal(submittal_filename: str, data_dir: str) -> List[Dict[str, Any]]:
    """
    Run a compliance audit on *submittal_filename* against the loaded specs.

    Pipeline (two-step, each step inspectable):
      Step 1 – LLM extracts every technical parameter from the submittal as JSON
      Step 2 – For each parameter:
               a) LLM navigates the clause-header tree to pick the relevant spec clause
               b) Full clause text is fetched (dict lookup, not vector search)
               c) LLM compares submitted value against the spec requirement and returns
                  status / severity / confidence / explanation
    """

    # Load submittal text
    try:
        submittal_text = get_submittal_content(data_dir, submittal_filename)
    except Exception as e:
        return [_error_row("File Load Error", str(e))]

    # --- STEP 1 -- Extract parameters ---
    extract_prompt = f"""You are an expert technical data extraction assistant for engineering documents.
Analyze the vendor submittal below and extract every technical specification/parameter.

For each parameter, extract:
- parameter_name: a clear, specific name (e.g., "Battery Autonomy", "Short-time Withstand Rating")
- value: the numerical value or specific option provided
- unit: the unit of measurement, or "N/A" if none

Vendor Submittal:
{submittal_text}

Return ONLY a valid JSON array. No prose before or after.
Example: [{{"parameter_name": "Battery Autonomy", "value": "8", "unit": "minutes"}}]"""

    print(f"[Step 1] Extracting parameters from {submittal_filename}...")
    raw = call_llm(extract_prompt)

    try:
        parameters = _parse_json(raw)
        if not isinstance(parameters, list):
            raise ValueError("Extraction did not return a list")
    except Exception as e:
        print(f"  [FAIL] Extraction failed: {e}")
        return [_error_row("Parameter Extraction", str(e))]

    print(f"  [OK] Extracted {len(parameters)} parameters")

    # --- STEP 2 -- Navigate + Compare ---
    toc = get_all_clause_headers()
    report: List[Dict[str, Any]] = []

    for param in parameters:
        name  = param.get("parameter_name", "")
        value = param.get("value", "")
        unit  = param.get("unit", "")
        if not name or value is None:
            continue

        submitted = f"{value} {unit}".strip() if unit and unit.lower() != "n/a" else str(value).strip()

        # Step 2a — Navigate: which clause covers this parameter?
        from .doc_parser import _doc_metadata
        doc_titles = [meta.get('title', fname) for fname, meta in _doc_metadata.items()]
        domain_hint = ', '.join(doc_titles) if doc_titles else "engineering specification"

        nav_prompt = f"""You are navigating specification clauses for: {domain_hint}.

Available clauses:
{toc}

Which single clause is most relevant for verifying this vendor-submitted parameter?
Parameter: {name} = {submitted}

Return ONLY a JSON object: {{"file": "filename.md", "clause": "X.X"}}
If no clause is relevant, return: {{"file": "none", "clause": "none"}}"""

        print(f"  [Nav] '{name}' -> ", end="")
        try:
            nav = _parse_json(call_llm(nav_prompt))
            t_file   = nav.get("file", "none")
            t_clause = nav.get("clause", "none")
        except Exception:
            t_file = t_clause = "none"

        if t_file == "none" or t_clause == "none":
            print("no matching clause")
            report.append({
                "parameter": name, "submitted": submitted,
                "required": "Not specified",
                "status": "UNABLE_TO_VERIFY", "severity": "None",
                "confidence": 0, "clause": "N/A",
                "explanation": "No matching specification clause found."
            })
            continue

        # Step 2b — Fetch the full clause text
        clause = get_clause_text(t_file, t_clause)
        if not clause:
            print(f"{t_file} Cl.{t_clause} (not found)")
            report.append({
                "parameter": name, "submitted": submitted,
                "required": "See spec", "status": "UNABLE_TO_VERIFY",
                "severity": "None", "confidence": 0,
                "clause": f"{t_file} (Clause {t_clause})",
                "explanation": f"Clause {t_clause} referenced but could not be found in {t_file}."
            })
            continue

        print(f"{t_file} Cl.{t_clause}")

        # Step 2c — Compare: judge compliance with confidence score
        compare_prompt = f"""You are AegisEPC, a Quality Compliance Agent for: {domain_hint}.
Cross-check a vendor-submitted parameter against the ground-truth specification clause.

Specification Requirement:
[File: {t_file}, Clause {t_clause}, Section: {clause['section']}]
{clause['text']}

Submitted Parameter:
{name} = {submitted}

Determine:
1. "status": COMPLIANT, DEVIATION, or UNABLE_TO_VERIFY
2. "severity": If DEVIATION — "Critical" (safety/capacity/Tier III violation), "Major" (efficiency/serviceability), "Minor" (formatting), or "None" if compliant
3. "confidence": Your confidence in this verdict, 0-100 (100 = absolutely certain)
4. "required": The exact spec requirement summarized briefly (e.g., "Min 10 minutes", "Min 65 kA for 3 cycles")
5. "explanation": A professional explanation referencing both the spec value and submitted value

Return ONLY valid JSON: {{"status": "...", "severity": "...", "confidence": ..., "required": "...", "explanation": "..."}}"""

        try:
            result = _parse_json(call_llm(compare_prompt))
            report.append({
                "parameter":  name,
                "submitted":  submitted,
                "required":   result.get("required", "See spec"),
                "status":     result.get("status", "UNABLE_TO_VERIFY"),
                "severity":   result.get("severity", "None"),
                "confidence": result.get("confidence", 0),
                "clause":     f"{t_file} (Clause {t_clause})",
                "explanation": result.get("explanation", "No explanation provided."),
            })
        except Exception as e:
            print(f"    [FAIL] Comparison failed for '{name}': {e}")
            report.append({
                "parameter": name, "submitted": submitted,
                "required": "Error", "status": "UNABLE_TO_VERIFY",
                "severity": "None", "confidence": 0,
                "clause": f"{t_file} (Clause {t_clause})",
                "explanation": f"Verification failed: {str(e)}"
            })

    return report


def _error_row(param: str, detail: str) -> Dict[str, Any]:
    return {
        "parameter": param, "submitted": "N/A", "required": "N/A",
        "status": "UNABLE_TO_VERIFY", "severity": "None",
        "confidence": 0, "clause": "N/A",
        "explanation": detail,
    }
