"""Shared ACP test doubles that avoid persistent state side effects."""

import pytest


class NoopSessionDB:
    """Minimal SessionDB substitute for ACP tests that do not test persistence."""

    def get_session(self, *_args, **_kwargs):
        return None

    def create_session(self, *_args, **_kwargs):
        return None

    def update_session_meta(self, *_args, **_kwargs):
        return None

    def delete_session(self, *_args, **_kwargs):
        return None

    def search_sessions(self, *_args, **_kwargs):
        return []

    def has_archived_messages(self, *_args, **_kwargs):
        return False

    def replace_messages(self, *_args, **_kwargs):
        return None

    def close(self):
        return None


@pytest.fixture
def noop_session_db():
    return NoopSessionDB()
