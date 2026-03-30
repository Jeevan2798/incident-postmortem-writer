"""
FastAPI server for the Incident Post-Mortem Writer environment.
Exposes all required OpenEnv endpoints:
  REST:      /health /reset /step /state /tasks /grade /docs
  WebSocket: /ws  (persistent session — required by OpenEnv spec)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from env.models import Action, ActionType, SectionName
from server.environment import PostMortemEnvironment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Incident Post-Mortem Writer",
    description=(
        "An OpenEnv environment where an AI agent learns to write structured "
        "incident post-mortems from raw alert logs and Slack threads. "
        "3 difficulty levels: easy, medium, hard."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_CONCURRENT_ENVS = int(os.environ.get("MAX_CONCURRENT_ENVS", "100"))
_sessions: Dict[str, PostMortemEnvironment] = {}

_http_envs: Dict[str, PostMortemEnvironment] = {
    "easy":   PostMortemEnvironment(difficulty="easy"),
    "medium": PostMortemEnvironment(difficulty="medium"),
    "hard":   PostMortemEnvironment(difficulty="hard"),
}
_active_difficulty = os.environ.get("DIFFICULTY", "easy")


def _get_http_env() -> PostMortemEnvironment:
    return _http_envs[_active_difficulty]


class ResetRequest(BaseModel):
    difficulty: str = "easy"


class ActionRequest(BaseModel):
    action_type: str
    section_name: str | None = None
    section_content: str | None = None
    query_service: str | None = None
    query_from: str | None = None
    query_to: str | None = None
    action_item_description: str | None = None
    action_item_owner: str | None = None
    action_item_due_date: str | None = None


def _parse_action(req: ActionRequest) -> Action:
    try:
        action_type = ActionType(req.action_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action_type '{req.action_type}'. Valid: {[a.value for a in ActionType]}"
        )
    section_name = None
    if req.section_name:
        try:
            section_name = SectionName(req.section_name)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid section_name '{req.section_name}'. Valid: {[s.value for s in SectionName]}"
            )
    return Action(
        action_type=action_type,
        section_name=section_name,
        section_content=req.section_content,
        query_service=req.query_service,
        query_from=req.query_from,
        query_to=req.query_to,
        action_item_description=req.action_item_description,
        action_item_owner=req.action_item_owner,
        action_item_due_date=req.action_item_due_date,
    )


def _parse_action_dict(data: dict) -> Action:
    req = ActionRequest(**{k: v for k, v in data.items() if k != "command"})
    return _parse_action(req)


@app.on_event("startup")
async def startup():
    logger.info("Incident Post-Mortem Writer environment starting...")
    logger.info(f"MAX_CONCURRENT_ENVS={MAX_CONCURRENT_ENVS}")
    logger.info("Scenarios loaded: easy, medium, hard. Server ready.")


@app.on_event("shutdown")
async def shutdown():
    logger.info(f"Shutting down. Cleaning {len(_sessions)} sessions.")
    _sessions.clear()


@app.get("/health")
def health() -> Dict[str, Any]:
    """Health check — required by OpenEnv validator."""
    return {
        "status": "healthy",
        "version": "1.0.0",
        "active_sessions": len(_sessions),
        "max_sessions": MAX_CONCURRENT_ENVS,
    }


@app.post("/reset")
def reset(req: ResetRequest = ResetRequest()) -> Dict[str, Any]:
    """Reset environment. difficulty: easy | medium | hard"""
    global _active_difficulty
    if req.difficulty not in _http_envs:
        raise HTTPException(status_code=400, detail=f"difficulty must be one of {list(_http_envs.keys())}")
    _active_difficulty = req.difficulty
    result = _get_http_env().reset()
    return result.dict()


@app.post("/step")
def step(req: ActionRequest) -> Dict[str, Any]:
    """Execute one action in the environment."""
    action = _parse_action(req)
    result = _get_http_env().step(action)
    return result.dict()


@app.get("/state")
def state() -> Dict[str, Any]:
    """Return current episode state."""
    return _get_http_env().state


@app.get("/tasks")
def list_tasks() -> Dict[str, Any]:
    """List all available tasks."""
    return {
        "tasks": [
            {"id": "easy",   "name": "Single-Service Database Outage",            "difficulty": "easy",   "max_steps": 25, "max_queries": 8},
            {"id": "medium", "name": "Cascading Microservices Failure",            "difficulty": "medium", "max_steps": 25, "max_queries": 8},
            {"id": "hard",   "name": "Multi-Service Degradation with False Causes","difficulty": "hard",   "max_steps": 25, "max_queries": 8},
        ]
    }


@app.post("/grade")
def grade_current() -> Dict[str, Any]:
    """Return current grade without ending the episode."""
    env = _get_http_env()
    if env._grade_result:
        return env._grade_result.dict()
    return {"message": "No grade yet. Call SUBMIT action to trigger grading."}


@app.get("/")
def root() -> Dict[str, str]:
    return {"name": "Incident Post-Mortem Writer", "version": "1.0.0",
            "docs": "/docs", "health": "/health", "tasks": "/tasks", "websocket": "/ws"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Persistent WebSocket session — each connection gets its own isolated env.

    Client sends JSON:
        {"command": "reset", "difficulty": "easy"}
        {"command": "step", "action_type": "QUERY_LOGS", "query_service": "payments", ...}
        {"command": "state"}
        {"command": "close"}

    Server responds with JSON:
        {"type": "reset_result", "data": {...}}
        {"type": "step_result",  "data": {...}}
        {"type": "state_result", "data": {...}}
        {"type": "error",        "message": "..."}
    """
    if len(_sessions) >= MAX_CONCURRENT_ENVS:
        await websocket.close(code=1013, reason="Max concurrent sessions reached")
        return

    await websocket.accept()
    session_id = str(uuid.uuid4())[:8]
    session_env = PostMortemEnvironment(difficulty="easy")
    _sessions[session_id] = session_env
    logger.info(f"WS session {session_id} opened. Active: {len(_sessions)}")

    try:
        await websocket.send_json({
            "type": "connected",
            "session_id": session_id,
            "message": "Send {\"command\": \"reset\", \"difficulty\": \"easy\"} to start.",
        })

        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=300)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "error", "message": "Session timeout."})
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON."})
                continue

            command = msg.get("command", "")

            if command == "reset":
                difficulty = msg.get("difficulty", "easy")
                if difficulty not in ["easy", "medium", "hard"]:
                    await websocket.send_json({"type": "error", "message": f"Invalid difficulty '{difficulty}'."})
                    continue
                session_env.difficulty = difficulty
                result = session_env.reset()
                await websocket.send_json({"type": "reset_result", "data": result.dict()})

            elif command == "step":
                try:
                    action = _parse_action_dict(msg)
                    result = session_env.step(action)
                    await websocket.send_json({"type": "step_result", "data": result.dict()})
                except HTTPException as e:
                    await websocket.send_json({"type": "error", "message": e.detail})
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": str(e)})

            elif command == "state":
                await websocket.send_json({"type": "state_result", "data": session_env.state})

            elif command == "close":
                await websocket.send_json({"type": "closed", "message": "Session closed."})
                break

            else:
                await websocket.send_json({"type": "error", "message": f"Unknown command '{command}'. Valid: reset, step, state, close"})

    except WebSocketDisconnect:
        logger.info(f"WS session {session_id} disconnected.")
    except Exception as e:
        logger.error(f"WS session {session_id} error: {e}")
    finally:
        _sessions.pop(session_id, None)
        logger.info(f"Session {session_id} cleaned up. Active: {len(_sessions)}")


def main():
    """Entry point for the OpenEnv server — callable via project.scripts."""
    import os
    import uvicorn
    uvicorn.run(
        "server.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "7860")),
        workers=int(os.environ.get("WORKERS", "1")),
    )


if __name__ == "__main__":
    main()
