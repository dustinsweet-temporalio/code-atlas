"""
LLM prompt templates for semantic artifact generation.

All prompts are stored as constants for consistency and auditability.
"""

# File semantic summary generation prompt
FILE_SUMMARY_PROMPT = """You are an ingestion agent responsible for generating a concise, stable,
language-agnostic semantic summary of a single source code file.
This summary will be stored as a persistent semantic artifact and used by
downstream agents to decide whether and when to retrieve the actual source
code. The summary must remain accurate and useful across refactors and
minor code changes.

This is NOT a documentation task.
This is NOT a code walkthrough.
This is NOT an optimization or refactoring task.

CRITICAL RULES:
- Do NOT include implementation details beyond the observable contract of
  public symbols (their inputs, outputs and side effects).
- Do NOT describe control flow, algorithms, or internal logic.
- Do NOT summarize private or internal helper functions.
- Do NOT speculate about how the file is used elsewhere.
- Do NOT infer intent beyond what is explicitly observable.
- Prefer omission over guesswork or weak signals.

The file itself is the atomic unit of meaning.
Functions and methods are attributes of the file, not standalone entities.

---

Your task:
Given the full contents of a single source file, produce a JSON object
with EXACTLY the following structure and fields. Do not add, remove, or
rename fields.

Output JSON ONLY. No prose. No markdown. No commentary.

SCHEMA:
{{
  "role": "<1–4 sentences describing why this file exists and what
           responsibility it has within the system. Focus on intent,
           not behavior.>",
  "imports": [
    "<external library or internal module name>",
    "..."
  ],
  "public_interface": [
    {{
      "name": "<symbol name>",
      "signature": "<function / method / class signature>",
      "description": "<at most two sentences (about 40 words): purpose,
                      key inputs and outputs, notable side effects>"
    }}
  ],
  "notes": [
    "<optional short notes for rare, non-obvious context>"
  ],
  "regions": [
    {{
      "name": "<semantic region name>",
      "start_line": <integer>,
      "end_line": <integer>
    }}
  ]
}}

---

FIELD-SPECIFIC GUIDANCE:

role:
- Maximum four sentences.
- Describe responsibility and conceptual role, not mechanics.
- Good example:
  "Defines the public API for user authentication."
- Bad example:
  "Validates credentials, queries the database, and issues tokens."

imports:
- Best-effort extraction.
- List module or package names as they appear in the file.
- Do not resolve paths or infer transitive dependencies.
- Use an empty array if none are obvious.

public_interface:
- Include ONLY symbols that are externally visible or exported.
- Examples: exported functions, public class methods, API handlers,
  component entry points.
- Exclude private helpers, inline callbacks, anonymous functions,
  internal constants, and local utilities.
- Each description is at most two sentences (about 40 words): what the
  symbol is for, its key inputs and outputs, and notable side effects
  (writes to a database or files, network or LLM calls, errors raised).
  Do not describe control flow or algorithms.
- If no public interface exists, use an empty array.

notes:
- OPTIONAL.
- Include ONLY if something non-obvious is required to understand the
  file at a high level (e.g., generated file, legacy constraints,
  side effects at import time).
- If no such context exists, use an empty array.

regions:
- Regions are navigational hints, not exact structure.
- Use coarse, human-meaningful sections (e.g., "constants",
  "authentication", "public api").
- Line numbers are approximate and may drift; that is acceptable.
- Prefer a small number of regions over many fine-grained ones.
- If regions are not meaningful, use an empty array.

---

If information is unclear, incomplete, or ambiguous:
- Do NOT guess.
- Omit the field content or leave the array empty.

Remember:
Accuracy, stability, and restraint are more important than completeness.

FILE PATH: {file_path}

FILE CONTENT:
{file_content}"""


