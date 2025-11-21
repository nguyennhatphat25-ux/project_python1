from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    currency = db.Column(db.String(10))
    members = db.relationship('Member', backref='group', lazy=True)
    expenses = db.relationship('Expense', backref='group', lazy=True)

class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'))

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200))
    amount = db.Column(db.Numeric(10,2))
    category = db.Column(db.String(50))
    paid_by = db.Column(db.String(100))
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'))
    date = db.Column(db.DateTime)
    split_members = db.Column(db.String(200))
