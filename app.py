from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from twilio.rest import Client
import random
import stripe
import os
import requests

app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Change in prod

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Env vars
TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
TWILIO_PHONE = os.getenv('TWILIO_PHONE')
STRIPE_SECRET = os.getenv('STRIPE_SECRET_KEY')
GROK_API_KEY = os.getenv('GROK_API_KEY')
stripe.api_key = STRIPE_SECRET
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True)
    verified = db.Column(db.Integer, default=0)

class Dispute(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    status = db.Column(db.String(50))

class Party(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispute_id = db.Column(db.Integer, db.ForeignKey('dispute.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    submitted = db.Column(db.Integer, default=0)
    truth = db.Column(db.Text)

class Resolution(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispute_id = db.Column(db.Integer, db.ForeignKey('dispute.id'))
    verdict = db.Column(db.Text)

with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, user_id)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        phone = request.form['phone']
        user = User.query.filter_by(phone=phone).first()
        if not user:
            user = User(phone=phone, verified=0)
            db.session.add(user)
            db.session.commit()
        session['phone'] = phone
        send_verification(phone)
        return redirect(url_for('verify'))
    return render_template('signup.html')

def send_verification(phone):
    code = random.randint(100000, 999999)
    session['code'] = code
    twilio_client.messages.create(body=f'Your code: {code}', from_=TWILIO_PHONE, to=phone)

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if request.method == 'POST':
        code = int(request.form['code'])
        if code == session.get('code'):
            phone = session['phone']
            user = User.query.filter_by(phone=phone).first()
            user.verified = 1
            db.session.commit()
            login_user(user)
            flash('Verified')
            return redirect('/')
        flash('Invalid code')
    return render_template('verify.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        phone = request.form['phone']
        user = User.query.filter_by(phone=phone, verified=1).first()
        if user:
            session['phone'] = phone
            send_verification(phone)
            return redirect(url_for('verify'))
        flash('Sign up first')
    return render_template('signup.html')  # Reuse for login

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')

@app.route('/create_dispute', methods=['GET', 'POST'])
@login_required
def create_dispute():
    if request.method == 'POST':
        dispute = Dispute(creator_id=current_user.id, status='open')
        db.session.add(dispute)
        db.session.commit()
        party = Party(dispute_id=dispute.id, user_id=current_user.id, submitted = 0
        db.session.add(party)
        db.session.commit()
        return redirect(url_for('dispute', dispute_id=dispute.id))
    return render_template('create_dispute.html')

@app.route('/dispute/<int:dispute_id>', methods=['GET', 'POST'])
@login_required
def dispute(dispute_id):
    party = Party.query.filter_by(dispute_id=dispute_id, user_id=current_user.id).first()
    if not party:
        party = Party(dispute_id=dispute_id, user_id=current_user.id, submitted=0)
        db.session.add(party)
        db.session.commit()
    parties = db.session.query(User.phone, Party.submitted).join(Party, User.id == Party.user_id).filter(Party.dispute_id==dispute_id).all()
    total = Party.query.filter_by(dispute_id=dispute_id).count()
    subs = Party.query.filter_by(dispute_id=dispute_id, submitted=1).count()
    if subs == total and total > 1:
        generate_verdict(dispute_id)
    resolution = Resolution.query.filter_by(dispute_id=dispute_id).first()
    verdict = resolution.verdict if resolution else None
    return render_template('dispute.html', dispute_id=dispute_id, parties=parties, verdict=verdict, link=f'{request.host_url}dispute/join/{dispute_id}')

@app.route('/dispute/join/<int:dispute_id>')
def join_dispute(dispute_id):
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    return redirect(url_for('dispute', dispute_id=dispute_id))

@app.route('/submit_truth/<int:dispute_id>', methods=['POST'])
@login_required
def submit_truth(dispute_id):
    truth = request.form['truth']
    try:
        charge = stripe.Charge.create(
            amount=100,
            currency='usd',
            description='Dispute submit',
            source=request.form['stripeToken')  # Fix typo if 'stripeToken'
        )
        party = Party.query.filter_by(dispute_id=dispute_id, user_id=current_user.id).first()
        party.submitted = 1
        party.truth = truth
        db.session.commit()
        flash('Submitted and paid')
    except:
        flash('Payment failed')
    return redirect(url_for('dispute', dispute_id=dispute_id))

def generate_verdict(dispute_id):
    truths = [p.truth for p in Party.query.filter_by(dispute_id=dispute_id).all()]
    prompt = f"Resolve fairly: Party1: {truths[0]} Party2: {truths[1] if len(truths)>1 else ''}"
    headers = {'Authorization': f'Bearer {GROK_API_KEY}', 'Content-Type': 'application/json'}
    data = {'model': 'grok', 'messages': [{'role': 'user', 'content': prompt}]}
    response = requests.post('https://api.x.ai/v1/chat/completions', headers=headers, json=data)  # Adjust if needed
    verdict = response.json()['choices'][0]['message']['content']
    resolution = Resolution(dispute_id=dispute_id, verdict=verdict)
    db.session.add(resolution)
    db.session.commit()
