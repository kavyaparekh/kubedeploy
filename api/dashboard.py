"""
Serve the dashboard HTML.
Add this to main.py imports.
"""
from fastapi.responses import FileResponse
from pathlib import Path

DASHBOARD = Path(__file__).parent / "dashboard.html"


def add_dashboard(app):
    @app.get("/")
    async def dashboard():
        return FileResponse(DASHBOARD)