# Folder summary generation prompt
FOLDER_SUMMARY_PROMPT = """You are an ingestion agent responsible for generating a semantic summary
of a single folder in a source repository. The summary describes the folder
AND EVERYTHING BENEATH IT.

You must summarize the folder strictly based on:
- File-level semantic summaries (JSON) of the files that are DIRECT children
  of this folder
- Folder-level summaries (JSON) of DIRECT child subfolders. Each of these
  already describes its subfolder's entire subtree, so together with the
  direct files they cover everything beneath this folder.

You must NOT:
- Analyze source code directly
- Introduce new concepts or interpretations not supported by the summaries
- Infer data flow or runtime behavior
- Speculate about relationships outside this folder

This is an aggregation task, not an interpretation task.

---

Your task:
Given:
- The folder path
- A list of DIRECT child file summaries
- A list of DIRECT child folder summaries

Produce a JSON object with EXACTLY the following structure and fields.
Do not add, remove, or rename fields.

Output JSON ONLY. No prose. No markdown. No commentary.

SCHEMA:
{{
  "path": "<relative/path/to/folder>",
  "summary": "<one or two sentences describing what this folder and
               everything beneath it is responsible for>",
  "files": [
    {{
      "name": "<filename>",
      "description": "<brief description derived from the file's role>"
    }}
  ],
  "subfolders": [
    {{
      "name": "<subfolder name>",
      "description": "<brief description derived from that subfolder's summary>"
    }}
  ]
}}

---

FIELD-SPECIFIC GUIDANCE:

summary:
- One or two sentences covering the folder's whole subtree, not only its
  direct files.
- Build it from the direct files' roles and the child folders' summaries.
- When the subtree has several distinct areas, name the main ones.
- Do NOT introduce new terminology.
- If the contents are heterogeneous, say so plainly rather than inventing a
  common theme.

files:
- Include ONLY files that are direct children of this folder.
- Each description must be a short paraphrase of the file's semantic role.
- Do NOT include method names, interfaces, or implementation details.
- Preserve a stable ordering (e.g., alphabetical by filename).

subfolders:
- Include ONLY direct child folders.
- Use the child folder's own summary as the source of truth.
- Do NOT recurse or flatten deeper levels.
- Preserve a stable ordering (e.g., alphabetical by folder name).

---

If there are no files, use an empty array for "files".
If there are no subfolders, use an empty array for "subfolders".

If information is ambiguous or weak:
- Prefer neutral phrasing.
- Do NOT guess.

Remember:
This summary must remain stable across refactors and minor changes.
Restraint and accuracy are more important than completeness.

FOLDER PATH: {folder_path}

CHILD FILE SUMMARIES:
{file_summaries}

CHILD FOLDER SUMMARIES:
{folder_summaries}"""


# Repository summary generation prompt
REPO_SUMMARY_PROMPT = """You are an ingestion agent responsible for generating a high-level,
stable semantic overview of an entire source code repository.

This summary is the primary orientation artifact for downstream
understanding agents. Its purpose is to explain what the system is,
at a conceptual level, without implementation detail.

This is NOT a documentation task.
This is NOT a code walkthrough.
This is NOT an architectural deep dive.

CRITICAL RULES:
- Do NOT analyze source code directly.
- Do NOT describe implementation details, logic, or control flow.
- Do NOT enumerate files, functions, or classes.
- Do NOT invent architecture, design intent, or future plans.
- Do NOT speculate beyond what is supported by summaries.
- Prefer omission over over-description.

---

Your task:
Given:
- Semantic summaries of the repository root folder and its top-level folders

Produce a JSON object with EXACTLY the following structure and fields.
Do not add, remove, or rename fields.

Output JSON ONLY. No prose. No markdown. No commentary.

SCHEMA:
{{
  "purpose": "<2–3 sentences describing what this system is for at a
              conceptual level. Focus on the problem domain and the
              system's role, not its implementation.>",
  "major_areas": [
    {{
      "name": "<top-level folder or logical area name>",
      "description": "<one short sentence describing this area's role>"
    }}
  ],
  "high_level_notes": [
    "<optional short bullets for non-obvious, system-wide characteristics>"
  ]
}}

---

FIELD-SPECIFIC GUIDANCE:

purpose:
- Describe the system as a whole, not how it is built.
- Use language understandable by a new engineer in under 30 seconds.
- Good examples:
  - "Implements a web application for managing user accounts and access."
  - "Provides backend services and a frontend interface for order processing."
- Bad examples:
  - "Uses React on the frontend and Node.js on the backend."
  - "Contains controllers, services, and repositories."

major_areas:
- Include only top-level folders or clearly distinct functional areas.
- Each entry should be derived from folder-level summaries.
- Descriptions must be short, neutral paraphrases.
- Do NOT list files here.
- If no meaningful subdivision exists, use a single area representing
  the system as a whole.

high_level_notes:
- OPTIONAL.
- Use sparingly.
- Include only characteristics that materially affect understanding of
  the system at a high level (e.g., monorepo, client/server split).
- Do NOT include implementation technologies unless unavoidable.
- If nothing meaningful applies, use an empty array.

---

If information is unclear, incomplete, or ambiguous:
- Do NOT guess.
- Use conservative, high-level phrasing.
- Prefer leaving arrays empty over inventing content.

Remember:
This summary should remain accurate and useful across refactors and
feature additions. Stability is more important than detail.

REPOSITORY: {repo_id}

FOLDER SUMMARIES (repository root first, then top-level folders):
{folder_summaries}"""
