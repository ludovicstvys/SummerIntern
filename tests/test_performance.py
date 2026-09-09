"""Representative local PostgreSQL load; does not claim Neon/Vercel latency."""
import json
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import insert, select, text, create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
from tests.test_postgres import pg
from trackr_app.models import User, Preference, UserSession, Offer, OfferSource, UserOffer, utcnow
from trackr_app.security import token_hash
from trackr_app.config import settings
from trackr_app.main import app
from trackr_app.database import get_db


def test_25_concurrent_dashboard_requests_with_10000_offers_and_20_users(pg):
    with pg.begin() as connection:
        schema = connection.scalar(text('select current_schema()'))
        connection.execute(insert(User), [{'id': i+1, 'email': f'load{i}@example.com'} for i in range(20)])
        connection.execute(insert(Preference), [{'user_id': i+1, 'status': 'active'} for i in range(20)])
        connection.execute(insert(UserSession), [{'user_id': i+1, 'token_hash': token_hash(f'load-session-{i}'),
            'csrf_token': f'csrf-{i}', 'expires_at': utcnow()+timedelta(days=1)} for i in range(20)])
        connection.execute(insert(Offer), [{'id': i+1, 'canonical_url': f'https://example.com/load/{i}',
            'offer_url': f'https://example.com/load/{i}', 'name': f'Internship {i}', 'region': 'France',
            'programme_type': 'summer'} for i in range(10000)])
        connection.execute(insert(OfferSource), [{'offer_id': i+1, 'region': 'France', 'programme_type': 'summer',
            'season': settings.season} for i in range(10000)])
        for uid in range(1,21):
            connection.execute(insert(UserOffer), [{'user_id': uid, 'offer_id': oid} for oid in range(1,10001)])
    engine = create_engine(pg.url, pool_size=2, max_overflow=3, pool_timeout=5, pool_pre_ping=True,
        connect_args={'connect_timeout': 5, 'options': f'-csearch_path={schema} -clock_timeout=2000'})
    factory = sessionmaker(engine, expire_on_commit=False)
    def database():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    client = TestClient(app)
    def request(i):
        started = time.perf_counter()
        response = client.get('/dashboard?page=2', headers={'cookie': f'trackr_session=load-session-{i%20}'})
        assert response.status_code == 200
        return time.perf_counter()-started
    try:
        cold = request(0)
        with ThreadPoolExecutor(max_workers=25) as pool:
            latencies = list(pool.map(request, range(25)))
        warm = request(1)
        print(json.dumps({'benchmark': 'local-postgres', 'offers': 10000, 'users': 20,
            'concurrency': 25, 'first_request_s': round(cold, 3), 'warm_request_s': round(warm, 3),
            'concurrent_max_s': round(max(latencies), 3), 'concurrent_median_s': round(sorted(latencies)[12], 3)}))
    finally:
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()
