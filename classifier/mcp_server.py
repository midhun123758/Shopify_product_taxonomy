import httpx
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("Shopify Taxonomy API")
BASE_URL = "http://localhost:80/api"

@mcp.tool()
def get_taxonomy_stats() -> dict:
    """Fetch the current product taxonomy classification statistics."""
    try:
        response = httpx.get(f"{BASE_URL}/products/stats/")
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def get_families_by_status(status: str, page: int = 1) -> dict:
    """
    Fetch a list of product families filtered by their status. 
    Valid statuses: PENDING, PROCESSING, COMPLETED, REVIEW
    """
    try:
        response = httpx.get(f"{BASE_URL}/families/", params={"status": status.upper(), "page": page})
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def get_family_details(family_id: int) -> dict:
    """Fetch the full details of a specific product family, including its assigned category and confidence score."""
    try:
        response = httpx.get(f"{BASE_URL}/families/{family_id}/")
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def analyze_family(family_id: int) -> dict:
    """Trigger the Gemini AI to analyze and classify a specific product family."""
    try:
        response = httpx.post(f"{BASE_URL}/families/{family_id}/analyze/")
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    mcp.run()
    