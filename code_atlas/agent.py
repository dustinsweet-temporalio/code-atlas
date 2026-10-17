"""
Code Atlas - question answering agent.
AI-powered semantic navigation assistant that helps engineers explore and
understand codebases through persistent semantic indexes stored in MongoDB.

STATELESS PATTERN: Full conversation history passed in system prompt.
"""
from strands import Agent
from strands.models.anthropic import AnthropicModel
from tools import ALL_TOOLS
from tools.search.ensure_search_indexes import ensure_search_indexes
from utils.mongo import get_mongo_client
from utils.validation import validate_repository
from utils.formatters.report import report_status_line
from tools.report import load_report
from config import (
    MODEL_ID,
    AGENT_SYSTEM_PROMPT,
    REPO_SUMMARIES_COLLECTION,
    LOG_LEVEL,
    LOG_FORMAT,
    QUERY_MAX_TOKENS,
    QUERY_EFFORT,
    QUERY_THINKING,
    QUERY_TEMPERATURE,
)
import logging
import uuid
from typing import Dict, Any, List

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=LOG_FORMAT
)
logger = logging.getLogger(__name__)

# ============================================================================
# APPLICATION INITIALIZATION
# ============================================================================
logger.info(f"Code Atlas agent initialized with {len(ALL_TOOLS)} tools")

# Log tool breakdown for debugging
from tools import (
    ALL_NAVIGATION_TOOLS,
    ALL_SEARCH_TOOLS,
    ALL_SOURCE_TOOLS,
    ALL_SYSTEM_TOOLS,
)
logger.info(f"  - Navigation tools: {len(ALL_NAVIGATION_TOOLS)}")
logger.info(f"  - Search tools: {len(ALL_SEARCH_TOOLS)}")
logger.info(f"  - Source tools: {len(ALL_SOURCE_TOOLS)}")
logger.info(f"  - System tools: {len(ALL_SYSTEM_TOOLS)}")

# ============================================================================
# STATE MANAGEMENT
# ============================================================================
# Track which repositories have had indexes verified (per-session)
_indexed_repos = set()

# ============================================================================
# MESSAGE EXTRACTION
# ============================================================================

def extract_text_from_content(content: Any) -> str:
    """
    Extract plain text from various content formats.
    
    Handles:
    - Simple strings: "hello"
    - Content arrays: [{"text": "hello"}]
    - Content-block arrays
    """
    if content is None:
        return ""
    
    if isinstance(content, str):
        return content
    
    if isinstance(content, (int, float)):
        return str(content)
    
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                text_val = part["text"]
                if text_val is not None:
                    text_parts.append(str(text_val))
            elif isinstance(part, str):
                text_parts.append(part)
            elif part is not None:
                text_parts.append(str(part))
        return " ".join(text_parts).strip()
    
    if isinstance(content, dict):
        if "text" in content:
            text_val = content["text"]
            return str(text_val) if text_val is not None else ""
        elif "content" in content:
            return extract_text_from_content(content["content"])
    
    try:
        return str(content)
    except Exception as e:
        logger.warning(f"Could not convert content to string: {type(content)}, error: {e}")
        return ""


