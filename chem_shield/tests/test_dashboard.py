import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chem_shield.communication.device_connection import DeviceConnection
from chem_shield.data.store import TelemetryStore
from chem_shield.hmi.app import create_app


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database = str(Path(self.temp_dir.name) / "telemetry.db")
        self.store = TelemetryStore(database)
        self.store.add_telemetry({
            "timestamp": "2026-01-01T00:00:00+00:00",
            "machine_id": "M-01",
            "plc_id": "PLC-01",
            "motor_status": "RUNNING",
            "target_rpm": 1500,
            "rpm": 1200,
            "temperature": 45.5,
            "pressure": 2.1,
            "power": 7.4,
        })
        self.store.add_event({
            "timestamp": "2026-01-01T00:00:01+00:00",
            "source": "TEST_MODE",
            "machine_id": "M-01",
            "event_type": "PRESSURE_SPIKE",
            "parameter": "pressure",
            "old_value": "normal",
            "new_value": 8,
            "severity": "high",
        })
        with patch.object(DeviceConnection, "connect", side_effect=ConnectionError("offline")):
            app = create_app(
                database=database,
                auth_database=str(Path(self.temp_dir.name) / "auth.db"),
                admin_email="admin@example.test",
                admin_username="labadmin",
                admin_password="StrongAdmin-Pass-123",
            )
        app.testing = True
        self.client = app.test_client()
        self.sign_in_admin()

    def tearDown(self):
        self.temp_dir.cleanup()

    def sign_in_admin(self):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            csrf_token = session["_csrf_token"]
        response = self.client.post(
            "/login",
            data={
                "_csrf_token": csrf_token,
                "username": "labadmin",
                "password": "StrongAdmin-Pass-123",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_dashboard_page_is_available_from_the_existing_hmi_app(self):
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Machine security dashboard", response.data)
        self.assertIn(b"NOT CONNECTED", response.data)
        self.assertIn(b"NOT WIRED", response.data)
        self.assertIn(b"dashboard.js", response.data)

        script = self.client.get("/static/dashboard.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn(b"/api/telemetry", script.data)

        hmi = self.client.get("/")
        self.assertEqual(hmi.status_code, 200)
        self.assertIn(b'href="/dashboard"', hmi.data)

    def test_dashboard_reads_existing_telemetry_and_event_endpoints(self):
        telemetry = self.client.get("/api/telemetry")
        events = self.client.get("/api/events")

        self.assertEqual(telemetry.status_code, 200)
        self.assertEqual(telemetry.get_json()[0]["temperature"], 45.5)
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.get_json()[0]["event_type"], "PRESSURE_SPIKE")


if __name__ == "__main__":
    unittest.main()
