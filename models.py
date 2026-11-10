import json
from datetime import datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select
from sqlalchemy.orm import column_property
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    emp_code = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120))
    department = db.Column(db.String(80))
    role = db.Column(db.String(20), default="employee")  # admin / employee
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True)
    photo = db.Column(db.String(100))  # optional profile photo file name
    created_at = db.Column(db.DateTime, default=datetime.now)

    faces = db.relationship("FaceSample", backref="user", cascade="all, delete-orphan")
    attendance = db.relationship("Attendance", backref="user", cascade="all, delete-orphan")
    concerns = db.relationship("Concern", backref="user", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_active(self):  # used by Flask-Login
        return bool(self.active)

    @property
    def initials(self):
        return "".join(p[0] for p in self.name.split()[:2]).upper() or "?"

    @property
    def has_face(self):
        # face_count is loaded together with the user (see the bottom of this file),
        # so lists of employees do not run one extra query per person.
        return (self.face_count or 0) > 0


class FaceSample(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    descriptor = db.Column(db.Text, nullable=False)  # JSON list of 128 floats
    created_at = db.Column(db.DateTime, default=datetime.now)

    def vector(self):
        return json.loads(self.descriptor)


class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    date = db.Column(db.Date, nullable=False, index=True)
    check_in = db.Column(db.DateTime)
    check_out = db.Column(db.DateTime)
    status = db.Column(db.String(20), default="present")  # present / late
    note = db.Column(db.String(200))
    last_scan = db.Column(db.DateTime)
    check_in_photo = db.Column(db.String(200))   # path under snapshots/
    check_out_photo = db.Column(db.String(200))
    # Set when an admin corrects this record
    edited_at = db.Column(db.DateTime)
    was_absent = db.Column(db.Boolean, default=False)  # no record existed before the edit
    original_in = db.Column(db.DateTime)                # camera times before the first edit
    original_out = db.Column(db.DateTime)

    __table_args__ = (db.UniqueConstraint("user_id", "date", name="uniq_user_day"),)

    @property
    def hours(self):
        if self.check_in and self.check_out:
            return round((self.check_out - self.check_in).total_seconds() / 3600, 2)
        return None


class Setting(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, nullable=False)  # JSON


class Holiday(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)


class LeaveRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    leave_type = db.Column(db.String(20), nullable=False)  # casual / sick / other
    half_day = db.Column(db.Boolean, default=False)          # only for one-day requests
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="pending")    # pending / approved / rejected / cancelled
    admin_reply = db.Column(db.Text)
    decided_by = db.Column(db.String(100))
    decided_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship("User", backref=db.backref("leaves", cascade="all, delete-orphan"))

    @property
    def days(self):
        """Leave length in days (a half-day leave counts as 0.5)."""
        return 0.5 if self.half_day else (self.end_date - self.start_date).days + 1


class Concern(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    date = db.Column(db.Date)  # the attendance day this concern is about
    requested_in = db.Column(db.String(5))   # "HH:MM" the employee says is correct
    requested_out = db.Column(db.String(5))
    subject = db.Column(db.String(150), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="open")  # open / resolved
    admin_reply = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


# Number of saved face samples, fetched in the same SELECT as the user.
User.face_count = column_property(
    select(func.count(FaceSample.id))
    .where(FaceSample.user_id == User.id)
    .correlate_except(FaceSample)
    .scalar_subquery(),
    deferred=False,
)
