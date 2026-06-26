import admin_dashboard
import business_onboarding

admin_dashboard.router.add_api_route(
    "/onboarding",
    business_onboarding.onboarding_page,
    methods=["GET"],
    response_class=business_onboarding.HTMLResponse,
)
admin_dashboard.router.add_api_route(
    "/onboarding/api/trades",
    business_onboarding.trades,
    methods=["GET"],
)
admin_dashboard.router.add_api_route(
    "/onboarding/api/users",
    business_onboarding.onboarded_users,
    methods=["GET"],
)
admin_dashboard.router.add_api_route(
    "/onboarding/api/confirm",
    business_onboarding.confirm_profile,
    methods=["POST"],
)
admin_dashboard.router.add_api_route(
    "/onboarding/api/upload",
    business_onboarding.upload_branding,
    methods=["POST"],
)
admin_dashboard.router.add_api_route(
    "/onboarding/api/extract",
    business_onboarding.extract_letterhead,
    methods=["POST"],
)
