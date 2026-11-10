"""App tests. Run from the project folder:  python -m unittest discover tests -v

They use a temporary database and photo folder, so your real data is never touched.
"""
import base64
import io
import os
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

# Point the app at temporary storage BEFORE importing it
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "test.db")
os.environ["SNAPSHOT_DIR"] = os.path.join(_tmp, "snapshots")
os.environ["PROFILE_DIR"] = os.path.join(_tmp, "profile_photos")
os.environ["SMTP_HOST"] = ""
os.environ["SECRET_KEY"] = "test-secret"
os.environ["KIOSK_KEY"] = "test-kiosk-key"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402
from models import Attendance, Concern, User, db  # noqa: E402

KEY = A.Config.KIOSK_KEY
FAKE_JPEG = "data:image/jpeg;base64," + base64.b64encode(bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"test").decode()


def face(seed):
    """A fake 128-number face descriptor. Same seed = same person."""
    rnd = random.Random(seed)
    return [rnd.uniform(-0.2, 0.2) for _ in range(128)]


def jitter(vec):
    """The same face seen again, with small camera noise."""
    return [x + random.uniform(-0.005, 0.005) for x in vec]


class AppTestCase(unittest.TestCase):
    def setUp(self):
        with A.app.app_context():
            db.drop_all()
            A.upgrade_db()
            admin = User(emp_code="admin", name="Admin", role="admin")
            admin.set_password("admin123")
            emp = User(emp_code="E1", name="Ravi", department="IT", created_at=datetime(2026, 1, 1))
            emp.set_password("secret1")
            db.session.add_all([admin, emp])
            db.session.commit()
            self.emp_id = emp.id
        # Reset in-memory caches between tests
        A._settings_cache = None
        A._face_cache = None
        A._failed_logins.clear()
        A._last_photo_cleanup = None
        self.set_settings(scan_cooldown_seconds=0)
        # Email: off by default; tests that need it turn it on and capture messages
        A.Config.SMTP_HOST, A.Config.SMTP_FROM, A.Config.EMAIL_ASYNC = "", "", False
        self.sent = []
        A._deliver = lambda to, subject, body: self.sent.append((to, subject, body))

    # ---------------- helpers

    def set_settings(self, **values):
        with A.app.app_context():
            A.save_settings(values)

    def post(self, client, url, data=None, json=None):
        """POST with the CSRF token, like a real page does."""
        with client.session_transaction() as s:
            token = s.setdefault("csrf", "test-token")
        if json is not None:
            return client.post(url, json=json, headers={"X-CSRF-Token": token})
        return client.post(url, data={**(data or {}), "csrf_token": token})

    def login(self, code, password, role):
        client = A.app.test_client()
        r = self.post(client, "/login", {"emp_code": code, "password": password, "role": role})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        return client

    def admin(self):
        return self.login("admin", "admin123", "admin")

    def employee(self):
        return self.login("E1", "secret1", "employee")

    def register_face(self, user_id, vec):
        """Admin registers 5 samples of a face for an employee."""
        return self.post(self.admin(), f"/admin/employees/{user_id}/face",
                         json={"descriptors": [jitter(vec) for _ in range(5)]})

    def scan(self, descriptor, photo=None):
        return A.app.test_client().post("/api/kiosk/scan",
                                        json={"key": KEY, "descriptor": descriptor, "photo": photo}).json

    def record(self, day):
        with A.app.app_context():
            return Attendance.query.filter_by(user_id=self.emp_id, date=day).first()

    # ---------------- login

    def test_login_tabs_check_role(self):
        c = A.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        self.assertIn("Admin login", page)
        self.assertIn("Employee login", page)
        r = self.post(c, "/login", {"emp_code": "E1", "password": "secret1", "role": "admin"})
        self.assertIn("not an admin account", r.get_data(as_text=True))
        r = self.post(c, "/login", {"emp_code": "admin", "password": "admin123", "role": "employee"})
        self.assertIn("This is an admin account", r.get_data(as_text=True))

    def test_employee_cannot_open_admin_pages(self):
        self.assertEqual(self.employee().get("/admin").status_code, 403)

    def test_only_admin_can_register_faces(self):
        emp = self.employee()
        self.assertEqual(emp.get("/me/face").status_code, 404)
        self.assertEqual(emp.get(f"/admin/employees/{self.emp_id}/face").status_code, 403)
        r = self.post(emp, f"/admin/employees/{self.emp_id}/face",
                      json={"descriptors": [jitter(face(1)) for _ in range(5)]})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("Register Face", emp.get("/me").get_data(as_text=True))
        self.assertEqual(self.admin().get(f"/admin/employees/{self.emp_id}/face").status_code, 200)

    def test_login_blocked_after_too_many_wrong_passwords(self):
        c = A.app.test_client()
        for _ in range(A.Config.MAX_FAILED_LOGINS):
            self.post(c, "/login", {"emp_code": "E1", "password": "wrong", "role": "employee"})
        r = self.post(c, "/login", {"emp_code": "E1", "password": "secret1", "role": "employee"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Too many wrong attempts", r.get_data(as_text=True))

    def test_post_without_csrf_token_is_rejected(self):
        c = self.admin()
        r = c.post("/admin/employees", data={"emp_code": "E9", "name": "X", "password": "secret1"})
        self.assertEqual(r.status_code, 302)
        with A.app.app_context():
            self.assertIsNone(User.query.filter_by(emp_code="E9").first())

    # ---------------- face + kiosk

    def test_register_face_then_kiosk_check_in_and_out(self):
        me = face(1)
        r = self.register_face(self.emp_id, me)
        self.assertEqual(r.status_code, 200, r.json)

        first = self.scan(jitter(me), FAKE_JPEG)
        self.assertEqual(first["action"], "check_in")
        self.assertEqual(first["name"], "Ravi")
        second = self.scan(jitter(me))
        self.assertEqual(second["action"], "check_out")
        self.assertFalse(self.scan(face(99))["matched"])

        rec = self.record(date.today())
        self.assertIsNotNone(rec.check_out)
        self.assertTrue(os.path.exists(os.path.join(A.Config.SNAPSHOT_DIR, rec.check_in_photo)))

    def test_same_face_cannot_be_registered_twice(self):
        me = face(1)
        self.register_face(self.emp_id, me)
        with A.app.app_context():
            other = User(emp_code="E2", name="Sara")
            other.set_password("secret1")
            db.session.add(other)
            db.session.commit()
            sara_id = other.id
        r = self.register_face(sara_id, me)
        self.assertEqual(r.status_code, 400)
        self.assertIn("already", r.json["error"])

    def test_wrong_kiosk_key_is_rejected(self):
        c = A.app.test_client()
        self.assertEqual(c.get("/kiosk").status_code, 403)
        self.assertEqual(c.post("/api/kiosk/scan", json={"key": "bad", "descriptor": face(1)}).status_code, 403)

    # ---------------- office rules

    def test_month_rules_late_holiday_off_early_half_day(self):
        self.set_settings(office_start="09:00", office_end="17:00", late_grace_minutes=10,
                          half_day_hours=4, weekly_off=[6])
        admin = self.admin()
        self.post(admin, "/admin/settings", {"action": "add_holiday", "date": "2026-10-28", "name": "Diwali"})
        self.post(admin, "/admin/attendance/edit", {"user_id": self.emp_id, "date": "2026-10-29",
                                                    "check_in": "09:05", "check_out": "12:00"})
        self.post(admin, "/admin/attendance/edit", {"user_id": self.emp_id, "date": "2026-10-30",
                                                    "check_in": "09:30", "check_out": "17:30"})
        with A.app.app_context():
            rows, summary = A.month_report(db.session.get(User, self.emp_id), 2026, 10)
        day = {r["date"].day: r for r in rows}
        self.assertEqual(day[28]["status"], "holiday")
        self.assertEqual(day[25]["status"], "off")          # Sunday
        self.assertEqual(day[29]["status"], "present")
        self.assertEqual(day[29]["flags"], ["early", "half_day"])
        self.assertEqual(day[30]["status"], "late")
        self.assertEqual(day[27]["status"], "absent")
        self.assertEqual(summary["half_day"], 1)

    def test_settings_reject_end_before_start(self):
        self.post(self.admin(), "/admin/settings", {
            "action": "save", "office_start": "18:00", "office_end": "09:00", "late_grace_minutes": "10",
            "half_day_hours": "4", "scan_cooldown_seconds": "60", "match_threshold": "0.5",
            "photo_keep_days": "90"})
        with A.app.app_context():
            self.assertEqual(A.S()["office_start"], "09:00")

    # ---------------- admin corrections

    def test_admin_edit_shows_on_employee_sheet(self):
        self.register_face(self.emp_id, face(1))
        self.scan(jitter(face(1)))
        camera_in = self.record(date.today()).check_in.strftime("%I:%M %p")

        self.post(self.admin(), "/admin/attendance/edit", {"user_id": self.emp_id, "date": date.today().isoformat(),
                                                           "check_in": "09:05", "check_out": "17:30"})
        page = self.employee().get("/me").get_data(as_text=True)
        self.assertIn("09:05 AM", page)
        self.assertIn("05:30 PM", page)
        self.assertIn("On time", page)
        self.assertIn("Corrected by admin", page)
        self.assertIn("in " + camera_in, page)  # original camera time is still shown

    def test_status_auto_or_forced(self):
        admin = self.admin()
        day = "2026-10-29"
        self.post(admin, "/admin/attendance/edit", {"user_id": self.emp_id, "date": day, "check_in": "09:30"})
        self.assertEqual(self.record(date(2026, 10, 29)).status, "late")
        self.post(admin, "/admin/attendance/edit",
                  {"user_id": self.emp_id, "date": day, "check_in": "09:30", "status": "present"})
        self.assertEqual(self.record(date(2026, 10, 29)).status, "present")

    def test_kiosk_does_not_change_admin_corrected_checkout(self):
        me = face(1)
        self.register_face(self.emp_id, me)
        self.scan(jitter(me))
        self.post(self.admin(), "/admin/attendance/edit", {"user_id": self.emp_id, "date": date.today().isoformat(),
                                                           "check_in": "09:00", "check_out": "18:00"})
        self.assertEqual(self.scan(jitter(me))["action"], "locked")
        self.assertEqual(self.record(date.today()).check_out.strftime("%H:%M"), "18:00")

    def test_kiosk_can_still_check_out_when_admin_set_only_check_in(self):
        me = face(1)
        self.register_face(self.emp_id, me)
        self.post(self.admin(), "/admin/attendance/edit",
                  {"user_id": self.emp_id, "date": date.today().isoformat(), "check_in": "09:00"})
        self.assertEqual(self.scan(jitter(me))["action"], "check_out")

    def test_concern_with_times_fixed_by_admin_in_one_click(self):
        emp = self.employee()
        self.post(emp, "/me/concerns", {"date": "2026-10-29", "subject": "Wrong time recorded",
                                        "message": "I left at 6", "requested_in": "09:00", "requested_out": "18:00"})
        with A.app.app_context():
            cid = Concern.query.one().id
        admin = self.admin()
        self.assertIn("Correct attendance", admin.get("/admin/concerns").get_data(as_text=True))
        self.post(admin, f"/admin/concerns/{cid}/fix", {"check_in": "09:00", "check_out": "18:00"})
        rec = self.record(date(2026, 10, 29))
        self.assertEqual(rec.check_out.strftime("%H:%M"), "18:00")
        with A.app.app_context():
            self.assertEqual(db.session.get(Concern, cid).status, "resolved")
        self.assertIn("Corrected", emp.get("/me/concerns").get_data(as_text=True))

    def test_concern_rejects_checkout_before_checkin(self):
        self.post(self.employee(), "/me/concerns", {"date": "2026-10-29", "subject": "Wrong time recorded",
                                                    "message": "x", "requested_in": "10:00", "requested_out": "09:00"})
        with A.app.app_context():
            self.assertEqual(Concern.query.count(), 0)

    # ---------------- export + photos

    def test_csv_export(self):
        admin = self.admin()
        self.post(admin, "/admin/attendance/edit", {"user_id": self.emp_id, "date": "2026-10-29",
                                                    "check_in": "09:00", "check_out": "17:00"})
        csv_text = admin.get("/admin/attendance/export?month=2026-10").get_data(as_text=True)
        self.assertIn("2026-10-29", csv_text)
        self.assertIn("On time", csv_text)
        self.assertIn("Corrected by admin", csv_text)

    def test_old_photos_are_deleted(self):
        old_day = date.today() - timedelta(days=200)
        folder = os.path.join(A.Config.SNAPSHOT_DIR, old_day.isoformat())
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "E1_in_090000.jpg"), "wb") as f:
            f.write(b"x")
        with A.app.app_context():
            db.session.add(Attendance(user_id=self.emp_id, date=old_day, check_in=datetime.combine(old_day, datetime.min.time()),
                                      check_in_photo=f"{old_day.isoformat()}/E1_in_090000.jpg"))
            db.session.commit()
        self.admin().get("/admin")  # cleanup runs here
        self.assertFalse(os.path.exists(folder))
        self.assertIsNone(self.record(old_day).check_in_photo)

    def test_all_pages_load(self):
        admin, emp = self.admin(), self.employee()
        for url in ["/admin", "/admin/employees", f"/admin/employees/{self.emp_id}", "/admin/attendance",
                    f"/admin/attendance?employee={self.emp_id}", "/admin/concerns?status=all", "/admin/settings",
                    "/admin/leaves", "/admin/leaves?status=all", "/admin/reports"]:
            self.assertEqual(admin.get(url).status_code, 200, url)
        for url in ["/me", "/me/concerns", "/me/leaves", "/change-password"]:
            self.assertEqual(emp.get(url).status_code, 200, url)
        self.assertEqual(A.app.test_client().get(f"/kiosk?key={KEY}").status_code, 200)


    # ---------------- leave requests

    def apply_leave(self, client, start, end, leave_type="casual", reason="Family function"):
        return self.post(client, "/me/leaves", {"action": "apply", "start_date": start, "end_date": end,
                                                "leave_type": leave_type, "reason": reason})

    def leave(self):
        with A.app.app_context():
            return A.LeaveRequest.query.order_by(A.LeaveRequest.id.desc()).first()

    def test_approved_leave_shows_as_on_leave_not_absent(self):
        emp = self.employee()
        self.apply_leave(emp, "2026-10-26", "2026-10-27", "sick")   # Mon-Tue
        lv = self.leave()
        self.assertEqual(lv.status, "pending")
        with A.app.app_context():
            self.assertEqual(A.working_days(lv.start_date, lv.end_date), 2)

        # Still absent while waiting
        self.assertNotIn("On leave ·", emp.get("/me?month=2026-10").get_data(as_text=True))

        admin = self.admin()
        self.assertIn("Family function", admin.get("/admin/leaves").get_data(as_text=True))
        self.post(admin, f"/admin/leaves/{lv.id}", {"action": "approve", "reply": "Get well soon"})
        self.assertEqual(self.leave().status, "approved")

        with A.app.app_context():
            rows, summary = A.month_report(db.session.get(User, self.emp_id), 2026, 10)
        day = {r["date"].day: r for r in rows}
        self.assertEqual(day[26]["status"], "leave")
        self.assertEqual(day[27]["status"], "leave")
        self.assertEqual(day[25]["status"], "off")       # Sunday stays weekly off
        self.assertEqual(summary["leave"], 2)

        page = emp.get("/me?month=2026-10").get_data(as_text=True)
        self.assertIn("On leave · Sick leave", page)
        self.assertIn("Get well soon", emp.get("/me/leaves").get_data(as_text=True))
        csv_text = admin.get("/admin/attendance/export?month=2026-10").get_data(as_text=True)
        self.assertIn("On leave", csv_text)
        day_view = admin.get("/admin/attendance?date=2026-10-26").get_data(as_text=True)
        self.assertIn("On leave", day_view)

    def test_todays_leave_on_dashboard(self):
        self.set_settings(weekly_off=[])  # so the test also works when run on a Sunday
        today = date.today().isoformat()
        self.apply_leave(self.employee(), today, today)
        admin = self.admin()
        self.post(admin, f"/admin/leaves/{self.leave().id}", {"action": "approve"})
        page = admin.get("/admin").get_data(as_text=True)
        self.assertIn("On leave · Casual leave", page)

    def test_attendance_wins_over_leave(self):
        self.apply_leave(self.employee(), "2026-10-29", "2026-10-29")
        admin = self.admin()
        self.post(admin, f"/admin/leaves/{self.leave().id}", {"action": "approve"})
        self.post(admin, "/admin/attendance/edit", {"user_id": self.emp_id, "date": "2026-10-29",
                                                    "check_in": "09:00", "check_out": "17:00"})
        with A.app.app_context():
            rows, _ = A.month_report(db.session.get(User, self.emp_id), 2026, 10)
        self.assertEqual(rows[28]["status"], "present")

    def test_rejected_and_cancelled_leave_do_not_count(self):
        emp = self.employee()
        self.apply_leave(emp, "2026-10-26", "2026-10-26")
        self.post(self.admin(), f"/admin/leaves/{self.leave().id}", {"action": "reject"})
        self.assertEqual(self.leave().status, "rejected")
        self.apply_leave(emp, "2026-10-26", "2026-10-26")   # can ask again after rejection
        self.post(emp, "/me/leaves", {"action": "cancel", "id": self.leave().id})
        self.assertEqual(self.leave().status, "cancelled")
        with A.app.app_context():
            rows, summary = A.month_report(db.session.get(User, self.emp_id), 2026, 10)
        self.assertEqual(summary["leave"], 0)
        self.assertEqual(rows[25]["status"], "absent")

    def test_leave_validation(self):
        emp = self.employee()
        far_past = (date.today() - timedelta(days=60)).isoformat()
        self.apply_leave(emp, far_past, far_past)                       # too old
        self.apply_leave(emp, "2026-10-27", "2026-10-26")               # end before start
        self.apply_leave(emp, "2026-10-25", "2026-10-25")               # Sunday only: nothing to take
        self.apply_leave(emp, "2026-10-26", "2026-10-26", reason="")    # no reason
        self.assertIsNone(self.leave())
        self.apply_leave(emp, "2026-10-26", "2026-10-28")
        self.apply_leave(emp, "2026-10-27", "2026-10-27")               # overlaps the pending one
        with A.app.app_context():
            self.assertEqual(A.LeaveRequest.query.count(), 1)

    def test_employee_cannot_cancel_others_leave_or_approve(self):
        self.apply_leave(self.employee(), "2026-10-26", "2026-10-26")
        lid = self.leave().id
        with A.app.app_context():
            other = User(emp_code="E2", name="Sara")
            other.set_password("secret1")
            db.session.add(other)
            db.session.commit()
        sara = self.login("E2", "secret1", "employee")
        self.assertEqual(self.post(sara, "/me/leaves", {"action": "cancel", "id": lid}).status_code, 403)
        self.assertEqual(self.post(sara, f"/admin/leaves/{lid}", {"action": "approve"}).status_code, 403)
        self.assertEqual(self.leave().status, "pending")


    # ---------------- no hardcoded secrets

    def test_seed_creates_admin_with_random_password(self):
        import contextlib
        import seed
        with A.app.app_context():
            User.query.filter_by(role="admin").delete()
            db.session.commit()
        os.environ.pop("ADMIN_PASSWORD", None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            seed.create_default_admin()
        password = out.getvalue().split("password:")[1].split()[0]
        self.assertNotEqual(password, "admin123")
        self.assertGreaterEqual(len(password), 10)
        self.login("admin", password, "admin")

    def test_kiosk_key_comes_from_environment(self):
        self.assertEqual(KEY, "test-kiosk-key")
        self.assertEqual(A.app.test_client().get("/kiosk?key=entrance123").status_code, 403)

    # ---------------- profile photo

    PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50

    def upload_photo(self, client, data, name="me.png"):
        with client.session_transaction() as sess:
            token = sess.setdefault("csrf", "test-token")
        return client.post(f"/admin/employees/{self.emp_id}", content_type="multipart/form-data",
                           data={"action": "upload_photo", "csrf_token": token,
                                 "photo": (io.BytesIO(data), name)})

    def test_profile_photo_upload_show_and_remove(self):
        admin = self.admin()
        self.assertNotIn(f"/photos/{self.emp_id}", admin.get("/admin/employees").get_data(as_text=True))
        self.upload_photo(admin, self.PNG)
        with A.app.app_context():
            photo = db.session.get(User, self.emp_id).photo
        self.assertTrue(photo and photo.endswith(".png"))
        self.assertIn(f"/photos/{self.emp_id}", admin.get("/admin/employees").get_data(as_text=True))
        self.assertEqual(admin.get(f"/photos/{self.emp_id}").status_code, 200)
        self.assertEqual(self.employee().get(f"/photos/{self.emp_id}").status_code, 200)  # own photo

        self.post(admin, f"/admin/employees/{self.emp_id}", {"action": "remove_photo"})
        with A.app.app_context():
            self.assertIsNone(db.session.get(User, self.emp_id).photo)
        self.assertFalse(os.path.exists(os.path.join(A.Config.PROFILE_DIR, photo)))

    def test_profile_photo_rejects_non_images(self):
        self.upload_photo(self.admin(), b"<script>alert(1)</script>", "evil.png")
        with A.app.app_context():
            self.assertIsNone(db.session.get(User, self.emp_id).photo)

    def test_other_employee_cannot_see_photo(self):
        self.upload_photo(self.admin(), self.PNG)
        with A.app.app_context():
            other = User(emp_code="E2", name="Sara")
            other.set_password("secret1")
            db.session.add(other)
            db.session.commit()
        sara = self.login("E2", "secret1", "employee")
        self.assertEqual(sara.get(f"/photos/{self.emp_id}").status_code, 403)

    # ---------------- half-day leave

    def test_half_day_leave(self):
        emp = self.employee()
        self.post(emp, "/me/leaves", {"action": "apply", "start_date": "2026-10-26", "end_date": "2026-10-27",
                                      "leave_type": "casual", "reason": "x", "half_day": "1"})
        self.assertIsNone(self.leave())  # half day must be one date
        self.post(emp, "/me/leaves", {"action": "apply", "start_date": "2026-10-26", "end_date": "2026-10-26",
                                      "leave_type": "casual", "reason": "Doctor visit", "half_day": "1"})
        self.post(emp, "/me/leaves", {"action": "apply", "start_date": "2026-10-27", "end_date": "2026-10-27",
                                      "leave_type": "sick", "reason": "Bank work", "half_day": "1"})
        admin = self.admin()
        with A.app.app_context():
            ids = [lv.id for lv in A.LeaveRequest.query.all()]
        for lid in ids:
            self.post(admin, f"/admin/leaves/{lid}", {"action": "approve"})
        # 26th: worked half the day -> present, no "early" / "half day" warnings
        self.post(admin, "/admin/attendance/edit", {"user_id": self.emp_id, "date": "2026-10-26",
                                                    "check_in": "09:00", "check_out": "13:00"})
        with A.app.app_context():
            rows, summary = A.month_report(db.session.get(User, self.emp_id), 2026, 10)
        self.assertEqual(rows[25]["status"], "present")
        self.assertEqual(rows[25]["flags"], ["half_leave"])
        self.assertEqual(rows[26]["status"], "leave")              # 27th: no attendance
        self.assertIn("(half day)", rows[26]["holiday"])
        self.assertEqual(summary["leave"], 0.5)
        page = emp.get("/me/leaves").get_data(as_text=True)
        self.assertIn("(half day)", page)
        self.assertIn(">1.0<", page)  # taken this year: 0.5 + 0.5

    # ---------------- email notifications

    def turn_email_on(self, address="ravi@example.com"):
        A.Config.SMTP_HOST, A.Config.SMTP_FROM = "smtp.example.com", "hr@example.com"
        with A.app.app_context():
            db.session.get(User, self.emp_id).email = address
            db.session.commit()

    def test_no_email_when_not_set_up(self):
        self.apply_leave(self.employee(), "2026-10-26", "2026-10-26")
        self.post(self.admin(), f"/admin/leaves/{self.leave().id}", {"action": "approve"})
        self.assertEqual(self.sent, [])

    def test_email_on_leave_decision(self):
        self.turn_email_on()
        self.apply_leave(self.employee(), "2026-10-26", "2026-10-27")
        self.post(self.admin(), f"/admin/leaves/{self.leave().id}", {"action": "reject", "reply": "Busy week"})
        self.assertEqual(len(self.sent), 1)
        to, subject, body = self.sent[0]
        self.assertEqual(to, "ravi@example.com")
        self.assertIn("rejected", subject)
        self.assertIn("26 Oct 2026 to 27 Oct 2026", body)
        self.assertIn("Busy week", body)

    def test_email_on_concern_fix_and_reply(self):
        self.turn_email_on()
        emp = self.employee()
        self.post(emp, "/me/concerns", {"date": "2026-10-29", "subject": "Check-out not marked",
                                        "message": "x", "requested_in": "09:00", "requested_out": "18:00"})
        admin = self.admin()
        with A.app.app_context():
            cid = Concern.query.one().id
        self.post(admin, f"/admin/concerns/{cid}/fix", {"check_in": "09:00", "check_out": "18:00"})
        self.assertIn("resolved", self.sent[-1][1])
        self.assertIn("06:00 PM", self.sent[-1][2])

        self.post(emp, "/me/concerns", {"subject": "Other", "message": "Camera is slow"})
        with A.app.app_context():
            cid2 = Concern.query.order_by(Concern.id.desc()).first().id
        self.post(admin, f"/admin/concerns/{cid2}", {"reply": "We will check it", "action": "reply"})
        self.assertIn("new reply", self.sent[-1][1])
        self.assertEqual(len(self.sent), 2)

    def test_no_email_for_employee_without_address(self):
        self.turn_email_on(address="")
        self.apply_leave(self.employee(), "2026-10-26", "2026-10-26")
        self.post(self.admin(), f"/admin/leaves/{self.leave().id}", {"action": "approve"})
        self.assertEqual(self.sent, [])

    def test_settings_page_shows_email_status(self):
        self.assertIn("Email is not set up", self.admin().get("/admin/settings").get_data(as_text=True))
        self.turn_email_on()
        self.assertIn("hr@example.com", self.admin().get("/admin/settings").get_data(as_text=True))
        self.post(self.admin(), "/admin/settings/test-email", {"to": "me@example.com"})
        self.assertEqual(self.sent[-1][0], "me@example.com")

    # ---------------- robustness + monthly report

    def test_deactivated_employee_is_logged_out(self):
        emp = self.employee()
        self.assertEqual(emp.get("/me").status_code, 200)
        self.post(self.admin(), f"/admin/employees/{self.emp_id}", {"action": "toggle"})
        self.assertEqual(emp.get("/me").status_code, 302)  # sent back to the login page

    def test_bad_month_and_date_do_not_crash(self):
        admin = self.admin()
        for url in ["/admin/reports?month=2026-13", "/admin/attendance/export?month=abc",
                    f"/admin/attendance?employee={self.emp_id}&month=0-0", "/admin/attendance?date=2026-02-31"]:
            self.assertEqual(admin.get(url).status_code, 200, url)
        self.assertEqual(self.employee().get("/me?month=99999-99").status_code, 200)
        r = self.post(admin, "/admin/attendance/edit", {"user_id": self.emp_id, "date": "not-a-date",
                                                        "check_in": "09:00"})
        self.assertEqual(r.status_code, 302)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.post(admin, "/admin/attendance/edit", {"user_id": self.emp_id, "date": tomorrow, "check_in": "09:00"})
        with A.app.app_context():
            self.assertEqual(Attendance.query.count(), 0)

    def test_role_must_be_admin_or_employee(self):
        admin = self.admin()
        self.post(admin, "/admin/employees", {"emp_code": "E9", "name": "Zed", "password": "secret9", "role": "boss"})
        self.post(admin, f"/admin/employees/{self.emp_id}", {"name": "Ravi", "role": "boss"})
        self.post(admin, f"/admin/employees/{self.emp_id}", {"name": "  ", "role": "employee"})
        with A.app.app_context():
            self.assertIsNone(User.query.filter_by(emp_code="E9").first())
            emp = db.session.get(User, self.emp_id)
            self.assertEqual((emp.role, emp.name), ("employee", "Ravi"))

    def test_redirect_back_stays_on_this_site(self):
        r = A.app.test_client().post("/login", data={"emp_code": "x"}, headers={"Referer": "https://evil.example/x"})
        self.assertNotIn("evil.example", r.headers["Location"])

    def test_security_headers_and_error_pages(self):
        r = self.admin().get("/no-such-page")
        self.assertEqual(r.status_code, 404)
        self.assertIn("This page does not exist", r.get_data(as_text=True))
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Referrer-Policy"], "same-origin")

    def test_kiosk_rejects_broken_requests(self):
        c = A.app.test_client()
        self.assertEqual(c.post("/api/kiosk/scan", json=["not", "an", "object"]).status_code, 403)
        self.assertEqual(c.post("/api/kiosk/scan", json={"key": 123, "descriptor": face(1)}).status_code, 403)
        bad = face(1)
        bad[0] = float("nan")
        r = c.post("/api/kiosk/scan", data='{"key": "%s", "descriptor": %s}' % (KEY, str(bad).replace("nan", "NaN")),
                   content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_monthly_report_and_summary_csv(self):
        admin = self.admin()
        with A.app.app_context():
            other = User(emp_code="=E2", name="Meena", created_at=datetime(2026, 1, 1))
            other.set_password("secret2")
            db.session.add(other)
            db.session.commit()
        for day, check_in in [("2026-10-28", "09:00"), ("2026-10-29", "10:30")]:
            self.post(admin, "/admin/attendance/edit", {"user_id": self.emp_id, "date": day,
                                                        "check_in": check_in, "check_out": "17:00"})
        page = admin.get("/admin/reports?month=2026-10").get_data(as_text=True)
        self.assertIn("Ravi", page)
        self.assertIn("Meena", page)
        with A.app.app_context():
            ravi = db.session.get(User, self.emp_id)
            _, s = A.month_report(ravi, 2026, 10)
            both = A.month_reports(User.query.filter_by(role="employee").all(), 2026, 10)
            self.assertEqual(both[self.emp_id][1], s)  # batch and single reports agree
        self.assertEqual((s["present"], s["late"], s["hours"]), (1, 1, 14.5))
        self.assertEqual(s["percent"], round(2 * 100 / (2 + s["absent"])))
        rows = admin.get("/admin/reports/export?month=2026-10").get_data(as_text=True).splitlines()
        self.assertTrue(rows[0].startswith("Employee ID,Name"))
        self.assertTrue(any(line.startswith("E1,Ravi,IT,1,1,") for line in rows))
        self.assertTrue(any(line.startswith("'=E2,Meena") for line in rows))  # no spreadsheet formulas

    def test_face_count_follows_registration(self):
        self.register_face(self.emp_id, face(1))
        with A.app.app_context():
            self.assertEqual(db.session.get(User, self.emp_id).face_count, 5)
        self.post(self.admin(), f"/admin/employees/{self.emp_id}", {"action": "clear_face"})
        with A.app.app_context():
            self.assertFalse(db.session.get(User, self.emp_id).has_face)


if __name__ == "__main__":
    unittest.main()
