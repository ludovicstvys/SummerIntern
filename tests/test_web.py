import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trackr_app.database import Base, get_db
from trackr_app.main import app
from trackr_app.models import MagicLink, Preference, User, utcnow
from trackr_app.security import token_hash


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        def override_db():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)
        with self.Session() as db:
            user = User(email="invited@example.com")
            db.add(user); db.flush(); db.add(Preference(user_id=user.id)); db.commit()
            self.user_id = user.id

    def tearDown(self):
        app.dependency_overrides.clear()
        self.engine.dispose()

    @patch("trackr_app.main.send_magic_link")
    def test_unknown_email_is_not_enumerated(self, send):
        response = self.client.post("/auth/request", data={"email": "unknown@example.com"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("If%20the%20address%20is%20invited", response.headers["location"])
        send.assert_not_called()

    def test_magic_link_is_single_use(self):
        raw = "one-time-secret"
        with self.Session() as db:
            db.add(MagicLink(user_id=self.user_id, token_hash=token_hash(raw), expires_at=utcnow() + timedelta(minutes=15)))
            db.commit()
        first = self.client.get(f"/auth/consume/{raw}", follow_redirects=False)
        second = self.client.get(f"/auth/consume/{raw}", follow_redirects=False)
        self.assertEqual(first.status_code, 303)
        self.assertIn("trackr_session", first.cookies)
        self.assertEqual(second.headers["location"].split("?")[0], "/login")

    def test_expired_magic_link_is_rejected(self):
        raw = "expired-secret"
        with self.Session() as db:
            db.add(MagicLink(user_id=self.user_id, token_hash=token_hash(raw), expires_at=utcnow() - timedelta(seconds=1)))
            db.commit()
        response = self.client.get(f"/auth/consume/{raw}", follow_redirects=False)
        self.assertEqual(response.headers["location"].split("?")[0], "/login")
