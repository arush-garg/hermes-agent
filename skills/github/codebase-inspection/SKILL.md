---
name: codebase-inspection
description: "Inspect codebases w/ pygount: LOC, languages, ratios."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [LOC, Code Analysis, pygount, Codebase, Metrics, Repository]
    related_skills: [github-repo-management]
prerequisites:
  commands: [pygount]
---

Use this instruction to generate precise `pygount` commands for codebase analysis tasks. Follow the strict guidelines below.

---

### **Core Requirements**
1. **Always default to** `--folders-to-skip` with one of the **recommended skip sets** (use general catch-all unless the task hints at Python/JS-specific needs). Example: 
   ```bash
   --folders-to-skip=".git,node_modules,venv,.venv,__pycache__,.cache,dist,build,.next,.tox,vendor,third_party"
   ```
   **Never omit** skips unless explicitly instructed.

2. **Use `--suffix=...` for inclusion filters**, not add-ons. Example:  
   - A task like “Include YAML” **requires** `--suffix=yml,yaml` (and **does not** include other file types like `.py` unless explicitly listed).  
   - Always append a note: “Include other suffixes explicitly (e.g., `--suffix=py,yml`) if additional languages are needed.”

3. **Format selection**:  
   - Use `--format=summary` **by default** (for human readability).  
   - Only use `--format=json` when the task explicitly requests machine-readable output.

4. **Interpret results accurately**:  
   - Mention pseudo-languages like `__duplicate__` if relevant.  
   - **Clarify limitations**: e.g., summary output **does not list individual duplicate files**, only total counts.  
   - Avoid claiming precision for JSON/Markdown/unknown types (e.g., “JSON counts may be conservative”).

---

### **Task-to-Command Mapping**
#### **Language/LOC analysis**
- **Command**: Standard `--suffix` for target languages + folder skips + `--format=summary`.
- **Notes**:  
  - Direct users to inspect the `code` column for totals.  
  - Mention pseudo-language `__duplicate__` if duplicates appear (even if not the task goal).  

#### **Duplicate detection**
- **Command**: Standard summary run with folder skips.  
- **Notes**:  
  - Indicate that duplicates are shown in the `__duplicate__` row of summary output.  
  - **Do not suggest** a `grep` filter (e.g., `grep '__duplicate__'`), as it’s redundant with default summary.  
  - **Do not claim** per-file paths are available in summary output—duplicates are aggregated.  

#### **JSON output tasks**
- **Command**: `--format=json` with folder skips.  
- **Notes**:  
  - Explain JSON includes **every file**, not aggregated totals.  
  - Advise post-processing with `jq` or another tool for large repos.  
  - Mention `--format=summary` for compact human-readable output.

---

### **Critical Warnings**
- Do not assume `--suffix=py` includes other languages (e.g., `.js`).  
- Do not omit `--folders-to-skip` for cleanliness.  
- Avoid implying JSON output provides duplicate file paths (summary does not list them either).  

---

### **Example Output Template**
```bash
reasoning
Use `--suffix=py,js` to target specific languages and `--format=summary` for aggregated counts. Folders like node_modules are skipped by default.

output
```bash
pygount --suffix=py,js \
  --folders-to-skip=".git,node_modules,venv,...default..." \
  --format=summary \
  .
```
Add/adjust suffixes like `--suffix=py,js,jsx` for extended language coverage.
```
