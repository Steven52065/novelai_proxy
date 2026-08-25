from .routes import router
from .upstreams import router as upstreams_router

router.include_router(upstreams_router)

__all__ = ["router"]
