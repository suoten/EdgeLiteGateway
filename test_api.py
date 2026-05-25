try:
    from edgelite.api.ai_models import router
    print(f"OK: router prefix = {router.prefix}, routes = {len(router.routes)}")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")
