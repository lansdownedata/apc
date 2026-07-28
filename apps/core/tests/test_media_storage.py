"""Guards the prod media-storage contract.

Heroku's filesystem is ephemeral: every dyno restart and redeploy wipes
MEDIA_ROOT, so FileSystemStorage silently destroyed every uploaded
vehicle-type photo and left the customer-facing quote page 404ing. Media
therefore goes to S3 in prod.

The switch is keyed on AWS_STORAGE_BUCKET_NAME rather than being
unconditional, so that deploying this code *without* the AWS env vars set
leaves prod on the old behaviour instead of failing to boot. These tests
pin both halves of that fallback plus the security posture of the S3 side
(private bucket, presigned URLs, no silent overwrites).

Settings modules are reloaded rather than imported, so each case sees a
different environment. prod.py mutates base.MIDDLEWARE in place, hence the
snapshot/restore in the fixture.
"""

import importlib

import pytest

S3_BACKEND = "storages.backends.s3.S3Storage"
FS_BACKEND = "django.core.files.storage.FileSystemStorage"
WHITENOISE_BACKEND = "whitenoise.storage.CompressedManifestStaticFilesStorage"

AWS_VARS = (
    "AWS_STORAGE_BUCKET_NAME",
    "AWS_S3_REGION_NAME",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


@pytest.fixture
def load_prod(monkeypatch):
    """Reload config.settings.prod under a controlled environment."""
    import config.settings.base as base_mod

    original_middleware = list(base_mod.MIDDLEWARE)

    def _load(**overrides):
        for name in AWS_VARS:
            monkeypatch.delenv(name, raising=False)
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
        import config.settings.prod as prod_mod

        return importlib.reload(prod_mod)

    yield _load

    base_mod.MIDDLEWARE[:] = original_middleware


def test_falls_back_to_filesystem_when_bucket_unset(load_prod):
    """Deploying without the AWS env vars must not break prod."""
    prod = load_prod()

    assert prod.STORAGES["default"]["BACKEND"] == FS_BACKEND
    # The app must keep serving /media/ itself while there is no bucket.
    assert prod.SERVE_MEDIA is True


def test_uses_s3_when_bucket_set(load_prod):
    prod = load_prod(
        AWS_STORAGE_BUCKET_NAME="apc-media-prod",
        AWS_ACCESS_KEY_ID="test-key",
        AWS_SECRET_ACCESS_KEY="test-secret",
    )

    assert prod.STORAGES["default"]["BACKEND"] == S3_BACKEND
    assert prod.AWS_STORAGE_BUCKET_NAME == "apc-media-prod"
    # S3 serves the media; routing /media/ through the dyno would be dead weight.
    assert prod.SERVE_MEDIA is False


def test_s3_region_defaults_to_us_east_1(load_prod):
    prod = load_prod(
        AWS_STORAGE_BUCKET_NAME="apc-media-prod",
        AWS_ACCESS_KEY_ID="test-key",
        AWS_SECRET_ACCESS_KEY="test-secret",
    )

    assert prod.AWS_S3_REGION_NAME == "us-east-1"


def test_s3_bucket_is_private_and_uses_presigned_urls(load_prod):
    """The bucket stays private; images are reached via expiring signed URLs."""
    prod = load_prod(
        AWS_STORAGE_BUCKET_NAME="apc-media-prod",
        AWS_ACCESS_KEY_ID="test-key",
        AWS_SECRET_ACCESS_KEY="test-secret",
    )

    # None => defer to the bucket's Object Ownership setting; never public-read.
    assert prod.AWS_DEFAULT_ACL is None
    assert prod.AWS_QUERYSTRING_AUTH is True
    assert prod.AWS_S3_SIGNATURE_VERSION == "s3v4"


def test_s3_does_not_overwrite_existing_uploads(load_prod):
    """Two photos named image.jpg must not clobber one another."""
    prod = load_prod(
        AWS_STORAGE_BUCKET_NAME="apc-media-prod",
        AWS_ACCESS_KEY_ID="test-key",
        AWS_SECRET_ACCESS_KEY="test-secret",
    )

    assert prod.AWS_S3_FILE_OVERWRITE is False


@pytest.mark.parametrize("bucket", ["", "apc-media-prod"])
def test_staticfiles_stay_on_whitenoise(load_prod, bucket):
    """S3 takes media only — static assets keep the WhiteNoise pipeline."""
    overrides = {}
    if bucket:
        overrides = {
            "AWS_STORAGE_BUCKET_NAME": bucket,
            "AWS_ACCESS_KEY_ID": "test-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
        }
    prod = load_prod(**overrides)

    assert prod.STORAGES["staticfiles"]["BACKEND"] == WHITENOISE_BACKEND
