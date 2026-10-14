"""Persisted semester selection; callers own the user lock and transaction."""
from sqlalchemy import func, select

from db import owned
from models import Semester


def next_semester_number(db, user_id):
    """Allocate within the account write lock, without reusing a current number."""
    return db.scalar(select(func.coalesce(func.max(Semester.number), 0)).where(
        Semester.user_id == user_id,
    )) + 1


def get_active_semester(db, user):
    """Read the persisted selection; an unset selection requires semester setup."""
    if user.active_semester_id is None:
        return None
    return owned(db, Semester, user.id, user.active_semester_id)


def set_active_semester(db, user, semester_id):
    semester = owned(db, Semester, user.id, semester_id)
    if user.active_semester_id != semester.id:
        user.active_semester_id = semester.id
        user.semester_state_version += 1
        db.flush()
    return semester
