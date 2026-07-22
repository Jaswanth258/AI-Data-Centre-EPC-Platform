import os
import re
from typing import List, Dict, Any, Optional

# ---------------------------------------------------------------------------
# Global in-memory storage (populated once at startup via build_clause_trees)
# ---------------------------------------------------------------------------
_clause_trees: Dict[str, List[Dict[str, Any]]] = {}   # {filename: [clause_dict]}
_full_docs: Dict[str, str] = {}                        # {filename: raw markdown}
_doc_metadata: Dict[str, Dict[str, str]] = {}          # {filename: {title, ...}}


def _extract_metadata(content: str) -> Dict[str, str]:
    """Pull **Key:** Value pairs and the top-level # title from a markdown doc."""
    meta: Dict[str, str] = {}
    for m in re.finditer(r'\*\*([^*]+):\*\*\s*(.+)', content):
        key = m.group(1).strip().lower().replace(' ', '_')
        meta[key] = m.group(2).strip()
    title_m = re.match(r'^#\s+(.+)', content, re.MULTILINE)
    if title_m:
        meta['title'] = title_m.group(1).strip()
    return meta


def _parse_clauses(filepath: str) -> List[Dict[str, Any]]:
    """
    Parse a clause-numbered markdown specification file into a list of
    clause entries.  Each entry has:
        clause       - e.g. "1.4"
        text         - full clause text (may span multiple lines)
        section      - the ### heading this clause falls under
        summary      - first ~120 chars, used for the LLM table-of-contents
        source_file  - basename of the file
    """
    filename = os.path.basename(filepath)
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    _full_docs[filename] = content
    _doc_metadata[filename] = _extract_metadata(content)

    clauses: List[Dict[str, Any]] = []
    lines = content.split('\n')

    current_h2 = ""
    current_h3 = ""

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track H2 headers
        if stripped.startswith('## '):
            current_h2 = stripped[3:].strip()
            current_h3 = ""
            
            # Check if this H2 header starts with a clause number (like "7. ", "1. ", etc.)
            h2_clause_match = re.match(r'^(\d+)\.?\s+(.*)', current_h2)
            if h2_clause_match:
                clause_num = h2_clause_match.group(1)
                clause_title = h2_clause_match.group(2)
                
                # Check if it has direct text before any H3 heading
                has_direct_text = False
                temp_i = i + 1
                while temp_i < len(lines):
                    nxt = lines[temp_i].strip()
                    if not nxt:
                        temp_i += 1
                        continue
                    if nxt.startswith('### ') or nxt.startswith('## '):
                        break
                    has_direct_text = True
                    break
                    
                if has_direct_text:
                    clause_lines = []
                    i += 1
                    while i < len(lines):
                        nxt = lines[i].strip()
                        # Stop if we hit any heading (H2 or H3)
                        if nxt.startswith('## ') or nxt.startswith('# ') or nxt.startswith('### '):
                            break
                        if nxt:
                            clause_lines.append(nxt)
                        i += 1
                        
                    full_text = f"{clause_title}: " + ' '.join(clause_lines) if clause_lines else clause_title
                    clean = full_text.replace('**', '')
                    summary = clean[:120] + ('...' if len(clean) > 120 else '')
                    
                    clauses.append({
                        'clause':      clause_num,
                        'text':        full_text,
                        'section':     current_h2,
                        'summary':     summary,
                        'source_file': filename,
                    })
                    continue

            i += 1
            continue

        # Track H3 headers
        if stripped.startswith('### '):
            current_h3 = stripped[4:].strip()
            # Check if this H3 header starts with a clause number (like "3.1 ", "3.1.2 ", etc.)
            h3_clause_match = re.match(r'^(\d+(?:\.\d+)+)\s+(.*)', current_h3)
            if h3_clause_match:
                clause_num = h3_clause_match.group(1)
                clause_title = h3_clause_match.group(2)
                clause_lines = []

                i += 1
                while i < len(lines):
                    nxt = lines[i].strip()
                    # Stop if we hit any heading (starts with #) or a line starting with another clause number
                    if nxt.startswith('#') or re.match(r'^\d+(?:\.\d+)+\s', nxt):
                        break
                    if nxt:
                        clause_lines.append(nxt)
                    i += 1

                full_text = f"{clause_title}: " + ' '.join(clause_lines) if clause_lines else clause_title
                clean = full_text.replace('**', '')
                summary = clean[:120] + ('...' if len(clean) > 120 else '')

                clauses.append({
                    'clause':      clause_num,
                    'text':        full_text,
                    'section':     current_h2 or filename,
                    'summary':     summary,
                    'source_file': filename,
                })
                continue

            i += 1
            continue

        # Match inline clause numbers (like "1.1 The UPS system serving...")
        inline_match = re.match(r'^(\d+(?:\.\d+)+)\s+(.*)', stripped)
        if inline_match:
            clause_num = inline_match.group(1)
            clause_lines = [stripped]

            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt:
                    i += 1
                    continue
                # Stop on new inline clause, any header, or bold metadata
                if re.match(r'^\d+(?:\.\d+)+\s', nxt) or nxt.startswith('#') or nxt.startswith('**'):
                    break
                clause_lines.append(nxt)
                i += 1

            full_text = ' '.join(clause_lines)
            clean = full_text.replace('**', '')
            summary = clean[:120] + ('...' if len(clean) > 120 else '')

            # The section name is current_h3 (if set) or current_h2
            section_name = current_h3 or current_h2 or filename
            section_name = section_name.replace('#', '').strip()

            clauses.append({
                'clause':      clause_num,
                'text':        full_text,
                'section':     section_name,
                'summary':     summary,
                'source_file': filename,
            })
            continue

        i += 1

    return clauses


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_clause_trees(data_dir: str) -> None:
    """Walk data/specs/ and build the in-memory clause tree for every spec."""
    global _clause_trees
    specs_dir = os.path.join(data_dir, "specs")
    if not os.path.exists(specs_dir):
        print(f"WARNING: Specs directory not found at {specs_dir}")
        return

    for fname in sorted(os.listdir(specs_dir)):
        if fname.endswith('.md') or fname.endswith('.txt'):
            fpath = os.path.join(specs_dir, fname)
            print(f"  Parsing specification: {fname}")
            clauses = _parse_clauses(fpath)
            _clause_trees[fname] = clauses
            print(f"    -> {len(clauses)} clauses indexed")

    total = sum(len(v) for v in _clause_trees.values())
    print(f"Clause-tree ready: {total} clauses across {len(_clause_trees)} specification files.")


