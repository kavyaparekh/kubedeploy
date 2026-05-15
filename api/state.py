"""
Build state registry.
In production this would be backed by Redis or a DB.
For the demo, in-memory dict is sufficient.
"""

from typing import Dict

build_registry: Dict[str, dict] = {}
