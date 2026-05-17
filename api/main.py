"""
KubeDeploy API Server
Receives GitHub webhooks and triggers Kaniko build jobs on Kubernetes.
A single POST replaces the 6-step manual containerization workflow.
"""

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from pathlib import Path
import hmac, hashlib, os, logging
from .builder import trigger_build
from .models import DeployRequest, BuildStatus
from .state import build_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="KubeDeploy",
    description="Git-to-Kubernetes CI/CD Deployment Platform",
    version="1.0.0",
)

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
DASHBOARD = Path(__file__).parent / "dashboard.html"


def verify_github_signature(payload: bytes, signature: str) -> bool:
    """Validate the HMAC-SHA256 signature from GitHub webhooks."""
    if not WEBHOOK_SECRET:
        return True  # Dev mode: skip validation
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.get("/")
async def dashboard():
    """Serve the build dashboard."""
    return FileResponse(DASHBOARD)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "kubedeploy"}


@app.post("/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    GitHub push webhook handler.
    Automatically triggers a build + deploy pipeline on every push to main.
    """
    payload_bytes = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")

    if not verify_github_signature(payload_bytes, sig):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event = request.headers.get("X-GitHub-Event", "")
    if event != "push":
        return JSONResponse({"message": f"Ignored event: {event}"})

    payload = await request.json()
    ref = payload.get("ref", "")
    if ref != "refs/heads/main":
        return JSONResponse({"message": f"Ignored ref: {ref}"})

    repo = payload["repository"]
    repo_url = repo["clone_url"]
    repo_name = repo["name"].lower().replace("_", "-")
    commit_sha = payload["after"][:7]
    image_tag = f"{repo_name}:{commit_sha}"

    logger.info(f"Received push: repo={repo_name} commit={commit_sha}")

    # Single API trigger — replaces 6 manual steps
    build_id = trigger_build(
        repo_url=repo_url,
        image_tag=image_tag,
        app_name=repo_name,
        commit_sha=commit_sha,
        background_tasks=background_tasks,
    )

    return JSONResponse({
        "message": "Build triggered",
        "build_id": build_id,
        "image_tag": image_tag,
        "status_url": f"/builds/{build_id}",
    }, status_code=202)


@app.post("/deploy")
async def manual_deploy(req: DeployRequest, background_tasks: BackgroundTasks):
    """
    Manual deploy endpoint — trigger a build from any repo URL + image tag.
    Useful for testing without a GitHub webhook.
    """
    build_id = trigger_build(
        repo_url=req.repo_url,
        image_tag=req.image_tag,
        app_name=req.app_name,
        commit_sha="manual",
        background_tasks=background_tasks,
        container_port=req.container_port,
    )
    return JSONResponse({
        "message": "Build triggered",
        "build_id": build_id,
        "status_url": f"/builds/{build_id}",
    }, status_code=202)


@app.get("/builds/{build_id}")
async def get_build_status(build_id: str):
    """Poll build and deployment status."""
    status = build_registry.get(build_id)
    if not status:
        raise HTTPException(status_code=404, detail="Build not found")
    return status


@app.get("/builds")
async def list_builds():
    """List all builds with their current status."""
    return {"builds": list(build_registry.values())}