def rebuild_clause_trees(data_dir: str) -> None:
    """Clear all cached data and rebuild clause trees from scratch.
    Called after a user uploads a new specification document."""
    global _clause_trees, _full_docs, _doc_metadata
    _clause_trees.clear()
    _full_docs.clear()
    _doc_metadata.clear()
    print("Re-indexing specifications after upload...")
    build_clause_trees(data_dir)


def get_all_clause_headers() -> str:
    """
    Build a formatted table-of-contents string from every loaded spec file.
    This is what gets passed to the LLM for clause-navigation prompts --
    it contains *only* the clause numbers and short summaries, NOT the full
    clause text (keeps the context window small).
    """
    toc_lines: List[str] = []
    for source_file, clauses in _clause_trees.items():
        doc_title = _doc_metadata.get(source_file, {}).get('title', source_file)
        toc_lines.append(f"\n--- {source_file} ({doc_title}) ---")
        for c in clauses:
            toc_lines.append(
                f"  Clause {c['clause']} [{c['section']}]: {c['summary']}"
            )
    return '\n'.join(toc_lines)


def get_clause_text(source_file: str, clause_number: str) -> Optional[Dict[str, Any]]:
    """Fetch the full parsed clause dict by file name and clause number with fuzzy filename & tolerant clause matching."""
    # 1. Fuzzy filename matching if exact match fails
    clauses = _clause_trees.get(source_file, [])
    if not clauses:
        for indexed_file in _clause_trees:
            if (source_file.lower() in indexed_file.lower() or 
                indexed_file.lower() in source_file.lower()):
                clauses = _clause_trees[indexed_file]
                print(f"  [Fuzzy match] '{source_file}' -> '{indexed_file}'")
                break

    if not clauses:
        return None

    # 2. Tolerant clause number matching (e.g. "7" vs "7.0" vs "07")
    def normalize(cn: str) -> str:
        parts = str(cn).strip().split('.')
        parts = [p.lstrip('0') or '0' for p in parts]
        while len(parts) > 1 and parts[-1] == '0':
            parts.pop()
        return '.'.join(parts)

    target_norm = normalize(clause_number)

    # Exact or normalized match
    for c in clauses:
        if c['clause'] == clause_number or normalize(c['clause']) == target_norm:
            return c

    # Prefix fallback (e.g. target "7" matches clause "7.1" or vice versa)
    for c in clauses:
        c_norm = normalize(c['clause'])
        if c_norm.startswith(target_norm + '.') or target_norm.startswith(c_norm + '.') or c['clause'].startswith(clause_number + '.'):
            return c

    return None


def get_all_clauses_for_file(source_file: str) -> List[Dict[str, Any]]:
    """Return every clause for a given spec file."""
    return _clause_trees.get(source_file, [])


def get_submittal_content(data_dir: str, filename: str) -> str:
    """Read and return the raw markdown of a submittal file."""
    filepath = os.path.join(data_dir, "submittals", filename)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Submittal file not found: {filename}")
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()


def list_submittals(data_dir: str) -> List[str]:
    """List every .md/.txt file inside data/submittals/."""
    sub_dir = os.path.join(data_dir, "submittals")
    if not os.path.exists(sub_dir):
        return []
    return sorted(f for f in os.listdir(sub_dir)
                  if f.endswith('.md') or f.endswith('.txt'))


def list_specs(data_dir: str) -> List[str]:
    """List every spec file inside data/specs/."""
    specs_dir = os.path.join(data_dir, "specs")
    if not os.path.exists(specs_dir):
        return []
    return sorted(f for f in os.listdir(specs_dir)
                  if f.endswith('.md') or f.endswith('.txt'))

