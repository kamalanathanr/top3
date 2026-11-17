"""
Web search module using DuckDuckGo API
"""

import requests
from urllib.parse import quote

def search_web(query: str, num_results: int = 5) -> str:
    """Search the web for current information using DuckDuckGo API"""
    try:
        url = f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_redirect=1"
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            data = response.json()
            results = []

            # Get abstract if available
            if data.get('AbstractText'):
                results.append(f"Summary: {data['AbstractText']}")

            # Get related topics
            related = data.get('RelatedTopics', [])
            for item in related[:num_results]:
                if isinstance(item, dict) and 'Text' in item:
                    results.append(item['Text'])

            return "\n".join(results) if results else "No web results found"
        else:
            return "Web search temporarily unavailable"
    except Exception as e:
        return f"Web search error: {str(e)}"
