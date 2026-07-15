---
name: design-md
description: Author/validate/export Google's DESIGN.md token spec files.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design, design-system, tokens, ui, accessibility, wcag, tailwind, dtcg, google]
    related_skills: [popular-web-designs, claude-design, excalidraw, architecture-diagram]
---

# DESIGN.md Assistant Instructions

You are working with DESIGN.md, Google's open spec (Apache-2.0, `google-labs-code/design.md`) for describing a visual identity to coding agents. One file combines YAML front matter (machine-readable design tokens) and a Markdown body (human-readable rationale in canonical sections).

## Core Capabilities

- **Author** new DESIGN.md files from a brand brief
- **Lint** existing DESIGN.md files for structure, token references, and WCAG contrast
- **Diff** two DESIGN.md files to detect regressions
- **Export** to Tailwind or W3C DTCG JSON

## Critical Workflow Rules

1. **For authoring tasks (creating a new file):**
   - You MUST use the `write_file` tool to actually create the file in the project root. Do not just output the file contents as a code block in your response.
   - After writing, you MUST run `npx -y @google/design.md lint <filename>` to validate it.
   - Parse the lint output (use `--format json` for structured findings) and FIX any errors (broken-ref, duplicate-section, invalid-color, invalid-dimension, invalid-typography) before returning.
   - Address WCAG contrast warnings by adjusting colors or adding notes about contrast.
   - Only after the file passes lint should you return the final path + a brief summary of what you built and any contrast/accessibility tradeoffs you made.

2. **For lint tasks (validating an existing file):**
   - Run `npx -y @google/design.md lint <file> --format json` and parse the output.
   - Report findings structurally: list each rule violation with its severity (error/warning/info), the line/section it points to, and a concrete suggested fix.
   - Do NOT fabricate lint output. If the CLI is not available, say so explicitly rather than guessing.

3. **For diff tasks:** Run `npx -y @google/design.md diff <old> <new>` and explain regressions in plain language.

4. **For export tasks:** Run `npx -y @google/design.md export --format <tailwind|dtcg> <file>` and write the output to a sibling file (`tailwind.theme.json` or `tokens.json`).

## File Anatomy Requirements

- **YAML front matter** (between `---` fences): must include `version: alpha` and `name:`; `colors:` strongly recommended.
- **Markdown body**: sections in this canonical order (1-8, missing sections are fine, present ones MUST follow this order, duplicates are errors):
  1. Overview (alias: Brand & Style)
  2. Colors
  3. Typography
  4. Layout (alias: Layout & Spacing)
  5. Elevation & Depth (alias: Elevation)
  6. Shapes
  7. Components
  8. Do's and Don'ts

- **Token reference syntax**: `{path.to.token}` — e.g., `{colors.primary}`, `{typography.h1.fontSize}`, `{rounded.md}`. References resolve by dotted path; `{primary}` alone does not work.

## YAML Pitfalls (frequently catch agents)

- **Hex colors MUST be quoted strings** in YAML, e.g., `primary: "#1A1C1E"`. Without quotes, `#` triggers YAML comment syntax or values get truncated.
- **Negative dimensions MUST be quoted**, e.g., `letterSpacing: "-0.02em"`. Unquoted `-0.02em` parses as a YAML flow sequence.
- **Spacing values with units** should be strings: `fontSize: "1rem"`, `padding: "12px 24px"`.
- **Token references in YAML** are also strings: `backgroundColor: "{colors.tertiary}"`.

## Component Property Whitelist

Only these properties are allowed on a component entry; anything else is a `unknown-component-property` warning:
`backgroundColor`, `textColor`, `typography`, `rounded`, `padding`, `size`, `height`, `width`.

**Variants are sibling keys, not nested**: use `button-primary-hover`, not `button-primary.hover` or `button-primary: { hover: ... }`.

## Token Type Formats

| Type | Format | Example |
|------|--------|---------|
| Color | `#` + 6-digit hex (sRGB), quoted | `"#1A1C1E"` |
| Dimension | number + unit, quoted if negative or multi-value | `48px`, `"-0.02em"`, `"12px 24px"` |
| Token reference | `{path.to.token}` in a quoted string | `"{colors.primary}"` |
| Typography | object with `fontFamily`, `fontSize`, `fontWeight`, `lineHeight`, `letterSpacing`, `fontFeature`, `fontVariation` | see example |

## Lint Rule Reference (7 rules)

- `broken-ref` (error) — `{colors.missing}` points at a non-existent token
- `duplicate-section` (error) — same `## Heading` appears twice
- `invalid-color`, `invalid-dimension`, `invalid-typography` (error)
- `wcag-contrast` (warning/info) — component `textColor` vs `backgroundColor` ratio against WCAG AA (4.5:1) and AAA (7:1)
- `unknown-component-property` (warning) — outside the whitelist

Exit code 1 = errors found. Always check the exit code in CI-style lint tasks.

## When to Use This Skill

- User asks for a DESIGN.md file, design tokens, or a design system spec
- User wants consistent UI/brand across multiple projects
- User pastes an existing DESIGN.md and asks to lint, diff, export, or extend it
- User asks to port a style guide into a format agents can consume
- User wants contrast / WCAG accessibility validation on their color palette

For purely visual inspiration or layout examples, use `popular-web-designs` instead. For process and taste when designing a one-off HTML artifact, use `claude-design`. This skill is for the formal spec file itself.

## Spec Source of Truth

- Repo: https://github.com/google-labs-code/design.md (Apache-2.0)
- CLI: `@google/design.md` on npm (use `npx -y @google/design.md ...` — no global install)
- Current spec version: `alpha` (as of Apr 2026) — spec is marked alpha, watch for breaking changes
- License of generated DESIGN.md files: whatever the user's project uses; the spec itself is Apache-2.0
