import logo_onboarding
import voice_confirm_routes

voice_confirm_routes.whatsapp_webhook = logo_onboarding.whatsapp_webhook

try:
    import admin_tester_debug
    from db_backend import using_postgres

    def tester_table_exists(conn, table: str) -> bool:
        if using_postgres():
            return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", (table,)).fetchone())
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    admin_tester_debug._exists = tester_table_exists
except Exception:
    pass
