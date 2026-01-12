"""Health check endpoint for monitoring and readiness probes."""

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
import time
from typing import Dict, Any

from oogway.config import settings

app = FastAPI(title="Oogway Health Check")

# Store bot start time
START_TIME = time.time()


@app.get("/health")
async def health_check() -> JSONResponse:
    """
    Basic health check endpoint.

    Returns:
        JSON with status and uptime information
    """
    uptime = int(time.time() - START_TIME)
    return JSONResponse({
        "status": "healthy",
        "uptime_seconds": uptime,
        "service": "oogway-bot"
    })


@app.get("/readiness")
async def readiness_check() -> Response:
    """
    Kubernetes-style readiness probe.

    Returns:
        200 if service is ready to accept traffic
        503 if service is not ready
    """
    # Add checks for critical dependencies
    # For now, simple check
    try:
        # Could add DB connectivity check here
        # Could add Redis connectivity check here
        return Response(status_code=200, content="Ready")
    except Exception as e:
        return Response(status_code=503, content=f"Not ready: {e}")


@app.get("/liveness")
async def liveness_check() -> Response:
    """
    Kubernetes-style liveness probe.

    Returns:
        200 if service is alive
    """
    return Response(status_code=200, content="Alive")


@app.get("/metrics")
async def metrics() -> Dict[str, Any]:
    """
    Basic metrics endpoint.

    Returns:
        Dictionary with basic metrics
    """
    uptime = int(time.time() - START_TIME)
    return {
        "uptime_seconds": uptime,
        "start_time": START_TIME,
        # Add more metrics as needed:
        # - Number of linked users
        # - Number of matches tracked
        # - API call counts
        # - Error counts
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
