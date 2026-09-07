"""Fetch the public form exactly as a browser does before submitting it."""
def auth_form(client, **data):
    client.get('/auth/password/request')
    return {'form_token': client.cookies['trackr_auth_csrf'], **data}
