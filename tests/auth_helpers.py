"""Read the actual rendered form token (each page has its own timestamp)."""
import re


def auth_form(client, **data):
    response = client.get('/auth/password/request')
    token = re.search(r'name="form_token" value="([^"]+)"', response.text).group(1)
    return {'form_token': token, **data}


def consume(client, path, **kwargs):
    return client.post(path, data=auth_form(client), **kwargs)
