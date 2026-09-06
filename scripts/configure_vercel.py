"""Synchronize configured production values without logging their contents."""
import json
import os
import urllib.request


def main():
    project = os.environ['VERCEL_PROJECT_ID']
    team = os.environ['VERCEL_ORG_ID']
    token = os.environ['VERCEL_TOKEN']
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    values = {key: os.getenv(key, '').strip() for key in (
        'DATABASE_URL', 'SECRET_KEY', 'ENCRYPTION_KEY', 'SMTP_USER', 'SMTP_PASS_APP',
        'NOTION_CLIENT_ID', 'NOTION_CLIENT_SECRET', 'ADMIN_EMAIL', 'SMTP_FROM',
    )}
    values.update(ENVIRONMENT='production', APP_URL='https://trackr-alerts.vercel.app', SMTP_SERVER=os.getenv('SMTP_SERVER') or 'smtp.gmail.com', SMTP_PORT=os.getenv('SMTP_PORT') or '587')
    values['ADMIN_EMAIL'] = values['ADMIN_EMAIL'] or values['SMTP_USER']
    values['SMTP_FROM'] = values['SMTP_FROM'] or values['SMTP_USER']
    for key, value in values.items():
        if not value:
            continue
        body = {'key': key, 'value': value, 'type': 'sensitive', 'target': ['production']}
        request = urllib.request.Request(f'https://api.vercel.com/v10/projects/{project}/env?teamId={team}&upsert=true', data=json.dumps(body).encode(), headers=headers, method='POST')
        with urllib.request.urlopen(request, timeout=30):
            print(f'Configured {key}')


if __name__ == '__main__':
    main()