def extract_messages_from_payload(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Extract full conversation history from payload.
    
    Converts a chat payload to a simple message format:
    [
        {"role": "user", "content": "text"},
        {"role": "assistant", "content": "text"},
        ...
    ]
    """
    # Simple string payload
    if isinstance(payload, str):
        return [{"role": "user", "content": payload}]
    
    # Dictionary with prompt field
    if isinstance(payload, dict) and "prompt" in payload:
        return [{"role": "user", "content": payload["prompt"]}]
    
    # Chat format with messages array
    if isinstance(payload, dict) and "messages" in payload:
        messages = payload.get("messages", [])
        
        if not messages:
            logger.warning("Empty messages array, using default")
            return [{"role": "user", "content": "Hello! What repository would you like help with today?"}]
        
        # Convert to simple format
        simple_messages = []
        for message in messages:
            role = message.get("role")
            content = message.get("content", "")
            
            # Skip system messages
            if role == "system":
                continue
            
            # Extract text from content
            text_content = extract_text_from_content(content)
            
            if text_content and role in ["user", "assistant"]:
                simple_messages.append({
                    "role": role,
                    "content": text_content
                })
        
        if not simple_messages:
            logger.warning("No valid messages extracted, using default")
            return [{"role": "user", "content": "Hello! What repository would you like help with today?"}]
        
        logger.info(f"Extracted {len(simple_messages)} messages from payload")
        return simple_messages
    
    # Unknown format
    logger.warning(f"Unknown payload format: {type(payload)}")
    return [{"role": "user", "content": "Hello! What repository would you like help with today?"}]


REPO_OVERVIEW_MAX_CHARS = 8000


def repository_section(repo_id: str, overview: str | None, report_line: str = "") -> str:
    """The selected repository, its overview and its Temporal report status, appended to the instructions."""
    if overview and len(overview) > REPO_OVERVIEW_MAX_CHARS:
        overview = overview[:REPO_OVERVIEW_MAX_CHARS] + " ..."
    return f"""--- SELECTED REPOSITORY ---
repo_id: {repo_id}
The user selected this repository before starting the conversation. Use repo_id "{repo_id}" in every tool call.

Overview from the index (call get_repo_summary for the full summary or its Temporal lens):
{overview or "(The index has no repository summary yet; start from get_folder_summary with path '.')"}
{report_line}
--- END SELECTED REPOSITORY ---"""


def load_repo_overview(database_name: str) -> str | None:
    """The repository summary's prose, or None if the index has none."""
    doc = get_mongo_client()[database_name][REPO_SUMMARIES_COLLECTION].find_one(
        {"document_type": "repo_summary"}, {"prose_summary": 1}
    )
    return (doc or {}).get("prose_summary")


def load_report_line(repo_id: str) -> str:
    """One line on the stored Temporal report (exists, freshness, headline verdicts)."""
    try:
        doc, index_commit = load_report(repo_id)
        return report_status_line(doc, index_commit)
    except Exception as e:  # the report is optional context
        logger.warning(f"Could not read the Temporal report for {repo_id}: {e}")
        return ""


def build_conversation_prompt(
    base_system_prompt: str,
    messages: List[Dict[str, str]],
    repo_section: str = "",
) -> str:
    """
    Build system prompt with the selected repository and the full conversation history.
    
    NO SUMMARIZATION - keeps entire conversation history.
    
    Args:
        base_system_prompt: Base instructions for the agent
        messages: Full conversation history
        repo_section: The selected repository and its overview (repository_section)
        
    Returns:
        Complete system prompt with embedded conversation
    """
    if repo_section:
        base_system_prompt = f"{base_system_prompt}\n\n{repo_section}"

    # Exclude the latest user message (will be passed to stream_async separately)
    history_messages = messages[:-1] if len(messages) > 1 else []
    
    if not history_messages:
        # First message - no history
        return base_system_prompt
    
    # Build full conversation transcript
    transcript = "\n".join(
        f"{msg['role'].upper()}: {msg['content']}"
        for msg in history_messages
    )
    
    # Combine base prompt with conversation history
    return f"""{base_system_prompt}

--- CONVERSATION HISTORY ---
{transcript}
--- END HISTORY ---

Respond to the user's latest message, taking into account the full conversation history above."""


# ============================================================================
# MODEL
# ============================================================================

def build_model() -> AnthropicModel:
    """
    Build the Anthropic model from the environment (QUERY_* settings).

    Optional parameters are only sent when set, because support differs by model:
    5.x models reject non-default temperatures, Haiku 4.5 rejects `effort`.
    """
    params: Dict[str, Any] = {}
    if QUERY_EFFORT:
        params["output_config"] = {"effort": QUERY_EFFORT}
    if QUERY_THINKING:
        params["thinking"] = {"type": QUERY_THINKING}
    if QUERY_TEMPERATURE:
        params["temperature"] = float(QUERY_TEMPERATURE)

    # ANTHROPIC_API_KEY is read from the environment by the SDK.
    return AnthropicModel(model_id=MODEL_ID, max_tokens=QUERY_MAX_TOKENS, params=params)


# ============================================================================
# REPOSITORY MANAGEMENT
# ============================================================================

def ensure_repository_indexes(repo_id: str, database_name: str) -> None:
    """
    Ensure search indexes exist for a repository (runs once per session).
    
    Args:
        repo_id: Repository identifier
        database_name: Database name
    """
    global _indexed_repos
    
    # Skip if already verified this session
    if repo_id in _indexed_repos:
        logger.debug(f"Search indexes already verified for repo '{repo_id}' this session")
        return
    
    try:
        logger.info(f"Ensuring search indexes for repository '{repo_id}'...")
        result = ensure_search_indexes(database_name)
        
        if result["status"] == "created":
            logger.info(f"Search indexes created for '{repo_id}': {result['collections']}")
        elif result["status"] == "exists":
            logger.info(f"Search indexes already exist for '{repo_id}'")
        else:
            logger.warning(f"Search index verification had issues: {result['details']}")
        
        # Mark as verified for this session
        _indexed_repos.add(repo_id)
        
    except Exception as e:
        logger.error(f"Failed to ensure search indexes for '{repo_id}': {str(e)}")
        logger.warning("Text search may not work correctly, but agent will continue")


# ============================================================================
# MAIN ENTRYPOINT
# ============================================================================

async def stream_answer(payload):
    """
    Answer a chat request, yielding text chunks as they are produced.
    
    STATELESS PATTERN:
    1. Extract full conversation history and the selected repository from payload
    2. Validate the repository (the web app checks it first; this is a backstop)
    3. Build system prompt with the repository overview and the conversation history
    4. Create fresh agent with conversation-aware system prompt
    5. Stream response using ONLY the latest user message
    """
    request_id = str(uuid.uuid4())[:8]
    logger.info(f"[{request_id}] ►►► REQUEST START")
    
    try:
        # Extract full conversation history
        messages = extract_messages_from_payload(payload)
        
        logger.info(f"[{request_id}] Conversation: {len(messages)} total messages")
        
        # Validate latest message
        if not messages:
            logger.error(f"[{request_id}] No messages in payload")
            yield "Error: No messages provided."
            return
        
        latest_message = messages[-1]
        latest_user_message = latest_message["content"]
        latest_role = latest_message["role"]
        
        if latest_role != "user":
            logger.warning(
                f"[{request_id}] Latest message is not from user (role={latest_role})"
            )
            latest_user_message = "Please continue."
        
        logger.info(f"[{request_id}] Latest user message: {latest_user_message[:100]}...")
        
        repo_id = (payload.get("repo_id") or "").strip() if isinstance(payload, dict) else ""
        is_valid, database_name, error = validate_repository(repo_id)
        if not is_valid:
            logger.warning(f"[{request_id}] Repository '{repo_id}' rejected: {error}")
            yield f"Error: repository '{repo_id}' is not available ({error}). Select an index and start a new chat."
            return
        ensure_repository_indexes(repo_id, database_name)

        # Build system prompt with the selected repository and full conversation history
        system_prompt = build_conversation_prompt(
            base_system_prompt=AGENT_SYSTEM_PROMPT,
            messages=messages,
            repo_section=repository_section(repo_id, load_repo_overview(database_name), load_report_line(repo_id)),
        )
        logger.info(f"[{request_id}] Repository: {repo_id}")
        
        logger.info(f"[{request_id}] System prompt built with {len(messages)-1} historical messages")
        
        # Create conversation-aware agent with all tools
        conversation_agent = Agent(
            model=build_model(),
            system_prompt=system_prompt,
            tools=ALL_TOOLS,
            callback_handler=None,
        )
        
        logger.info(f"[{request_id}] Agent created with conversation context and {len(ALL_TOOLS)} tools")
        
        # Stream response
        buffer = ""
        chunk_count = 0

        logger.info(f"[{request_id}] Starting stream...")

        # Pass ONLY the latest user message (context is in system prompt)
        async for event in conversation_agent.stream_async(latest_user_message):
            if "data" in event:
                chunk_text = event.get("data", "")
                
                if chunk_text is None:
                    continue
                
                if not isinstance(chunk_text, str):
                    chunk_text = str(chunk_text)
                
                if chunk_text:
                    chunk_count += 1
                    
                    # Yield chunks directly for code fences
                    # Check if this chunk or buffer contains code fence markers
                    combined = buffer + chunk_text
                    
                    if "```" in combined:  # In code block or building up to one
                        buffer += chunk_text
                        
                        # Yield complete lines when we have them
                        while "\n" in buffer:
                            line_end = buffer.index("\n") + 1
                            yield buffer[:line_end]
                            buffer = buffer[line_end:]
                    else:
                        # Normal text - use word boundary logic
                        buffer += chunk_text
                        
                        import re
                        safe_break_pattern = r'(\S+\s+)'
                        matches = list(re.finditer(safe_break_pattern, buffer))
                        
                        if matches:
                            last_match = matches[-1]
                            last_safe_pos = last_match.end()
                            output_text = buffer[:last_safe_pos]
                            
                            if output_text:
                                yield output_text
                            
                            buffer = buffer[last_safe_pos:]

        # Output remaining buffer
        if buffer:
            yield buffer
        
        logger.info(f"[{request_id}] ◄◄◄ REQUEST COMPLETE ({chunk_count} chunks)")
        
    except Exception as e:
        logger.error(f"[{request_id}] ❌ ERROR: {e}", exc_info=True)
        error_message = f"\n\n❌ **Error processing request:** {str(e)}\n\nPlease try rephrasing your question or contact support if the issue persists."
        yield error_message
