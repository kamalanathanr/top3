"""
Claude CLI execution module
Handles running Claude queries and filtering responses
"""

import subprocess

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

def run_claude_query(prompt: str, raw: bool = False, model: str = DEFAULT_MODEL, timeout: int | None = 300, allowed_tools: list[str] | None = None) -> dict:
    """Execute Claude query via CLI subprocess.
    raw=True skips markdown filtering — use for queries that return JSON or plain text.
    model defaults to Haiku (fast, cheap); pass claude-sonnet-4-6 for complex tasks.
    timeout=None disables the subprocess timeout entirely.
    allowed_tools: list of tool names to enable, e.g. ["WebSearch"].
    """
    try:
        cmd = ['claude', '--model', model, '-p', prompt]
        if allowed_tools:
            cmd += ['--allowedTools', ','.join(allowed_tools)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if result.returncode != 0:
            return {
                'error': f"Claude CLI error: {result.stderr}",
                'success': False
            }

        output = result.stdout.strip()

        # Skip markdown filtering for raw mode (e.g. JSON responses)
        if raw:
            if not output:
                return {'error': 'Empty response from Claude', 'success': False}
            return {'output': output, 'success': True}

        # Aggressively remove permission requests, explanations, and non-structured content
        lines = output.split('\n')
        filtered_lines = []
        skip_until_next_section = False
        in_structured_content = False

        for line in lines:
            line_lower = line.lower()
            line_stripped = line.strip()

            # Skip permission/authorization blocks entirely
            if any(phrase in line_lower for phrase in [
                'i need permission',
                'i appreciate',
                'let me provide',
                'i apologize',
                'could you',
                'could you grant',
                'permission to search',
                'permission to perform',
                'authorization',
                'encountered a technical issue',
                'technical issue with real-time',
                'encounter'
            ]):
                skip_until_next_section = True
                continue

            # Skip conversational intro lines
            if line_lower.startswith('here are') or line_lower.startswith('based on'):
                continue

            # If we're skipping, keep going until we find a structured line (starts with ** or #)
            if skip_until_next_section:
                if line_stripped.startswith('**') or line_stripped.startswith('#'):
                    skip_until_next_section = False
                    in_structured_content = True
                    filtered_lines.append(line)
                continue

            # Keep structured content (lines with ** or -)
            if line_stripped.startswith('**') or line_stripped.startswith('-') or line_stripped.startswith('•'):
                in_structured_content = True
                filtered_lines.append(line)
            elif in_structured_content and line_stripped == '':
                # Keep blank lines in structured content
                filtered_lines.append(line)
            elif in_structured_content:
                filtered_lines.append(line)
            elif line_stripped.startswith('**'):
                # Start of structured content
                in_structured_content = True
                filtered_lines.append(line)

        output = '\n'.join(filtered_lines).strip()

        # If output is empty after filtering, return error
        if not output:
            return {
                'error': 'Permission request was stripped, no content available',
                'success': False
            }

        return {
            'output': output,
            'success': True
        }
    except subprocess.TimeoutExpired:
        return {
            'error': "Claude query timed out after 5 minutes",
            'success': False
        }
    except FileNotFoundError:
        return {
            'error': "Claude CLI not found. Make sure 'claude' command is available in PATH",
            'success': False
        }
    except Exception as e:
        return {
            'error': f"Error running Claude: {str(e)}",
            'success': False
        }
