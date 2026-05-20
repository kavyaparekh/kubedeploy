"""
Kubernetes client module.

Handles all kubectl operations programmatically via the official Python client.
This is the event-driven controller that watches build job states and
auto-provisions Deployments and Services.
"""

import asyncio
import logging
import os
from kubernetes import client, config, watch

logger = logging.getLogger(__name__)

# Load kube config — in-cluster when running on k8s, local kubeconfig for dev
try:
    config.load_incluster_config()
    logger.info("Loaded in-cluster kubeconfig")
except Exception:
    config.load_kube_config()
    logger.info("Loaded local kubeconfig")

batch_v1 = client.BatchV1Api()
apps_v1 = client.AppsV1Api()
core_v1 = client.CoreV1Api()

REGISTRY_SECRET = os.getenv("REGISTRY_SECRET_NAME", "registry-credentials")


def create_kaniko_job(
    job_name: str,
    repo_url: str,
    image: str,
    namespace: str,
) -> None:
    """
    Create a Kaniko build job on Kubernetes.

    Kaniko runs as a pod and builds a Docker image from source WITHOUT requiring
    a Docker daemon — safe to run inside a Kubernetes cluster.

    The init container clones the Git repo; Kaniko then builds and pushes the image.
    """
    job = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=namespace,
            labels={"app": "kubedeploy", "component": "builder"},
        ),
        spec=client.V1JobSpec(
            ttl_seconds_after_finished=300,
            backoff_limit=1,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"job-name": job_name}
                ),
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    init_containers=[
                        client.V1Container(
                            name="git-clone",
                            image="alpine/git:latest",
                            command=["git", "clone", "--depth=1", repo_url, "/workspace"],
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name="workspace",
                                    mount_path="/workspace",
                                )
                            ],
                        )
                    ],
                    containers=[
                        client.V1Container(
                            name="kaniko",
                            image="gcr.io/kaniko-project/executor:latest",
                            args=[
                                f"--context=dir:///workspace",
                                f"--dockerfile=/workspace/Dockerfile",
                                f"--destination={image}",
                                "--cache=true",
                                "--cache-ttl=24h",
                            ],
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name="workspace",
                                    mount_path="/workspace",
                                ),
                                client.V1VolumeMount(
                                    name="docker-config",
                                    mount_path="/kaniko/.docker",
                                ),
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": "500m", "memory": "512Mi"},
                                limits={"cpu": "2", "memory": "2Gi"},
                            ),
                        )
                    ],
                    volumes=[
                        client.V1Volume(
                            name="workspace",
                            empty_dir=client.V1EmptyDirVolumeSource(),
                        ),
                        client.V1Volume(
                            name="docker-config",
                            secret=client.V1SecretVolumeSource(
                                secret_name=REGISTRY_SECRET,
                                items=[
                                    client.V1KeyToPath(
                                        key=".dockerconfigjson",
                                        path="config.json",
                                    )
                                ],
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )
    batch_v1.create_namespaced_job(namespace=namespace, body=job)
    logger.info(f"Created Kaniko job: {job_name}")


async def watch_job_until_complete(
    job_name: str,
    namespace: str,
    timeout: int = 600,
) -> bool:
    """
    Asynchronously watch a Kubernetes Job until it succeeds or fails.

    This is the event-driven controller loop: instead of polling on a fixed
    interval, we use the Kubernetes Watch API which streams events in real time.
    """
    loop = asyncio.get_event_loop()

    def _watch_sync() -> bool:
        w = watch.Watch()
        logger.info(f"Watching job {job_name}...")
        try:
            for event in w.stream(
                batch_v1.list_namespaced_job,
                namespace=namespace,
                field_selector=f"metadata.name={job_name}",
                timeout_seconds=timeout,
            ):
                job_obj = event["object"]
                status = job_obj.status

                if status.succeeded and status.succeeded >= 1:
                    logger.info(f"Job {job_name} succeeded")
                    w.stop()
                    return True

                if status.failed and status.failed >= 1:
                    logger.error(f"Job {job_name} failed")
                    w.stop()
                    return False

        except Exception as e:
            logger.exception(f"Watch error for {job_name}: {e}")
            return False

        return False

    # Run the synchronous watch in a thread pool so we don't block the event loop
    return await loop.run_in_executor(None, _watch_sync)


def create_deployment(
    name: str,
    image: str,
    app_label: str,
    namespace: str,
    replicas: int = 1,
    container_port: int = 8080,
) -> None:
    """
    Auto-provision a Kubernetes Deployment for the freshly built image.
    Called automatically by the controller after a successful Kaniko build.
    """
    deployment = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={"app": app_label, "managed-by": "kubedeploy"},
        ),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(
                match_labels={"app": app_label}
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"app": app_label}
                ),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name=app_label,
                            image=image,
                            ports=[client.V1ContainerPort(container_port=container_port)],
                            image_pull_policy="Always",
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": "100m", "memory": "128Mi"},
                                limits={"cpu": "500m", "memory": "512Mi"},
                            ),
                            liveness_probe=client.V1Probe(
                                http_get=client.V1HTTPGetAction(
                                    path="/",
                                    port=container_port,
                                ),
                                initial_delay_seconds=10,
                                period_seconds=30,
                            ),
                        )
                    ],
                    image_pull_secrets=[
                        client.V1LocalObjectReference(name=REGISTRY_SECRET)
                    ],
                ),
            ),
        ),
    )
    apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment)
    logger.info(f"Created Deployment: {name}")


def create_service(
    name: str,
    app_label: str,
    namespace: str,
    container_port: int = 8080,
) -> int:
    """
    Auto-provision a NodePort Service to expose the deployed app.
    Returns the assigned NodePort.
    """
    svc = client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={"app": app_label, "managed-by": "kubedeploy"},
        ),
        spec=client.V1ServiceSpec(
            selector={"app": app_label},
            type="NodePort",
            ports=[
                client.V1ServicePort(
                    port=80,
                    target_port=container_port,
                    protocol="TCP",
                )
            ],
        ),
    )
    result = core_v1.create_namespaced_service(namespace=namespace, body=svc)
    node_port = result.spec.ports[0].node_port
    logger.info(f"Created Service: {name} on NodePort {node_port}")
    return node_port


def get_service_ip(svc_name: str, namespace: str) -> str:
    """Get the external IP or node IP for a service."""
    try:
        nodes = core_v1.list_node()
        for node in nodes.items:
            for addr in node.status.addresses:
                if addr.type == "InternalIP":
                    return addr.address
    except Exception:
        pass
    return "localhost"
