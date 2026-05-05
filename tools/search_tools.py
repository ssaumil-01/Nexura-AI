import os
from langchain_core.tools import tool

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

@tool
def web_search(query: str) -> str:
    """Useful to search the web for documentation, API references, or troubleshooting. 
    Input should be a clear, specific search query. Returns a summary of web results."""
    if DDGS is None:
        return "ERROR: duckduckgo-search module is not available. Please ensure it is installed."
    try:
        # Use native DDGS to avoid Langchain community version conflicts
        ddgs = DDGS()
        results = ddgs.text(query, max_results=3)
        if not results:
            return "No results found."
        
        output = []
        for r in results:
            output.append(f"Title: {r.get('title')}\nLink: {r.get('href')}\nSnippet: {r.get('body')}\n")
            
        return "\n".join(output)
    except Exception as e:
        return f"Web search failed: {e}"
