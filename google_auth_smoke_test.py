"""Network-free checks for Google role mapping/session issuance.
Actual Google signature validation is performed by google-auth at runtime.
"""
import session_auth as sa

sa.GOOGLE_ADMIN_EMAILS = {"admin@gmail.com"}
sa.GOOGLE_OFFICER_EMAILS = {"officer@gmail.com"}
sa.GOOGLE_ADMIN_DOMAINS = {"admin.example.org"}
sa.GOOGLE_OFFICER_DOMAINS = {"inspect.example.org"}

assert sa._role_for_google_identity("admin@gmail.com") == "admin"
assert sa._role_for_google_identity("officer@gmail.com") == "officer"
assert sa._role_for_google_identity("person@inspect.example.org", "inspect.example.org") == "officer"
assert sa._role_for_google_identity("person@admin.example.org", "admin.example.org") == "admin"
# Email-domain alone is deliberately insufficient for domain-wide role access.
assert sa._role_for_google_identity("person@inspect.example.org", "") is None
assert sa._role_for_google_identity("random@gmail.com") is None

original = sa.verify_google_credential
try:
    sa.verify_google_credential = lambda credential: {
        "sub": "12345678901234567890",
        "email": "officer@gmail.com",
        "email_verified": True,
        "name": "Test Officer",
        "iss": "https://accounts.google.com",
    }
    result = sa.google_staff_login("mock-token")
    assert result["role"] == "officer"
    assert result["actor_id"] == "google:12345678901234567890"
    assert result["email"] == "officer@gmail.com"
    assert result["auth_provider"] == "google"
    assert sa.verify_token(result["token"])["role"] == "officer"

    sa.verify_google_credential = lambda credential: {
        "sub": "999",
        "email": "unknown@gmail.com",
        "email_verified": True,
        "name": "Unknown",
        "iss": "https://accounts.google.com",
    }
    try:
        sa.google_staff_login("mock-token")
        raise AssertionError("unapproved account was not denied")
    except sa.GoogleAccessDenied:
        pass
finally:
    sa.verify_google_credential = original

print("PASS: Google staff auth mapping/session smoke tests")
