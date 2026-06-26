from admin_dashboard import router as admin_router
from business_onboarding import router as onboarding_router

admin_router.include_router(onboarding_router)
