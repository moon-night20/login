import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chem_shield.communication.device_connection import DeviceConnection
from chem_shield.hmi.app import create_app


ADMIN_EMAIL = "admin@example.test"
ADMIN_USERNAME = "labadmin"
ADMIN_PASSWORD = "StrongAdmin-Pass-123"
OPERATOR_EMAIL = "operator@example.test"
OPERATOR_USERNAME = "operator1"
OPERATOR_PASSWORD = "StrongOperator-Pass-123"


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        directory = Path(self.temp_dir.name)
        with patch.object(DeviceConnection, "connect", side_effect=ConnectionError("offline")):
            app = create_app(
                database=str(directory / "telemetry.db"),
                auth_database=str(directory / "auth.db"),
                admin_email=ADMIN_EMAIL,
                admin_username=ADMIN_USERNAME,
                admin_password=ADMIN_PASSWORD,
            )
        app.testing = True
        self.app = app
        self.client = app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf_token(self, client):
        with client.session_transaction() as session:
            if "_csrf_token" not in session:
                raise AssertionError("Expected a CSRF token in the session")
            return session["_csrf_token"]

    def sign_in(self, client, identity, password):
        client.get("/login")
        return client.post(
            "/login",
            data={
                "_csrf_token": self.csrf_token(client),
                "username": identity,
                "password": password,
            },
        )

    def add_operator(self):
        self.sign_in(self.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        self.client.get("/admin/users")
        return self.client.post(
            "/admin/users",
            data={
                "_csrf_token": self.csrf_token(self.client),
                "email": OPERATOR_EMAIL,
                "username": OPERATOR_USERNAME,
                "password": OPERATOR_PASSWORD,
            },
        )

    def test_virtual_machine_and_api_require_authentication(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertIn("/login", self.client.get("/").headers["Location"])
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.assertEqual(self.client.get("/api/telemetry").status_code, 401)

    def test_login_rejects_invalid_credentials(self):
        response = self.sign_in(self.client, "unknown", "incorrect")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Username or password is incorrect.", response.data)

    def test_login_page_and_stylesheet_are_public(self):
        page = self.client.get("/login")
        stylesheet = self.client.get("/login.css")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b'name="username"', page.data)
        self.assertNotIn(b'name="identity"', page.data)
        self.assertIn(b'name="password"', page.data)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(b".login-card", stylesheet.data)
        self.assertIn(b"place-items: center", stylesheet.data)
        stylesheet.close()

    def test_admin_can_create_operator_who_can_login_by_username(self):
        created = self.add_operator()
        self.assertEqual(created.status_code, 302)

        operator = self.create_operator_client()
        login = self.sign_in(operator, OPERATOR_USERNAME, OPERATOR_PASSWORD)
        self.assertEqual(login.status_code, 302)
        self.assertEqual(operator.get("/").status_code, 200)
        self.assertEqual(operator.get("/api/telemetry").status_code, 200)
        self.assertEqual(operator.get("/admin/users").status_code, 403)

    def test_email_address_is_not_accepted_as_login_name(self):
        self.add_operator()

        response = self.sign_in(self.create_operator_client(), OPERATOR_EMAIL, OPERATOR_PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Username or password is incorrect.", response.data)

    def test_admin_can_revoke_an_active_operator(self):
        self.add_operator()
        operator = self.create_operator_client()
        self.sign_in(operator, OPERATOR_USERNAME, OPERATOR_PASSWORD)

        response = self.client.post(
            "/admin/users/1/delete",
            data={"_csrf_token": self.csrf_token(self.client)},
        )

        self.assertEqual(response.status_code, 302)
        revoked = operator.get("/")
        self.assertEqual(revoked.status_code, 302)
        self.assertIn("/login", revoked.headers["Location"])

    def test_admin_login_and_management_page(self):
        response = self.sign_in(self.client, ADMIN_USERNAME, ADMIN_PASSWORD)

        self.assertEqual(response.status_code, 302)
        page = self.client.get("/admin/users")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Authorized accounts", page.data)
        self.assertIn(b"CREATE AUTHORIZED ACCOUNT", page.data)

    def test_machine_control_requests_require_a_csrf_token(self):
        self.sign_in(self.client, ADMIN_USERNAME, ADMIN_PASSWORD)

        response = self.client.post("/api/connection", json={"action": "disconnect"})

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid security token", response.data)

    def create_operator_client(self):
        return self.app.test_client()


if __name__ == "__main__":
    unittest.main()
