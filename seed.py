"""Creates the first admin account from the terminal.  Run:  python seed.py

Optional: the app also offers a set-up page in the browser on first start.

The password comes from the ADMIN_PASSWORD environment variable. If it is not set,
a random password is created and printed once. Change it after the first login.
"""
import os
import secrets

from app import app
from models import User, db

ADMIN_ID = os.environ.get("ADMIN_ID", "admin")


def create_default_admin():
    with app.app_context():
        if User.query.filter_by(role="admin").first():
            print("An admin already exists. Nothing to do.")
            return
        password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        admin = User(emp_code=ADMIN_ID, name="Administrator", role="admin")
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        print(f"Admin created ->  ID: {ADMIN_ID}   password: {password}")
        print("Save this password now and change it after your first login.")


if __name__ == "__main__":
    create_default_admin()
