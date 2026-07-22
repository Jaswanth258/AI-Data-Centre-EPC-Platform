import json
import re
from typing import Dict, Any, List
from .doc_parser import get_all_clause_headers, get_clause_text
from .llm_client import call_llm


def answer_rfi(question: str, history: List[Dict[str, str]] = None, scope: str = "all") -> Dict[str, Any]:
    """
    Answer an RFI question using two-step clause-tree navigation and optional conversation history.
      Step 0 — Handle meta questions directly without clause retrieval
      Step 1 — Rephrase follow-up question using chat history (if history exists)
      Step 2 — Show clause headers to LLM (scoped to specific file if requested)
      Step 3 — Fetch full clause text (lookup)
      Step 4 — Ask LLM to answer using fetched clauses and history context
    """

    # ── Step 0: Meta Question Handler ──
    META_TRIGGERS = [
        "what documents", "which documents", "what files", "what specs",
        "what is indexed", "what have you indexed", "what do you know about",
        "list documents", "show documents", "available specs", "indexed documents"
    ]
    q_lower = question.lower()
    if any(trigger in q_lower for trigger in META_TRIGGERS):
        from .doc_parser import _clause_trees, _doc_metadata
        if not _clause_trees:
            return {"answer": "No specification documents are currently indexed.", "sources": []}
        
        doc_lines = []
        for fname, clauses in _clause_trees.items():
            title = _doc_metadata.get(fname, {}).get('title', fname)
            doc_lines.append(f"- **{fname}** — {title} ({len(clauses)} clauses indexed)")
        
        doc_list = '\n'.join(doc_lines)
        return {
            "answer": f"I currently have the following specification documents indexed:\n\n{doc_list}",
            "sources": []
        }

    # ── Step 1: Query Condensation (Stateful RAG) ──
    search_query = question
    history_text = ""
    
    if history and len(history) > 0:
        history_lines = []
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            history_lines.append(f"{role.capitalize()}: {content}")
        history_text = "\n".join(history_lines)
        
        condense_prompt = f"""Given the following conversation history and a follow-up question, rephrase the follow-up question to be a standalone question that contains all necessary context from the history.
Do NOT answer the question. Just return the rephrased question as plain text.

History:
{history_text}

Follow-up Question: {question}

Standalone Question:"""

        try:
            condensed = call_llm(condense_prompt).strip()
            # Clean up potential LLM prefixing
            if ":" in condensed and not condensed.startswith("http"):
                parts = condensed.split(":", 1)
                if len(parts[0].split()) < 4:  # short label
                    condensed = parts[1].strip()
            if condensed:
                print(f"  [Stateful RFI] Condensed: '{question}' -> '{condensed}'")
                search_query = condensed
        except Exception as e:
            print(f"  [Stateful RFI] Failed to condense query: {e}")

    # ── Step 2: Get TOC — SCOPED or ALL ──
    if scope and scope != "all":
        from .doc_parser import _clause_trees, _doc_metadata
        target_file = scope
        if target_file not in _clause_trees:
            for indexed_file in _clause_trees:
                if target_file.lower() in indexed_file.lower() or indexed_file.lower() in target_file.lower():
                    target_file = indexed_file
                    break

        clauses = _clause_trees.get(target_file, [])
        if not clauses:
            return {"answer": f"Document '{scope}' is not currently indexed.", "sources": []}
        
        doc_title = _doc_metadata.get(target_file, {}).get('title', target_file)
        toc_lines = [f"\n--- {target_file} ({doc_title}) ---"]
        for c in clauses:
            toc_lines.append(f"  Clause {c['clause']} [{c['section']}]: {c['summary']}")
        toc = '\n'.join(toc_lines)
    else:
        toc = get_all_clause_headers()

    if not toc.strip():
        return {
            "answer": "No specification documents have been loaded.",
            "sources": []
        }

    # ── Step 3: Ask the LLM which clauses are relevant ──
    from .doc_parser import _doc_metadata
    doc_titles = [meta.get('title', fname) for fname, meta in _doc_metadata.items()]
    domain_hint = ', '.join(doc_titles) if doc_titles else "engineering specification"
    scope_hint = f"Focus ONLY on document: {scope}" if scope != "all" else "Search all documents."

    nav_prompt = f"""You are navigating a table of contents for the following specification documents:
{domain_hint}
{scope_hint}

Here are the available clauses:
{toc}

Question: {search_query}

Which clause number(s) are most likely to answer this question?
Return ONLY a JSON array of objects with "file" and "clause" keys. No other text.
Example: [{{"file": "sample_specification.md", "clause": "3.1"}}]"""

    nav_response = call_llm(nav_prompt)

    # Parse the navigation response
    clause_refs = _parse_clause_refs(nav_response)

    if not clause_refs:
        return {
            "answer": "I could not identify relevant specification clauses for your question.",
            "sources": []
        }

    # ── Step 4: Fetch the full text of each referenced clause ──
    fetched_clauses = []
    sources = []
    for ref in clause_refs[:5]:                           # cap at 5 clauses
        clause_data = get_clause_text(ref.get("file", ""), ref.get("clause", ""))
        if clause_data:
            fetched_clauses.append(clause_data)
            sources.append({
                "file":    clause_data["source_file"],
                "clause":  clause_data["clause"],
                "section": clause_data["section"],
                "snippet": (clause_data["text"][:200]
                            + ("..." if len(clause_data["text"]) > 200 else "")),
            })

    if not fetched_clauses:
        return {
            "answer": "I found clause references but could not retrieve their full text. "
                      "The clauses may not exist in the loaded documents.",
            "sources": []
        }

    # ── Step 5: Build the answer prompt ──
    context = ""
    for i, c in enumerate(fetched_clauses):
        context += (f"\n[{i+1}] Source: {c['source_file']}, "
                    f"Clause {c['clause']} ({c['section']})\n{c['text']}\n")

    history_context = ""
    if history_text:
        history_context = f"\nConversation History:\n{history_text}\n"

    answer_prompt = f"""You are AegisEPC, a Project Knowledge and RFI Intelligence Agent for: {domain_hint}.

Answer the user's latest question. You can use the conversation history for context, but base your answer ONLY on the specification clause text below. Do NOT add information from outside these clauses.

For every claim or requirement you mention, cite the source in the format [Source: filename, Clause X.X].

If the clauses do not contain the answer, say so clearly.

Specification Clauses:
{context}
{history_context}
Latest Question: {question}

Answer with precise citations:"""

    answer = call_llm(answer_prompt)

    return {
        "answer": answer,
        "sources": sources,
    }


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_clause_refs(raw: str):
    """Best-effort parse of the LLM navigation response into a list of refs."""
    cleaned = raw.strip()
    # Strip markdown fences
    if cleaned.startswith('```'):
        cleaned = '\n'.join(cleaned.split('\n')[1:])
    if cleaned.endswith('```'):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        result = json.loads(cleaned)
        return result if isinstance(result, list) else [result]
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: regex extraction
    pairs = re.findall(
        r'"file"\s*:\s*"([^"]+)".*?"clause"\s*:\s*"([^"]+"|\d+(?:\.\d+)*)', raw
    )
    return [{"file": f, "clause": c.replace('"', '')} for f, c in pairs]
