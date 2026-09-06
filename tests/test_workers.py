import json
import unittest
from datetime import time
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from trackr_app.database import Base
from trackr_app.models import Delivery, Offer, Preference, User
from trackr_app.workers import process_digests, process_immediate_alerts


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = User(email="person@example.com")
        self.db.add(self.user)
        self.db.flush()
        self.preference = Preference(user_id=self.user.id, status="active", program_types=json.dumps(["summer"]), regions=json.dumps(["France"]))
        self.db.add(self.preference)
        self.offer = Offer(canonical_url="https://example.com/job", offer_url="https://example.com/job", name="Intern", company="Example", region="France", programme_type="summer")
        self.db.add(self.offer)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("trackr_app.workers.send_email", return_value="<stable@example.com>")
    def test_immediate_delivery_is_claimed_and_sent(self, send):
        delivery = Delivery(user_id=self.user.id, offer_id=self.offer.id, mode="immediate")
        self.db.add(delivery)
        self.db.commit()
        self.assertEqual(process_immediate_alerts(self.db), 1)
        self.db.refresh(delivery)
        self.assertEqual(delivery.status, "sent")
        self.assertIsNone(delivery.processing_started_at)
        self.assertEqual(process_immediate_alerts(self.db), 0)
        send.assert_called_once()

    @patch("trackr_app.workers.send_email", return_value="<stable@example.com>")
    def test_digest_runs_after_target_only_once_per_local_day(self, send):
        self.preference.delivery_mode = "daily_digest"
        self.preference.digest_time = time(0, 0)
        delivery = Delivery(user_id=self.user.id, offer_id=self.offer.id, mode="daily_digest")
        self.db.add(delivery)
        self.db.commit()
        self.assertEqual(process_digests(self.db), 1)
        self.assertIsNotNone(self.preference.last_digest_date)
        self.assertEqual(process_digests(self.db), 0)
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
