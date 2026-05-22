"""
Demo / local builder module.

When KUBEDEPLOY_DEMO=true (default for local dev), this simulates the full
pipeline with realistic delays so you can take screenshots showing all phases.

Set KUBEDEPLOY_DEMO=false in a real cluster to use the real k8s_client.
"""

import asyncio
import uuid
import logging
import os
from fastapi import BackgroundTasks
from .state import build_registry
from .models import BuildStatus, BuildPhase

logger = logging.getLogger(__name__)

DEMO_MODE = os.getenv("KUBEDEPLOY_DEMO", "true").lower() == "true"
REGISTRY = os.getenv("IMAGE_REGISTRY", "localhost:5000")
NAMESPACE = os.getenv("K8S_NAMESPACE", "kubedeploy")


def trigger_build(
    repo_url: str,
    image_tag: str,
    app_name: str,
    commit_sha: str,
    background_tasks: BackgroundTasks,
    container_port: int = 5000,
) -> str:
    build_id = str(uuid.uuid4())[:8]
    full_image = f"{REGISTRY}/{image_tag}"

    status = BuildStatus(
        build_id=build_id,
        app_name=app_name,
        image_tag=full_image,
        commit_sha=commit_sha,
        phase=BuildPhase.PENDING,
        logs=[f"[{build_id}] Build queued for {repo_url}"],
    )
    build_registry[build_id] = status.model_dump()

    if DEMO_MODE:
        background_tasks.add_task(run_demo_pipeline, build_id, repo_url, full_image, app_name, commit_sha)
    else:
        background_tasks.add_task(run_real_pipeline, build_id, repo_url, full_image, app_name, commit_sha, container_port)

    return build_id


async def run_demo_pipeline(
    build_id: str,
    repo_url: str,
    full_image: str,
    app_name: str,
    commit_sha: str,
):
    """Simulates the full CI/CD pipeline with realistic timing for demo/screenshots."""

    def log(phase: BuildPhase, msg: str, **kwargs):
        build_registry[build_id]["phase"] = phase.value
        build_registry[build_id]["logs"].append(msg)
        for k, v in kwargs.items():
            build_registry[build_id][k] = v
        logger.info(f"[{build_id}] {msg}")

    await asyncio.sleep(0.5)
    log(BuildPhase.BUILDING, f"[{build_id}] Cloning repo: {repo_url}")
    await asyncio.sleep(1.5)
    log(BuildPhase.BUILDING, f"[{build_id}] Creating Kaniko build job: kaniko-{app_name}-{build_id}",
        kaniko_job=f"kaniko-{app_name}-{build_id}")
    await asyncio.sleep(1.0)
    log(BuildPhase.BUILDING, f"[{build_id}] Kaniko: Resolving base image...")
    await asyncio.sleep(2.0)
    log(BuildPhase.BUILDING, f"[{build_id}] Kaniko: Building layer [1/4] — installing dependencies")
    await asyncio.sleep(2.0)
    log(BuildPhase.BUILDING, f"[{build_id}] Kaniko: Building layer [2/4] — copying source files")
    await asyncio.sleep(1.5)
    log(BuildPhase.BUILDING, f"[{build_id}] Kaniko: Building layer [3/4] — setting entrypoint")
    await asyncio.sleep(1.0)
    log(BuildPhase.PUSHING, f"[{build_id}] Kaniko: Building layer [4/4] — finalizing image")
    await asyncio.sleep(1.5)
    log(BuildPhase.PUSHING, f"[{build_id}] Pushing image: {full_image}")
    await asyncio.sleep(2.5)
    log(BuildPhase.PUSHING, f"[{build_id}] Image push complete: {full_image}")
    await asyncio.sleep(0.5)
    log(BuildPhase.DEPLOYING, f"[{build_id}] Provisioning Deployment: {app_name}-{build_id}",
        deployment_name=f"{app_name}-{build_id}")
    await asyncio.sleep(1.5)
    log(BuildPhase.DEPLOYING, f"[{build_id}] Deployment ready. Provisioning Service...")
    await asyncio.sleep(1.0)
    port = 31000 + (hash(build_id) % 1000)
    log(BuildPhase.DEPLOYING, f"[{build_id}] Service {app_name}-svc-{build_id} created on NodePort {port}",
        service_name=f"{app_name}-svc-{build_id}", service_port=port)
    await asyncio.sleep(0.5)
    log(BuildPhase.RUNNING,
        f"[{build_id}] App live at 127.0.0.1:{port}",
        service_ip="127.0.0.1")
    logger.info(f"[{build_id}] Pipeline complete.")


async def run_real_pipeline(
    build_id: str,
    repo_url: str,
    full_image: str,
    app_name: str,
    commit_sha: str,
    container_port: int = 5000,
):
    """Real pipeline used when KUBEDEPLOY_DEMO=false and a real cluster is available."""
    from .k8s_client import (
        create_kaniko_job,
        watch_job_until_complete,
        create_deployment,
        create_service,
        get_service_ip,
    )

    def update(phase: BuildPhase, log: str, **kwargs):
        build_registry[build_id]["phase"] = phase.value
        build_registry[build_id]["logs"].append(log)
        for k, v in kwargs.items():
            build_registry[build_id][k] = v
        logger.info(f"[{build_id}] {log}")

    try:
        update(BuildPhase.BUILDING, f"Creating Kaniko build job for {repo_url}")
        job_name = f"kaniko-{app_name}-{build_id}"
        create_kaniko_job(job_name=job_name, repo_url=repo_url, image=full_image, namespace=NAMESPACE)
        build_registry[build_id]["kaniko_job"] = job_name

        update(BuildPhase.PUSHING, f"Kaniko job {job_name} created, watching for completion...")
        success = await watch_job_until_complete(job_name, NAMESPACE)
        if not success:
            update(BuildPhase.FAILED, f"Kaniko job {job_name} failed")
            return

        update(BuildPhase.DEPLOYING, "Build complete. Provisioning Deployment + Service...")
        dep_name = f"{app_name}-{build_id}"
        create_deployment(name=dep_name, image=full_image, app_label=app_name, namespace=NAMESPACE, container_port=container_port)
        build_registry[build_id]["deployment_name"] = dep_name

        svc_name = f"{app_name}-svc-{build_id}"
        port = create_service(name=svc_name, app_label=app_name, namespace=NAMESPACE, container_port=container_port)
        build_registry[build_id]["service_name"] = svc_name
        build_registry[build_id]["service_port"] = port

        build_registry[build_id]["service_ip"] = "localhost"
        update(BuildPhase.RUNNING, f"App live at localhost:{port}")

    except Exception as e:
        logger.exception(f"Pipeline error")
        build_registry[build_id]["phase"] = BuildPhase.FAILED.value
        build_registry[build_id]["error"] = str(e)
        build_registry[build_id]["logs"].append(f"ERROR: {e}")
