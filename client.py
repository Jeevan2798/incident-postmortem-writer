"""
Typed HTTP Client for the Incident Post-Mortem Writer OpenEnv environment.
Follows the OpenEnv HTTPEnvClient pattern with typed _step_payload and _parse_result.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import requests


class PostMortemEnv:
    """
    Typed HTTP client for the Incident Post-Mortem Writer OpenEnv environment.
    Follows the OpenEnv HTTPEnvClient pattern.

    Usage (sync):
        with PostMortemEnv(base_url="http://localhost:7860") as env:
            result = env.reset(difficulty="easy")
            obs    = result["observation"]
            result = env.step({"action_type": "QUERY_LOGS",
                               "query_service": "payments",
                               "query_from": "03:38", "query_to": "03:45"})
            result = env.submit()

    Usage (WebSocket — persistent session):
        import websockets, asyncio, json
        async with websockets.connect("ws://localhost:7860/ws") as ws:
            await ws.send(json.dumps({"command": "reset", "difficulty": "easy"}))
            result = json.loads(await ws.recv())
    """

    def __init__(self, base_url: str = "http://localhost:7860", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout  = timeout
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # OpenEnv core API — matches HTTPEnvClient interface
    # ------------------------------------------------------------------

    def _step_payload(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Convert typed action dict to JSON payload for HTTP POST /step."""
        return action

    def _parse_result(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Parse HTTP JSON response into validated result dict with observation, reward, done, info."""
        return {
            "observation": payload.get("observation", {}),
            "reward": float(payload.get("reward", {}).get("total", 0.0) if isinstance(payload.get("reward"), dict) else payload.get("reward") or 0.0),
            "done": bool(payload.get("done", False)),
            "info": payload.get("info", {}),
        }

    def _parse_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Parse HTTP JSON response from GET /state."""
        return payload

    def reset(self, difficulty: str = "easy") -> Dict[str, Any]:
        """Reset environment. Returns StepResult with observation, reward, done, info."""
        resp = self._session.post(
            f"{self.base_url}/reset",
            json={"difficulty": difficulty},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self._parse_result(resp.json())

    def step(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute one action. Returns StepResult with observation, reward, done, info."""
        resp = self._session.post(
            f"{self.base_url}/step",
            json=self._step_payload(action),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self._parse_result(resp.json())

    def state(self) -> Dict[str, Any]:
        """Get current episode state."""
        resp = self._session.get(f"{self.base_url}/state", timeout=self.timeout)
        resp.raise_for_status()
        return self._parse_state(resp.json())

    def health(self) -> bool:
        """Returns True if server is healthy."""
        try:
            resp = self._session.get(f"{self.base_url}/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def tasks(self) -> Dict[str, Any]:
        """List all available tasks."""
        resp = self._session.get(f"{self.base_url}/tasks", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Typed convenience helpers
    # ------------------------------------------------------------------

    def query_logs(self, service: str, from_time: str, to_time: str) -> Dict[str, Any]:
        return self.step({"action_type": "QUERY_LOGS",
                          "query_service": service,
                          "query_from": from_time,
                          "query_to": to_time})

    def write_section(self, section_name: str, content: str) -> Dict[str, Any]:
        return self.step({"action_type": "WRITE_SECTION",
                          "section_name": section_name,
                          "section_content": content})

    def assign_action_item(self, description: str, owner: str, due_date: str) -> Dict[str, Any]:
        return self.step({"action_type": "ASSIGN_ACTION_ITEM",
                          "action_item_description": description,
                          "action_item_owner": owner,
                          "action_item_due_date": due_date})

    def submit(self) -> Dict[str, Any]:
        return self.step({"action_type": "SUBMIT"})

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._session.close()
