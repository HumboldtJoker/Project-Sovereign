"""
Tool registry for the ReAct agent.

Provides a clean interface for registering, discovering, and executing tools.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class Tool:
    """A callable tool that the agent can invoke."""

    def __init__(self, name: str, description: str, parameters: Dict, function: Callable):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.function = function

    def execute(self, **kwargs) -> Dict[str, Any]:
        try:
            result = self.function(**kwargs)
            return {"success": True, "data": result}
        except Exception as e:
            logger.error("Tool %s failed: %s", self.name, e)
            return {"success": False, "error": str(e)}

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Central registry for all agent tools."""

    def __init__(self):
        self.tools: Dict[str, Tool] = {}

    def register(self, tool: Tool):
        self.tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    def register_function(self, name: str, description: str, parameters: Dict, function: Callable):
        """Convenience method to register a function directly."""
        self.register(Tool(name, description, parameters, function))

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def list_tools(self) -> List[Dict]:
        return [tool.to_dict() for tool in self.tools.values()]

    def get_descriptions(self) -> str:
        descriptions = []
        for tool in self.tools.values():
            params = json.dumps(tool.parameters, indent=2)
            descriptions.append(
                f"{tool.name}:\n"
                f"  Description: {tool.description}\n"
                f"  Parameters: {params}\n"
            )
        return "\n".join(descriptions)

    def tool_names(self) -> List[str]:
        return list(self.tools.keys())
