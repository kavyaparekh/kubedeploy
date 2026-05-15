from pydantic import BaseModel
from typing import Optional
from enum import Enum


class BuildPhase(str, Enum):
    PENDING = "PENDING"
    BUILDING = "BUILDING"
    PUSHING = "PUSHING"
    DEPLOYING = "DEPLOYING"
    RUNNING = "RUNNING"
    FAILED = "FAILED"


class BuildStatus(BaseModel):
    build_id: str
    app_name: str
    image_tag: str
    commit_sha: str
    phase: BuildPhase = BuildPhase.PENDING
    kaniko_job: Optional[str] = None
    deployment_name: Optional[str] = None
    service_name: Optional[str] = None
    service_ip: Optional[str] = None
    service_port: Optional[int] = None
    error: Optional[str] = None
    logs: list[str] = []


class DeployRequest(BaseModel):
    repo_url: str
    image_tag: str
    app_name: str
    container_port: int = 5000
