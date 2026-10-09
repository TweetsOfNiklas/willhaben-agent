"""`python -m app` starts the dashboard (HOST/PORT env, default 0.0.0.0:8000)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, log_level="info")
