"""
Prose serialization for semantic artifacts.

Converts structured semantic summaries into natural language text
optimized for LLM consumption and full-text search.
"""

from semantic_ingress.models import (
    FileSemanticSummary,
    FolderSemanticSummary,
    RepositorySemanticSummary,
)


def serialize_file_summary(summary: FileSemanticSummary) -> str:
    """
    Serialize FileSemanticSummary to prose format.
    
    Args:
        summary: File semantic summary to serialize
    
    Returns:
        Natural language formatted string
    """
    parts = []
    
    # Role (as opening statement)
    if summary.role:
        parts.append(summary.role)
    
    # Imports (as natural sentence)
    if summary.imports:
        if len(summary.imports) == 1:
            parts.append(f"It imports {summary.imports[0]}.")
        elif len(summary.imports) == 2:
            parts.append(f"It imports {summary.imports[0]} and {summary.imports[1]}.")
        else:
            last = summary.imports[-1]
            others = ", ".join(summary.imports[:-1])
            parts.append(f"It imports {others}, and {last}.")
    
    # Public interface (as list of exports)
    if summary.public_interface:
        parts.append("\nPublic interface:")
        for symbol in summary.public_interface:
            parts.append(f"- {symbol.name}: {symbol.signature}")
            if symbol.description:
                parts.append(f"  {symbol.description}")
    
    # Regions (as structural description)
    if summary.regions:
        region_descs = []
        for region in summary.regions:
            region_descs.append(f"{region.name} (lines {region.start_line}-{region.end_line})")
        
        if len(region_descs) == 1:
            parts.append(f"\nThe file contains one semantic region: {region_descs[0]}.")
        else:
            parts.append(
                f"\nThe file contains {len(region_descs)} semantic regions: "
                f"{', '.join(region_descs[:-1])}, and {region_descs[-1]}."
            )
    
    # Notes (as additional context)
    if summary.notes:
        if len(summary.notes) == 1:
            parts.append(f"\nNote: {summary.notes[0]}")
        else:
            parts.append("\nAdditional notes:")
            for note in summary.notes:
                parts.append(f"- {note}")
    
    return "\n".join(parts)


def serialize_folder_summary(summary: FolderSemanticSummary) -> str:
    """
    Serialize FolderSemanticSummary to prose format.
    
    Args:
        summary: Folder semantic summary to serialize
    
    Returns:
        Natural language formatted string
    """
    parts = []
    
    # Path as heading
    path = summary.path if summary.path else "(root)"
    parts.append(f"Folder: {path}")
    
    # Summary (as opening statement)
    if summary.summary:
        parts.append(summary.summary)
    
    # Files (as natural list)
    if summary.files:
        if len(summary.files) == 1:
            f = summary.files[0]
            parts.append(f"\nContains one file: {f.name} - {f.description}")
        else:
            parts.append(f"\nContains {len(summary.files)} files:")
            for file_ref in summary.files:
                parts.append(f"- {file_ref.name}: {file_ref.description}")
    
    # Subtree size (deterministic counts)
    if summary.subtree and summary.subtree.total_folders:
        st = summary.subtree
        largest = sorted(st.by_subfolder.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
        breakdown = ", ".join(f"{name}: {count}" for name, count in largest)
        levels = "level" if st.max_depth == 1 else "levels"
        parts.append(
            f"\nSubtree: {st.total_files} files in {st.total_folders + 1} folders, "
            f"{st.max_depth} {levels} deep ({breakdown})."
        )

    # Subfolders (as natural list)
    if summary.subfolders:
        if len(summary.subfolders) == 1:
            sf = summary.subfolders[0]
            parts.append(f"\nContains one subfolder: {sf.name} - {sf.description}")
        else:
            parts.append(f"\nContains {len(summary.subfolders)} subfolders:")
            for subfolder_ref in summary.subfolders:
                parts.append(f"- {subfolder_ref.name}: {subfolder_ref.description}")
    
    return "\n".join(parts)


def serialize_repo_summary(summary: RepositorySemanticSummary) -> str:
    """
    Serialize RepositorySemanticSummary to prose format.
    
    Args:
        summary: Repository semantic summary to serialize
    
    Returns:
        Natural language formatted string
    """
    parts = []
    
    # Purpose (as opening statement)
    if summary.purpose:
        parts.append(summary.purpose)
    
    # Major areas (as structured list)
    if summary.major_areas:
        if len(summary.major_areas) == 1:
            area = summary.major_areas[0]
            parts.append(f"\nThe repository contains one major area: {area.name} - {area.description}")
        else:
            parts.append(f"\nThe repository contains {len(summary.major_areas)} major areas:")
            for area in summary.major_areas:
                parts.append(f"- {area.name}: {area.description}")
    
    # High-level notes (as additional context)
    if summary.high_level_notes:
        if len(summary.high_level_notes) == 1:
            parts.append(f"\nNote: {summary.high_level_notes[0]}")
        else:
            parts.append("\nAdditional notes:")
            for note in summary.high_level_notes:
                parts.append(f"- {note}")
    
    return "\n".join(parts)
