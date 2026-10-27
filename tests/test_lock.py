import pytest

from instascrap.lock import SessionBusy, session_lock


def test_session_lock_is_exclusive(tmp_path):
    with session_lock(tmp_path):
        with pytest.raises(SessionBusy):
            with session_lock(tmp_path, wait=False):
                pass


def test_session_lock_is_released(tmp_path):
    with session_lock(tmp_path):
        pass
    with session_lock(tmp_path, wait=False):
        pass
